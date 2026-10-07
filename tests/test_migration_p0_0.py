from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from trends.database import get_engine, init_db
from trends.migrations._common import (
    MigrationError,
    backup_database,
    main,
    report,
    restore_database,
    run_migration,
    sha256_file,
)
from trends.migrations.p0_0_fix_event_analysis import MIGRATION as FIX_EVENT_ANALYSIS
from trends.migrations.p0_1_dedupe_rss import MIGRATION as DEDUPE
from trends.migrations.p0_2_timezone import MIGRATION as TIMEZONE


# Verbatim definition of the legacy table found in the production data.db.
LEGACY_EVENT_ANALYSIS = """
CREATE TABLE event_analysis (
    id INTEGER NOT NULL,
    trend_id INTEGER NOT NULL,
    news_id INTEGER,
    stock_id VARCHAR(16) NOT NULL,
    event_date DATE NOT NULL,
    keyword VARCHAR(255) NOT NULL,
    news_sentiment VARCHAR(20),
    return_1d FLOAT, return_3d FLOAT, return_5d FLOAT, return_10d FLOAT,
    future_return_1d FLOAT, future_return_3d FLOAT, future_return_5d FLOAT, future_return_10d FLOAT,
    PRIMARY KEY (id),
    UNIQUE (trend_id, news_id, stock_id),
    FOREIGN KEY(trend_id) REFERENCES google_trends (id) ON DELETE CASCADE,
    FOREIGN KEY(news_id) REFERENCES google_trends_news (id) ON DELETE CASCADE
)
"""
TRENDS = [
    (1, "台積電", "100+", "2026-10-05 20:40:00", "2026-10-06 03:50:00"),
    (2, "台積電", "200+", "2026-10-05 20:40:00", "2026-10-06 04:00:00"),
]
NEWS = [
    (1, 1, "t1", "https://u1", "s", "2026-10-06 03:50:00", "Positive", 0.9, "其他"),
    (2, 2, "t1", "https://u1", "s", "2026-10-06 04:00:00", None, None, None),
]
EXPECTED_FOREIGN_KEYS = {("google_trends", "trend_id", "trend_id"), ("google_trends_news", "news_id", "news_id")}


def create_legacy_database(path: Path, event_rows: int = 0, version: int = 0) -> Path:
    engine = get_engine(path)
    init_db(engine)
    engine.dispose()
    connection = sqlite3.connect(path)
    # A legacy database has neither the RSS unique indexes (added by init_db from C4 on) nor a correct event_analysis.
    for index in ("uq_google_trends_keyword_published", "uq_google_trends_news_trend_url"):
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    connection.execute("DROP TABLE event_analysis")
    connection.execute(LEGACY_EVENT_ANALYSIS)
    connection.execute("CREATE INDEX ix_event_analysis_event_date ON event_analysis (event_date)")
    connection.executemany(
        "INSERT INTO google_trends (trend_id, keyword, approx_traffic, published_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
        TRENDS,
    )
    connection.executemany(
        "INSERT INTO google_trends_news (news_id, trend_id, news_title, news_url, news_source, fetched_at, "
        "news_sentiment, sentiment_score, event_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        NEWS,
    )
    for row_id in range(1, event_rows + 1):
        connection.execute(
            "INSERT INTO event_analysis (id, trend_id, news_id, stock_id, event_date, keyword) VALUES (?, 1, 1, '2330', '2026-10-06', 'x')",
            (row_id,),
        )
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()
    return path


def state(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        return {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "foreign_keys": {(r[2], r[3], r[4]) for r in connection.execute("PRAGMA foreign_key_list(event_analysis)")},
            "other_schema": connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name <> 'event_analysis' ORDER BY type, name"
            ).fetchall(),
            "trends": connection.execute("SELECT * FROM google_trends ORDER BY trend_id").fetchall(),
            "news": connection.execute("SELECT * FROM google_trends_news ORDER BY news_id").fetchall(),
            "event_rows": connection.execute("SELECT COUNT(*) FROM event_analysis").fetchone()[0],
            "event_indexes": sorted(r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'event_analysis' AND sql IS NOT NULL"
            )),
        }
    finally:
        connection.close()


