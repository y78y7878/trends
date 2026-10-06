from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import trends.dashboard as dashboard
from trends.database import get_engine, init_db


SRC = Path(dashboard.__file__).resolve().parent


class PrepareThemeNewsDateTests(unittest.TestCase):
    def observations(self, published: list[object], fetched: list[object]) -> pd.DataFrame:
        return pd.DataFrame({
            "keyword": "AI",
            "news_id": list(range(1, len(published) + 1)),
            "news_title": "t",
            "news_source": "s",
            "news_url": [f"u{index}" for index in range(len(published))],
            "news_sentiment": None,
            "sentiment_score": None,
            "published_at": published,
            "fetched_at": fetched,
        })

    def test_business_date_is_taipei_day_of_published_utc(self) -> None:
        news = dashboard.prepare_theme_news(self.observations(
            [pd.Timestamp("2026-10-05 15:59:00"), pd.Timestamp("2026-10-05 16:00:00")],
            [pd.Timestamp("2026-10-05 16:10:00"), pd.Timestamp("2026-10-05 16:10:00")],
        ), ["AI"])

        self.assertEqual(news["date"].tolist(), [pd.Timestamp("2026-10-05"), pd.Timestamp("2026-10-06")])

    def test_published_at_wins_over_fetched_at_and_falls_back_when_missing(self) -> None:
        news = dashboard.prepare_theme_news(self.observations(
            [pd.Timestamp("2026-10-04 23:00:00"), None],
            [pd.Timestamp("2026-10-05 17:00:00"), pd.Timestamp("2026-10-05 17:00:00")],
        ), ["AI"])

        self.assertEqual(news["date"].tolist(), [pd.Timestamp("2026-10-05"), pd.Timestamp("2026-10-06")])

    def test_unanalyzed_sentiment_still_separated(self) -> None:
        news = dashboard.prepare_theme_news(self.observations(
            [pd.Timestamp("2026-10-05 01:00:00")], [pd.Timestamp("2026-10-05 01:00:00")],
        ), ["AI"])

        self.assertEqual(news["news_sentiment"].tolist(), [dashboard.UNANALYZED])


class ResearchStartFilterTests(unittest.TestCase):
    def test_research_start_is_taipei_midnight(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fresh.db"
            engine = init_db(get_engine(path))
            connection = sqlite3.connect(path)
            connection.executemany(
                "INSERT INTO google_trends (trend_id, keyword, published_at, fetched_at) VALUES (?, ?, ?, ?)",
                [
                    (1, "before", "2026-08-20 15:59:59", "2026-08-20 16:05:00"),
                    (2, "start", "2026-08-20 16:00:00", "2026-08-20 16:05:00"),
                    (3, "later", "2026-09-01 00:00:00", "2026-09-01 00:05:00"),
                ],
            )
            connection.commit()
            connection.close()
            try:
                with patch.object(dashboard, "database_engine", return_value=engine):
                    dashboard.load_trend_observations.clear()
                    observations = dashboard.load_trend_observations()
                    dashboard.load_trend_observations.clear()
            finally:
                engine.dispose()

        self.assertEqual(sorted(observations["keyword"]), ["later", "start"])


class TimeConventionTests(unittest.TestCase):
    """Guards for P0: one time toolkit (timeutil), no local-clock calls, no Neutral fill-in."""

    def source(self, name: str) -> str:
        return (SRC / name).read_text(encoding="utf-8")

    def test_dashboard_does_not_fill_missing_sentiment_with_neutral(self) -> None:
        self.assertNotIn('fillna("Neutral")', self.source("dashboard.py"))

    def test_display_and_etl_code_use_timeutil_only(self) -> None:
        forbidden = re.compile(r"datetime\.now\(|date\.today\(|Timestamp\.now\(|Timestamp\.today\(|ZoneInfo\(|tz_localize\(|tz_convert\(")
        for name in ("dashboard.py", "etl.py", "rss_collector.py"):
            with self.subTest(name=name):
                self.assertIsNone(forbidden.search(self.source(name)), name)

    def test_time_zones_are_defined_only_in_timeutil(self) -> None:
        offenders = [
            path.name for path in SRC.rglob("*.py")
            if path.name != "timeutil.py" and "ZoneInfo(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
