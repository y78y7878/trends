from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import (
    GoogleTrend,
    GoogleTrendNews,
    KeywordClassification,
    Stock,
    ThemeDailyStats,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.etl import get_system_status, run_full_update


class SystemStatusTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.engine: Engine = get_engine(Path(self.temporary_directory.name) / "status.db")
        init_db(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_empty_database_is_reported_as_stale(self) -> None:
        status = get_system_status(self.engine)

        self.assertEqual(status["health"], "stale")
        self.assertEqual(status["total_keywords"], 0)
        self.assertEqual(status["unanalyzed_news"], 0)

    def test_recent_complete_data_is_healthy(self) -> None:
        now = datetime.now()
        with self.engine.begin() as connection:
            connection.execute(insert(GoogleTrend.__table__).values({
                "trend_id": 1,
                "keyword": "台積電",
                "published_at": now,
                "fetched_at": now,
            }))
            connection.execute(insert(GoogleTrendNews.__table__).values({
                "news_id": 1,
                "trend_id": 1,
                "news_title": "測試新聞",
                "news_sentiment": "Neutral",
                "sentiment_score": 0.8,
                "event_type": "產業趨勢",
            }))
            connection.execute(insert(KeywordClassification.__table__).values({
                "keyword": "台積電",
                "canonical_keyword": "台積電",
                "entity_name": "台積電",
                "entity_type": "上市公司",
                "theme_name": "科技類",
                "sub_theme": "半導體",
                "stock_related": 1,
                "confidence_score": 1.0,
                "classification_source": "gemini",
            }))
            connection.execute(insert(Stock.__table__).values({
                "date": date.today(), "stock_id": "2330",
            }))
            connection.execute(insert(ThemeMapping.__table__).values({
                "theme_name": "科技類",
                "keyword": "台積電",
                "stock_id": "2330",
                "category": "科技類",
                "active": 1,
            }))
            connection.execute(insert(ThemeDailyStats.__table__).values({
                "stat_date": date.today(), "theme_name": "科技類",
                "keyword_count": 1, "news_count": 1, "event_count": 1, "stock_count": 1,
            }))

        status = get_system_status(self.engine)

        self.assertEqual(status["health"], "healthy")
        self.assertEqual(status["total_keywords"], 1)
        self.assertEqual(status["classified_keywords"], 1)
        self.assertEqual(status["analyzed_news"], 1)
        self.assertEqual(status["tracked_stock_count"], 1)

    def test_trends_data_older_than_24_hours_is_stale(self) -> None:
        old_time = datetime.now() - timedelta(hours=25)
        with self.engine.begin() as connection:
            connection.execute(insert(GoogleTrend.__table__).values({
                "keyword": "過期資料",
                "published_at": old_time,
                "fetched_at": old_time,
            }))

        status = get_system_status(self.engine)

        self.assertEqual(status["health"], "stale")

    def test_full_update_runs_each_stage_and_returns_pending_counts(self) -> None:
        with (
            patch("trends.stock_collector.fetch_and_store", return_value=5) as fetch_prices,
            patch("trends.etl.run_entity_resolution_etl", return_value=2) as resolve_entities,
            patch("trends.etl.classify_pending_keywords", return_value=3) as classify_keywords,
            patch("trends.etl.classify_pending_news", return_value=4) as classify_news,
            patch("trends.etl.refresh_theme_daily_stats", return_value=6) as refresh_stats,
            patch("trends.etl.get_system_status", return_value={
                "unclassified_keywords": 7,
                "unanalyzed_news": 8,
            }),
        ):
            result = run_full_update(self.engine)

        fetch_prices.assert_called_once_with(engine=self.engine)
        resolve_entities.assert_called_once_with(self.engine, limit=0)
        classify_keywords.assert_called_once_with(self.engine, limit=0, resolve_entities=False)
        classify_news.assert_called_once_with(self.engine, limit=0)
        refresh_stats.assert_called_once_with(self.engine)
        self.assertEqual(result, {
            "stock_rows": 5,
            "resolved_entities": 2,
            "classified_keywords": 3,
            "classified_news": 4,
            "theme_stats": 6,
            "unclassified_keywords": 7,
            "unanalyzed_news": 8,
        })


if __name__ == "__main__":
    import unittest

    unittest.main()