def foreign_key_check(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


class FixEventAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = create_legacy_database(self.root / "data.db")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_fixture_reproduces_production_failure(self) -> None:
        with self.assertRaisesRegex(sqlite3.OperationalError, "foreign key mismatch"):
            foreign_key_check(self.db)

    def test_report_is_read_only_and_detects_wrong_keys(self) -> None:
        before = sha256_file(self.db)

        result = report(self.db, FIX_EVENT_ANALYSIS)

        self.assertTrue(result["ok"])
        self.assertFalse(result["summary"]["already_correct"])
        self.assertIn(["google_trends_news", "news_id", "id"], result["summary"]["foreign_keys_before"])
        self.assertEqual(sha256_file(self.db), before)

    def test_apply_rebuilds_only_event_analysis(self) -> None:
        before = state(self.db)

        run_migration(self.db, FIX_EVENT_ANALYSIS)

        after = state(self.db)
        self.assertEqual(after["foreign_keys"], EXPECTED_FOREIGN_KEYS)
        self.assertEqual(foreign_key_check(self.db), [])
        self.assertEqual(after["version"], 0)
        self.assertEqual(after["event_rows"], 0)
        self.assertEqual(after["event_indexes"], ["ix_event_analysis_event_date"])
        for key in ("other_schema", "trends", "news"):
            self.assertEqual(after[key], before[key], key)

    def test_parent_deletes_work_after_fix(self) -> None:
        run_migration(self.db, FIX_EVENT_ANALYSIS)

        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM google_trends_news WHERE news_id = 2")
        connection.execute("DELETE FROM google_trends WHERE trend_id = 2")
        connection.rollback()
        connection.close()

    def test_non_empty_table_aborts_without_changes(self) -> None:
        db = create_legacy_database(self.root / "busy.db", event_rows=1)
        before = state(db)

        with self.assertRaisesRegex(MigrationError, "must be empty"):
            run_migration(db, FIX_EVENT_ANALYSIS)
        self.assertEqual(state(db), before)

    def test_second_run_is_a_no_op(self) -> None:
        run_migration(self.db, FIX_EVENT_ANALYSIS)
        fixed = state(self.db)

        result = run_migration(self.db, FIX_EVENT_ANALYSIS)

        self.assertTrue(result["summary"]["already_correct"])
        self.assertEqual(state(self.db), fixed)

    def test_refuses_after_timezone_migration(self) -> None:
        db = create_legacy_database(self.root / "v1.db", version=1)

        with self.assertRaisesRegex(MigrationError, "user_version=0"):
            run_migration(db, FIX_EVENT_ANALYSIS)

    def test_missing_table_aborts(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute("DROP TABLE event_analysis")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(MigrationError, "missing table"):
            run_migration(self.db, FIX_EVENT_ANALYSIS)


class LegacySchemaRunnerTests(unittest.TestCase):
    """The runner must cope with the broken legacy schema without tracebacks."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = create_legacy_database(self.root / "data.db")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_backup_of_legacy_database_succeeds(self) -> None:
        target = self.root / "backup.db"

        backup_database(self.db, target)

        self.assertTrue(target.exists())

    def test_timezone_migration_aborts_cleanly_before_fix(self) -> None:
        before = state(self.db)

        with self.assertRaisesRegex(MigrationError, "p0_0_fix_event_analysis"):
            run_migration(self.db, TIMEZONE)
        self.assertEqual(state(self.db), before)

    def test_full_p0_pipeline_on_legacy_schema(self) -> None:
        run_migration(self.db, FIX_EVENT_ANALYSIS)
        run_migration(self.db, TIMEZONE)
        run_migration(self.db, DEDUPE)

        after = state(self.db)
        self.assertEqual(after["version"], 2)
        self.assertEqual([row[0] for row in after["trends"]], [1])
        self.assertEqual([(row[0], row[6]) for row in after["news"]], [(1, "Positive")])
        self.assertEqual(foreign_key_check(self.db), [])

    def run_cli(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(FIX_EVENT_ANALYSIS, ["--db", str(self.db), *argv])
        return code, output.getvalue()

    def test_cli_rehearse_apply_and_restore(self) -> None:
        original = state(self.db)
        code, output = self.run_cli("--rehearse", str(self.root / "rehearsal.db"))
        self.assertEqual(code, 0, output)
        self.assertEqual(state(self.db), original)
        self.assertEqual(state(self.root / "rehearsal.db")["foreign_keys"], EXPECTED_FOREIGN_KEYS)

        digest = report(self.db, FIX_EVENT_ANALYSIS)["digest"]
        code, output = self.run_cli("--apply", "--expected-plan", digest, "--confirm", "--backup-dir", str(self.root / "b"))
        self.assertEqual(code, 0, output)
        self.assertEqual(state(self.db)["foreign_keys"], EXPECTED_FOREIGN_KEYS)

        backup = next((self.root / "b").glob("*.db"))
        restore_database(backup, self.db, sha256_file(backup))
        self.assertEqual(state(self.db), original)


if __name__ == "__main__":
    unittest.main()
