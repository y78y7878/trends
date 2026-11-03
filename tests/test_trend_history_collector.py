from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from trends.database import GoogleTrendsHistory, ThemeMapping, get_engine, init_db
from trends.trend_history_collector import run_history_collection, save_history


class RateLimited(Exception):
    def __init__(self) -> None:
        super().__init__("The request failed: Google returned a response with code 429")
        self.response = SimpleNamespace(status_code=429)


class FakeTrendClient:
    def __init__(self, failures: dict[str, list[Exception]] | None = None) -> None:
        self.failures = {keyword: list(errors) for keyword, errors in (failures or {}).items()}
        self.requested: list[str] = []
        self.keyword = ""

    def build_payload(self, kw_list, timeframe, geo) -> None:
        self.keyword = kw_list[0]
        self.requested.append(self.keyword)
        pending = self.failures.get(self.keyword)
        if pending:
            raise pending.pop(0)

    def interest_over_time(self) -> pd.DataFrame:
        dates = pd.date_range(date.today() - timedelta(days=2), date.today(), freq="D", name="date")
        return pd.DataFrame({self.keyword: [10, 20, 30], "isPartial": [False, False, True]}, index=dates)


class RunHistoryCollectionTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.engine: Engine = init_db(get_engine(Path(self.temporary_directory.name) / "history.db"))
        sync_patch = patch("trends.trend_history_collector.sync_theme_mapping", lambda engine: 0)
        sync_patch.start()
        self.addCleanup(sync_patch.stop)
        sleep_patch = patch("trends.trend_history_collector.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def add_keywords(self, *keywords: str, theme: str = "科技類", stock_id: str | None = "2330") -> None:
        with self.engine.begin() as connection:
            connection.execute(ThemeMapping.__table__.insert(), [
                {"theme_name": theme, "keyword": keyword, "stock_id": stock_id, "category": "company", "active": 1}
                for keyword in keywords
            ])

    def run_collection(self, client: FakeTrendClient):
        return run_history_collection(self.engine, request_delay=0, trend_client=client, classify_keywords=False)

    def history_count(self) -> int:
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(GoogleTrendsHistory))

    def test_collects_keywords_and_drops_partial_day(self) -> None:
        self.add_keywords("台積電", "鴻海")
        client = FakeTrendClient()

        outcome = self.run_collection(client)

        self.assertEqual((outcome.success, outcome.failed, outcome.skipped), (2, 0, 0))
        self.assertFalse(outcome.rate_limited)
        self.assertEqual(outcome.rows, 4)
        self.assertEqual(self.history_count(), 4)

    def test_skips_keyword_already_updated_to_yesterday(self) -> None:
        self.add_keywords("台積電", "鴻海")
        yesterday = date.today() - timedelta(days=1)
        save_history(self.engine, "科技類", "鴻海", pd.DataFrame({"trend_date": [yesterday], "trend_score": [50]}))
        client = FakeTrendClient()

        outcome = self.run_collection(client)

        self.assertEqual(client.requested, ["台積電"])
        self.assertEqual((outcome.success, outcome.skipped), (1, 1))

    def test_stale_keyword_is_collected_again(self) -> None:
        self.add_keywords("鴻海")
        stale = date.today() - timedelta(days=2)
        save_history(self.engine, "科技類", "鴻海", pd.DataFrame({"trend_date": [stale], "trend_score": [50]}))
        client = FakeTrendClient()

        outcome = self.run_collection(client)

        self.assertEqual(client.requested, ["鴻海"])
        self.assertEqual((outcome.success, outcome.skipped), (1, 0))

    def test_rate_limit_stops_run_without_retry(self) -> None:
        self.add_keywords("A", "B", "C")
        client = FakeTrendClient(failures={"B": [RateLimited()]})

        outcome = self.run_collection(client)

        self.assertEqual(client.requested, ["A", "B"])
        self.assertTrue(outcome.rate_limited)
        self.assertEqual((outcome.success, outcome.failed, outcome.not_attempted), (1, 1, 1))
        self.sleep.assert_not_called()
        self.assertIn("rate limited", outcome.summary())

    def test_other_errors_are_retried_then_counted_as_failed(self) -> None:
        self.add_keywords("A", "B")
        client = FakeTrendClient(failures={"A": [ValueError("boom")] * 3})

        outcome = self.run_collection(client)

        self.assertEqual(client.requested, ["A", "A", "A", "B"])
        self.assertFalse(outcome.rate_limited)
        self.assertEqual((outcome.success, outcome.failed), (1, 1))

    def test_transient_error_recovers_on_retry(self) -> None:
        self.add_keywords("A")
        client = FakeTrendClient(failures={"A": [ValueError("timeout")]})

        outcome = self.run_collection(client)

        self.assertEqual(client.requested, ["A", "A"])
        self.assertEqual((outcome.success, outcome.failed), (1, 0))

    def test_rotation_queries_stock_mapped_then_stalest_keywords_first(self) -> None:
        self.add_keywords("其他甲", "其他乙", theme="其他", stock_id=None)
        self.add_keywords("舊股", "新股")
        stale = date.today() - timedelta(days=5)
        save_history(self.engine, "科技類", "舊股", pd.DataFrame({"trend_date": [stale], "trend_score": [10]}))
        save_history(self.engine, "其他", "其他甲", pd.DataFrame({"trend_date": [stale], "trend_score": [10]}))
        client = FakeTrendClient()

        self.run_collection(client)

        # Stock-mapped first (never collected before stale), then the rest the same way.
        self.assertEqual(client.requested, ["新股", "舊股", "其他乙", "其他甲"])

    def test_rate_limited_runs_rotate_instead_of_repeating_the_same_keywords(self) -> None:
        self.add_keywords("A", "B", "C", "D")
        first = FakeTrendClient(failures={"C": [RateLimited()]})
        self.run_collection(first)
        second = FakeTrendClient()

        self.run_collection(second)

        self.assertEqual(first.requested, ["A", "B", "C"])
        self.assertEqual(second.requested, ["C", "D"])
