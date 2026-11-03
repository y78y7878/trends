from __future__ import annotations

import sqlite3
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from trends.db_backup import backup, main


class DbBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.db = root / "project" / "data.db"
        self.db.parent.mkdir()
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE t (value INTEGER)")
        connection.execute("INSERT INTO t VALUES (42)")
        connection.commit()
        connection.close()
        self.db.with_name("data.gemini_progress.json").write_text("{}", encoding="utf-8")
        self.cloud = root / "drive"
        self.cloud.mkdir()
        self.dest = self.cloud / "trends_backup"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_archive_holds_a_readable_snapshot_and_progress(self) -> None:
        archive = backup(self.db, self.dest, keep=14, today=date(2026, 11, 3))

        self.assertEqual(archive.name, "data_20261103.zip")
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(sorted(bundle.namelist()), ["data.db", "data.gemini_progress.json"])
            bundle.extract("data.db", self.cloud / "restore")
        restored = sqlite3.connect(self.cloud / "restore" / "data.db")
        self.assertEqual(restored.execute("SELECT value FROM t").fetchone()[0], 42)
        restored.close()

    def test_only_the_newest_archives_are_kept(self) -> None:
        self.dest.mkdir()
        (self.dest / "notes.txt").write_text("keep me", encoding="utf-8")
        for days in range(5):
            backup(self.db, self.dest, keep=3, today=date(2026, 11, 1) + timedelta(days=days))

        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), [
            "data_20261103.zip", "data_20261104.zip", "data_20261105.zip", "notes.txt",
        ])

    def test_missing_cloud_drive_fails_without_writing_locally(self) -> None:
        missing = Path(self.directory.name) / "unmounted" / "trends_backup"

        self.assertEqual(main(["--db", str(self.db), "--dest", str(missing)]), 1)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
