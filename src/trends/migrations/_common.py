"""Shared runner for one-off data migrations.

Safety model:
- ``--report`` opens the database read-only and only prints the plan.
- ``--rehearse COPY`` snapshots the database to COPY and applies the migration there.
- ``--apply`` requires the plan digest printed by the rehearsal, takes a verified backup,
  then applies everything in one ``BEGIN IMMEDIATE`` transaction. Any failed precondition,
  digest mismatch or post-verification failure rolls the whole transaction back.
- ``--restore BACKUP`` puts a verified backup back in place (keeping the current file aside).

``PRAGMA user_version`` records which migration has been applied, so a migration cannot run
twice or out of order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from trends.database import DB_PATH


DEFAULT_BACKUP_DIR = DB_PATH.parent / "backups"


class MigrationError(RuntimeError):
    pass


@dataclass
class Plan:
    summary: dict[str, object]
    changes: dict[str, list]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        self.digest = compute_digest(self.changes)


class Migration(Protocol):
    name: str
    from_version: int
    to_version: int

    def check_preconditions(self, connection: sqlite3.Connection) -> list[str]: ...

    def build_plan(self, connection: sqlite3.Connection) -> Plan: ...

    def apply(self, connection: sqlite3.Connection, plan: Plan) -> None: ...

    def verify(self, connection: sqlite3.Connection, plan: Plan) -> list[str]: ...


def compute_digest(changes: dict[str, list]) -> str:
    payload = json.dumps(changes, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise MigrationError(f"Database not found: {resolved}")
    if readonly:
        connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, isolation_level=None)
    else:
        connection = sqlite3.connect(resolved, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def row_count(connection: sqlite3.Connection, table: str) -> int:
    if not table_exists(connection, table):
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def storage_failures(connection: sqlite3.Connection) -> list[str]:
    """Page-level corruption only; used to verify that a backup is a faithful, readable copy."""
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    return [] if integrity == ["ok"] else [f"integrity_check: {integrity[:5]}"]


def integrity_failures(connection: sqlite3.Connection) -> list[str]:
    """Storage integrity plus foreign-key consistency; a malformed FK definition is reported, not raised."""
    failures = storage_failures(connection)
    try:
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.OperationalError as error:
        failures.append(f"foreign_key_check: {error} (run p0_0_fix_event_analysis first)")
    else:
        if foreign_keys:
            failures.append(f"foreign_key_check: {len(foreign_keys)} violation(s), e.g. {foreign_keys[:3]}")
    return failures


def _check_version(connection: sqlite3.Connection, migration: Migration) -> None:
    version = user_version(connection)
    if version != migration.from_version:
        raise MigrationError(
            f"{migration.name} requires user_version={migration.from_version}, found {version}"
        )


def report(path: str | Path, migration: Migration) -> dict[str, object]:
    connection = connect(path, readonly=True)
    try:
        _check_version(connection, migration)
        failures = migration.check_preconditions(connection)
        if failures:
            return {"migration": migration.name, "ok": False, "precondition_failures": failures}
        plan = migration.build_plan(connection)
        return {"migration": migration.name, "ok": True, "digest": plan.digest, "summary": plan.summary}
    finally:
        connection.close()


def run_migration(
    path: str | Path,
    migration: Migration,
    expected_digest: str | None = None,
) -> dict[str, object]:
    """Apply ``migration`` atomically. On any failure nothing is written."""
    started = time.perf_counter()
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _check_version(connection, migration)
            failures = migration.check_preconditions(connection)
            if failures:
                raise MigrationError(f"Precondition failures: {failures}")
            plan = migration.build_plan(connection)
            if expected_digest is not None and plan.digest != expected_digest:
                raise MigrationError(
                    f"Plan digest mismatch: expected {expected_digest}, computed {plan.digest}. "
                    "The data changed since the rehearsal; rehearse again."
                )
            migration.apply(connection, plan)
            failures = migration.verify(connection, plan) + integrity_failures(connection)
            if failures:
                raise MigrationError(f"Verification failures: {failures}")
            connection.execute(f"PRAGMA user_version = {int(migration.to_version)}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return {
        "migration": migration.name,
        "ok": True,
        "digest": plan.digest,
        "user_version": migration.to_version,
        "seconds": round(time.perf_counter() - started, 3),
        "summary": plan.summary,
    }


def backup_database(source: str | Path, destination: str | Path) -> str:
    """Consistent online snapshot via the SQLite backup API. Returns the SHA-256 of the copy."""
    destination = Path(destination)
    if destination.exists():
        raise MigrationError(f"Refusing to overwrite existing file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = connect(source, readonly=True)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    check = connect(destination, readonly=True)
    try:
        failures = storage_failures(check)
    finally:
        check.close()
    if failures:
        destination.unlink(missing_ok=True)
        raise MigrationError(f"Backup failed verification: {failures}")
    return sha256_file(destination)


def restore_database(backup: str | Path, target: str | Path, expected_sha256: str) -> dict[str, str]:
    """Replace ``target`` with ``backup`` after verifying it; the replaced file is kept aside."""
    backup, target = Path(backup), Path(target)
    actual = sha256_file(backup)
    if actual != expected_sha256:
        raise MigrationError(f"Backup checksum mismatch: expected {expected_sha256}, got {actual}")
    if Path(f"{target}-journal").exists():
        raise MigrationError("Target has a hot journal; stop every process using the database first")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    set_aside = target.with_name(f"{target.stem}_replaced_{stamp}{target.suffix}")
    backup_database(target, set_aside)
    source_connection = connect(backup, readonly=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return {"restored_from": str(backup), "previous_copy": str(set_aside)}


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(migration: Migration, argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog=f"python -m trends.migrations.{migration.name}")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database (default: project data.db)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="read-only plan and summary")
    mode.add_argument("--rehearse", type=Path, metavar="COPY", help="snapshot --db to COPY and apply there")
    mode.add_argument("--apply", action="store_true", help="apply to --db (needs --expected-plan and --confirm)")
    mode.add_argument("--restore", type=Path, metavar="BACKUP", help="restore BACKUP over --db")
    parser.add_argument("--expected-plan", help="plan digest printed by --rehearse")
    parser.add_argument("--expected-sha256", help="backup checksum printed by --apply (for --restore)")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--confirm", action="store_true", help="required for --apply and --restore")
    args = parser.parse_args(argv)

    try:
        if args.report:
            _print(report(args.db, migration))
        elif args.rehearse:
            snapshot_sha = backup_database(args.db, args.rehearse)
            result = run_migration(args.rehearse, migration)
            result.update({"rehearsal_copy": str(args.rehearse), "source_snapshot_sha256": snapshot_sha})
            _print(result)
            print(f"\nNext: --apply --expected-plan {result['digest']} --confirm")
        elif args.apply:
            if not (args.confirm and args.expected_plan):
                parser.error("--apply requires --expected-plan DIGEST and --confirm")
            preview = report(args.db, migration)
            if not preview["ok"] or preview["digest"] != args.expected_plan:
                raise MigrationError(f"Live plan does not match the rehearsal: {preview.get('digest')}")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = args.backup_dir / f"{Path(args.db).stem}_pre_{migration.name}_{stamp}.db"
            backup_sha = backup_database(args.db, backup_path)
            print(f"Backup: {backup_path}\nSHA-256: {backup_sha}")
            result = run_migration(args.db, migration, expected_digest=args.expected_plan)
            result.update({"backup": str(backup_path), "backup_sha256": backup_sha})
            _print(result)
            print(f"\nRollback: --restore {backup_path} --expected-sha256 {backup_sha} --confirm")
        else:
            if not (args.confirm and args.expected_sha256):
                parser.error("--restore requires --expected-sha256 and --confirm")
            _print(restore_database(args.restore, args.db, args.expected_sha256))
    except MigrationError as error:
        print(f"ABORTED (no changes written): {error}", file=sys.stderr)
        return 1
    return 0
