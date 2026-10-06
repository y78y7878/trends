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
from trends.migrations.p0_1_dedupe_rss import MIGRATION as DEDUPE
from trends.migrations.p0_2_timezone import MIGRATION as TIMEZONE


def create_database(path: Path, trends: list[tuple], news: list[tuple], version: int = 0) -> Path:
    engine = get_engine(path)
    init_db(engine)
    engine.dispose()
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO google_trends (trend_id, keyword, approx_traffic, published_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
        trends,
    )
    connection.executemany(
        "INSERT INTO google_trends_news (news_id, trend_id, news_title, news_url, news_source, fetched_at, "
        "news_sentiment, sentiment_score, event_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        news,
    )
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()
    return path


def snapshot(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        return {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "trends": connection.execute("SELECT * FROM google_trends ORDER BY trend_id").fetchall(),
            "news": connection.execute("SELECT * FROM google_trends_news ORDER BY news_id").fetchall(),
        }
    finally:
        connection.close()


def news_row(news_id: int, trend_id: int, url: str, fetched: str, sentiment: str | None = None) -> tuple:
    analyzed = sentiment is not None
    return (
        news_id, trend_id, f"title {news_id}", url, "source", fetched,
        sentiment, 0.9 if analyzed else None, "其他" if analyzed else None,
    )


# Imported era: published PT wall time + fetched Taipei, 26 chars. SQLite era: PT + UTC, 19 chars.
MIXED_TRENDS = [
    (1, "台積電", "200+", "2026-08-21 00:00:00.000000", "2026-08-21 15:27:59.000000"),
    (2, "台積電", "500+", "2026-08-21 00:00:00.000000", "2026-08-21 15:37:59.000000"),
    (3, "AI", "100+", "2026-10-05 20:40:00", "2026-10-06 03:50:00"),
]
MIXED_NEWS = [
    news_row(1, 1, "https://a", "2026-08-21 15:28:00.000000", "Positive"),
    news_row(2, 3, "https://b", "2026-10-06 03:50:01"),
]


class TimezoneMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = create_database(self.root / "data.db", MIXED_TRENDS, MIXED_NEWS)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_report_is_read_only(self) -> None:
        before = sha256_file(self.db)

        result = report(self.db, TIMEZONE)

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["google_trends_rows_by_era"], {"imported": 2, "sqlite": 1})
        self.assertEqual(result["summary"]["published_at_utc_offsets_applied"], {"+7h": 3})
        self.assertEqual(sha256_file(self.db), before)

    def test_apply_converts_each_era(self) -> None:
        run_migration(self.db, TIMEZONE)

        state = snapshot(self.db)
        trends = {row[0]: (row[3], row[4]) for row in state["trends"]}
        self.assertEqual(state["version"], 1)
        self.assertEqual(trends[1], ("2026-08-21 07:00:00", "2026-08-21 07:27:59"))
        self.assertEqual(trends[3], ("2026-10-06 03:40:00", "2026-10-06 03:50:00"))
        news = {row[0]: row[5] for row in state["news"]}
        self.assertEqual(news, {1: "2026-08-21 07:28:00", 2: "2026-10-06 03:50:01"})

    def test_lag_becomes_small_and_non_negative(self) -> None:
        summary = report(self.db, TIMEZONE)["summary"]

        self.assertAlmostEqual(summary["lag_hours_before"]["imported"]["min"], 15.466, places=2)
        self.assertAlmostEqual(summary["lag_hours_after"]["imported"]["min"], 0.466, places=2)
        self.assertAlmostEqual(summary["lag_hours_after"]["sqlite"]["min"], 0.167, places=2)

    def test_cannot_run_twice(self) -> None:
        run_migration(self.db, TIMEZONE)
        converted = snapshot(self.db)

        with self.assertRaises(MigrationError):
            run_migration(self.db, TIMEZONE)
        self.assertEqual(snapshot(self.db), converted)

    def test_negative_lag_rolls_back_everything(self) -> None:
        db = create_database(self.root / "bad.db", [
            *MIXED_TRENDS,
            (4, "X", "100+", "2026-10-05 21:00:00", "2026-10-06 03:00:00"),
        ], MIXED_NEWS)
        before = snapshot(db)

        with self.assertRaisesRegex(MigrationError, "time-zone assumption"):
            run_migration(db, TIMEZONE)
        self.assertEqual(snapshot(db), before)

    def test_precondition_failure_writes_nothing(self) -> None:
        db = create_database(self.root / "mixed_row.db", [
            (1, "X", "100+", "2026-08-21 00:00:00.000000", "2026-08-21 15:27:59"),
        ], [])
        before = snapshot(db)

        self.assertFalse(report(db, TIMEZONE)["ok"])
        with self.assertRaisesRegex(MigrationError, "layouts differ"):
            run_migration(db, TIMEZONE)
        self.assertEqual(snapshot(db), before)

    def test_ambiguous_dst_time_aborts(self) -> None:
        db = create_database(self.root / "dst.db", [
            (1, "X", "100+", "2026-11-01 01:30:00", "2026-11-01 10:00:00"),
        ], [])
        before = snapshot(db)

        with self.assertRaisesRegex(MigrationError, "Unconvertible"):
            run_migration(db, TIMEZONE)
        self.assertEqual(snapshot(db), before)

    def test_digest_mismatch_aborts(self) -> None:
        before = snapshot(self.db)

        with self.assertRaisesRegex(MigrationError, "digest mismatch"):
            run_migration(self.db, TIMEZONE, expected_digest="0" * 64)
        self.assertEqual(snapshot(self.db), before)

    def test_non_empty_derived_table_blocks_migration(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute(
            "INSERT INTO news_theme_classification (news_id, keyword, theme_name, confidence_score) VALUES (1, 'AI', '科技類', 1)"
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(MigrationError, "news_theme_classification must be empty"):
            run_migration(self.db, TIMEZONE)


DEDUPE_TRENDS = [
    (1, "台積電", "100+", "2026-09-01 01:00:00", "2026-09-01 01:05:00"),
    (2, "台積電", "200+", "2026-09-01 01:00:00", "2026-09-01 01:15:00"),
    (3, "台積電", "500+", "2026-09-01 01:00:00", "2026-09-01 01:25:00"),
    (4, "台積電", "100+", "2026-09-03 01:00:00", "2026-09-03 01:05:00"),
    (5, "AI", "100+", "2026-09-01 01:00:00", "2026-09-01 01:05:00"),
]
DEDUPE_NEWS = [
    news_row(1, 1, "https://u1", "2026-09-01 01:05:00"),
    news_row(2, 2, "https://u1", "2026-09-01 01:15:00", "Positive"),
    news_row(3, 3, "https://u1", "2026-09-01 01:25:00"),
    news_row(4, 2, "https://u2", "2026-09-01 01:15:00"),
    news_row(5, 4, "https://u1", "2026-09-03 01:05:00"),
    news_row(6, 5, "https://u3", "2026-09-01 01:05:00", "Negative"),
]


class DedupeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = create_database(self.root / "data.db", DEDUPE_TRENDS, DEDUPE_NEWS, version=1)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_report_counts(self) -> None:
        before = sha256_file(self.db)

        summary = report(self.db, DEDUPE)["summary"]

        self.assertEqual((summary["google_trends_before"], summary["google_trends_after"]), (5, 3))
        self.assertEqual((summary["google_trends_news_before"], summary["google_trends_news_after"]), (6, 4))
        self.assertEqual(summary["sentiment_merges"], 1)
        self.assertEqual((summary["analyzed_news_before"], summary["analyzed_news_after"]), (2, 2))
        self.assertEqual(sha256_file(self.db), before)

    def test_apply_keeps_first_trend_latest_traffic_and_merges_sentiment(self) -> None:
        run_migration(self.db, DEDUPE)

        state = snapshot(self.db)
        trends = {row[0]: row for row in state["trends"]}
        news = {row[0]: row for row in state["news"]}
        self.assertEqual(state["version"], 2)
        self.assertEqual(sorted(trends), [1, 4, 5])
        self.assertEqual(trends[1][2], "500+")
        self.assertEqual(trends[1][4], "2026-09-01 01:05:00")
        self.assertEqual(sorted(news), [1, 4, 5, 6])
        self.assertEqual(news[1][1], 1)
        self.assertEqual((news[1][6], news[1][7], news[1][8]), ("Positive", 0.9, "其他"))
        self.assertEqual(news[4][1], 1)
        self.assertEqual(news[5][1], 4, "a re-trend with a new pubDate is a separate event")

    def test_conflicting_sentiment_aborts(self) -> None:
        db = create_database(self.root / "conflict.db", DEDUPE_TRENDS[:2], [
            news_row(1, 1, "https://u1", "2026-09-01 01:05:00", "Positive"),
            news_row(2, 2, "https://u1", "2026-09-01 01:15:00", "Negative"),
        ], version=1)
        before = snapshot(db)

        with self.assertRaisesRegex(MigrationError, "Conflicting sentiments"):
            run_migration(db, DEDUPE)
        self.assertEqual(snapshot(db), before)

    def test_requires_timezone_migration_first(self) -> None:
        db = create_database(self.root / "v0.db", DEDUPE_TRENDS, DEDUPE_NEWS, version=0)

        with self.assertRaisesRegex(MigrationError, "user_version=1"):
            run_migration(db, DEDUPE)

    def test_survivor_must_be_first_fetch(self) -> None:
        db = create_database(self.root / "order.db", [
            (1, "X", "100+", "2026-09-01 01:00:00", "2026-09-01 02:00:00"),
            (2, "X", "100+", "2026-09-01 01:00:00", "2026-09-01 01:30:00"),
        ], [], version=1)
        before = snapshot(db)

        with self.assertRaisesRegex(MigrationError, "Survivor invariant"):
            run_migration(db, DEDUPE)
        self.assertEqual(snapshot(db), before)

    def test_cannot_run_twice(self) -> None:
        run_migration(self.db, DEDUPE)

        with self.assertRaises(MigrationError):
            run_migration(self.db, DEDUPE)

    def test_timezone_then_dedupe_pipeline(self) -> None:
        db = create_database(self.root / "pipeline.db", [
            (1, "AI", "100+", "2026-10-05 20:40:00", "2026-10-06 03:50:00"),
            (2, "AI", "200+", "2026-10-05 20:40:00", "2026-10-06 04:00:00"),
        ], [
            news_row(1, 1, "https://u1", "2026-10-06 03:50:00"),
            news_row(2, 2, "https://u1", "2026-10-06 04:00:00"),
        ])

        run_migration(db, TIMEZONE)
        run_migration(db, DEDUPE)

        state = snapshot(db)
        self.assertEqual(state["version"], 2)
        self.assertEqual([row[0] for row in state["trends"]], [1])
        self.assertEqual(state["trends"][0][3], "2026-10-06 03:40:00")
        self.assertEqual([row[0] for row in state["news"]], [1])


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = create_database(self.root / "data.db", MIXED_TRENDS, MIXED_NEWS)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(TIMEZONE, ["--db", str(self.db), *argv])
        return code, output.getvalue()

    def test_backup_refuses_to_overwrite(self) -> None:
        target = self.root / "backup.db"
        backup_database(self.db, target)

        with self.assertRaises(MigrationError):
            backup_database(self.db, target)

    def test_rehearse_changes_only_the_copy(self) -> None:
        before = snapshot(self.db)
        copy = self.root / "rehearsal.db"

        code, output = self.run_cli("--rehearse", str(copy))

        self.assertEqual(code, 0, output)
        self.assertEqual(snapshot(self.db), before)
        self.assertEqual(snapshot(copy)["version"], 1)
        self.assertIn("--expected-plan", output)

    def test_apply_requires_rehearsal_digest_and_confirmation(self) -> None:
        before = snapshot(self.db)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(TIMEZONE, ["--db", str(self.db), "--apply"])
        code, _ = self.run_cli("--apply", "--expected-plan", "0" * 64, "--confirm")

        self.assertEqual(code, 1)
        self.assertEqual(snapshot(self.db), before)

    def test_apply_backs_up_and_restore_rolls_back(self) -> None:
        original = snapshot(self.db)
        digest = report(self.db, TIMEZONE)["digest"]
        backup_dir = self.root / "backups"

        code, output = self.run_cli(
            "--apply", "--expected-plan", digest, "--confirm", "--backup-dir", str(backup_dir)
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(snapshot(self.db)["version"], 1)
        backup = next(backup_dir.glob("*.db"))

        with self.assertRaises(MigrationError):
            restore_database(backup, self.db, "0" * 64)
        result = restore_database(backup, self.db, sha256_file(backup))

        self.assertEqual(snapshot(self.db), original)
        self.assertEqual(snapshot(Path(result["previous_copy"]))["version"], 1)


if __name__ == "__main__":
    unittest.main()
