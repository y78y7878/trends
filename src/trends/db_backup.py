"""Daily off-site backup of data.db (replaces keeping data.db in git, which was nearing GitHub's
100 MB per-file limit).

Each run takes a consistent snapshot with SQLite's online backup API (safe while the collectors
write), zips it together with the Gemini progress file and writes ``data_YYYYMMDD.zip`` into a
synced cloud folder (default: Google Drive ``G:\\我的雲端硬碟\\trends_backup``, or
``TRENDS_BACKUP_DIR``). Only the newest ``--keep`` archives are kept. Usage::

    python -m trends.db_backup                      # for Windows Task Scheduler (run_backup.bat)
    python -m trends.db_backup --dest D:\\backup --keep 7

Restore: unzip ``data.db`` (and ``data.gemini_progress.json``) into the project root while no
scheduled task is running.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from trends.database import DB_PATH


DEFAULT_DEST = Path(os.getenv("TRENDS_BACKUP_DIR", r"G:\我的雲端硬碟\trends_backup"))
DEFAULT_KEEP = 14
ARCHIVE = re.compile(r"data_(\d{8})\.zip")


def snapshot(db_path: Path, destination: Path) -> None:
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def backup(db_path: Path = DB_PATH, dest: Path = DEFAULT_DEST, keep: int = DEFAULT_KEEP, today: date | None = None) -> Path:
    if not db_path.is_file():
        raise FileNotFoundError(f"資料庫不存在：{db_path}")
    if not dest.parent.exists():
        # The cloud drive is not mounted; creating the folder would back up to the local disk only.
        raise FileNotFoundError(f"備份位置不存在（雲端硬碟未啟動？）：{dest.parent}")
    dest.mkdir(exist_ok=True)
    archive = dest / f"data_{(today or date.today()):%Y%m%d}.zip"
    progress = db_path.with_name(f"{db_path.stem}.gemini_progress.json")
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / db_path.name
        snapshot(db_path, copy)
        # Write next to the target and rename, so the sync client never uploads a half-written zip.
        partial = archive.with_suffix(".zip.partial")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            bundle.write(copy, db_path.name)
            if progress.is_file():
                bundle.write(progress, progress.name)
        os.replace(partial, archive)
    for old in sorted((path for path in dest.iterdir() if ARCHIVE.fullmatch(path.name)), reverse=True)[keep:]:
        old.unlink()
    return archive


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Back up data.db to a synced cloud folder")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    args = parser.parse_args(argv)
    try:
        archive = backup(args.db, args.dest, max(args.keep, 1))
    except (OSError, sqlite3.Error) as error:
        print(f"Backup failed: {error}")
        return 1
    print(f"Backup written: {archive} ({archive.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
