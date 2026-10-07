"""P0-0: rebuild the empty ``event_analysis`` table whose foreign keys point at missing columns.

The legacy table (created by an older ORM) declares ``REFERENCES google_trends (id)`` and
``REFERENCES google_trends_news (id)``, but the parent keys are ``trend_id`` / ``news_id``.
SQLite then raises "foreign key mismatch" on ``PRAGMA foreign_key_check`` and on every DELETE
from the parent tables, which blocks p0_1_dedupe_rss.

Only that table is touched, and only while it is empty: it is dropped and recreated from the
current ORM definition inside the runner's single transaction. ``user_version`` stays 0, so it
must run before p0_2_timezone. Re-running on a fixed database is a no-op. Usage::

    python -m trends.migrations.p0_0_fix_event_analysis --report
"""
from __future__ import annotations

import sqlite3

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from trends.database import EventAnalysis
from trends.migrations._common import Plan, main, row_count, table_exists


TABLE = "event_analysis"


def _ddl() -> list[str]:
    dialect = sqlite_dialect.dialect()
    table = EventAnalysis.__table__
    statements = [str(CreateTable(table).compile(dialect=dialect)).strip()]
    statements += [str(CreateIndex(index).compile(dialect=dialect)).strip() for index in sorted(table.indexes, key=lambda i: i.name)]
    return statements


def _expected_foreign_keys() -> set[tuple[str, str, str]]:
    return {
        (key.column.table.name, key.parent.name, key.column.name)
        for key in EventAnalysis.__table__.foreign_keys
    }


def _actual_foreign_keys(connection: sqlite3.Connection) -> set[tuple[str, str, str | None]]:
    return {(row[2], row[3], row[4]) for row in connection.execute(f"PRAGMA foreign_key_list({TABLE})")}


def _schema_objects(connection: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    """Every schema object except event_analysis and its indexes, to prove nothing else changed."""
    return connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name <> ? ORDER BY type, name", (TABLE,)
    ).fetchall()


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {name: row_count(connection, name) for name in names}


class FixEventAnalysisMigration:
    name = "p0_0_fix_event_analysis"
    from_version = 0
    to_version = 0

    def check_preconditions(self, connection: sqlite3.Connection) -> list[str]:
        if not table_exists(connection, TABLE):
            return [f"missing table {TABLE}"]
        rows = row_count(connection, TABLE)
        return [f"{TABLE} must be empty (has {rows} rows); aborting without changes"] if rows else []

    def build_plan(self, connection: sqlite3.Connection) -> Plan:
        old_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
        ).fetchone()[0]
        actual, expected = _actual_foreign_keys(connection), _expected_foreign_keys()
        needs_rebuild = actual != expected
        summary = {
            "already_correct": not needs_rebuild,
            "foreign_keys_before": sorted(map(list, actual)),
            "foreign_keys_after": sorted(map(list, expected)),
            "table_row_counts": _table_counts(connection),
            "other_schema_objects": len(_schema_objects(connection)),
        }
        changes = {"rebuild": [old_sql, *_ddl()] if needs_rebuild else []}
        return Plan(summary=summary, changes=changes)

    def apply(self, connection: sqlite3.Connection, plan: Plan) -> None:
        if not plan.changes["rebuild"]:
            return
        plan.summary["_schema_before"] = _schema_objects(connection)
        connection.execute(f"DROP TABLE {TABLE}")
        for statement in plan.changes["rebuild"][1:]:
            connection.execute(statement)

    def verify(self, connection: sqlite3.Connection, plan: Plan) -> list[str]:
        failures = []
        actual = _actual_foreign_keys(connection)
        if actual != _expected_foreign_keys():
            failures.append(f"{TABLE} foreign keys still wrong: {sorted(actual)}")
        if row_count(connection, TABLE):
            failures.append(f"{TABLE} is not empty after rebuild")
        if _table_counts(connection) != plan.summary["table_row_counts"]:
            failures.append("row counts of other tables changed")
        before = plan.summary.pop("_schema_before", None)
        if before is not None and _schema_objects(connection) != before:
            failures.append("schema of other tables changed")
        return failures


MIGRATION = FixEventAnalysisMigration()


if __name__ == "__main__":
    raise SystemExit(main(MIGRATION))
