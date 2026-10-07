from __future__ import annotations

import unittest

import pandas as pd

import trends.dashboard as dashboard
import trends.event_study as event_study
from trends.event_study import MIXED, UNANALYZED, calculate_event_performance


DAY = pd.Timestamp("2026-09-02")


def news(rows: list[tuple[pd.Timestamp, str, str]]) -> pd.DataFrame:
    return pd.DataFrame([
        {"date": date, "keyword": keyword, "news_id": index, "news_sentiment": sentiment}
        for index, (date, keyword, sentiment) in enumerate(rows, start=1)
    ], columns=["date", "keyword", "news_id", "news_sentiment"])


def triggers(*rows: tuple[pd.Timestamp, str]) -> pd.DataFrame:
    return pd.DataFrame([{"event_date": date, "keyword": keyword, "stock_id": "2330"} for date, keyword in rows])


class AnnotateEventSentimentTests(unittest.TestCase):
    def test_display_string_unchanged(self) -> None:
        events = dashboard.annotate_event_sentiment(
            triggers((DAY, "AI"), (DAY, "HBM")),
            news([(DAY, "AI", "Positive"), (DAY, "AI", "Positive"), (DAY, "AI", UNANALYZED)]),
        )

        self.assertEqual(events["news_sentiment"].tolist(), ["Positive 2, 未分析 1", "無新聞"])

    def test_event_fields_from_matching_news(self) -> None:
        events = dashboard.annotate_event_sentiment(
            triggers((DAY, "AI"), (DAY, "HBM")),
            news([
                (DAY, "AI", "Positive"), (DAY, "AI", "Positive"), (DAY, "AI", "Negative"), (DAY, "AI", UNANALYZED),
                (DAY, "HBM", "Positive"), (DAY, "HBM", "Negative"),
            ]),
        )

        ai, hbm = events.to_dict(orient="records")
        self.assertEqual(
            (ai["event_sentiment"], ai["analyzed_news_count"], ai["positive_news_count"], ai["negative_news_count"]),
            ("Positive", 3, 2, 1),
        )
        self.assertEqual((hbm["event_sentiment"], hbm["analyzed_news_count"]), (MIXED, 2))

    def test_news_on_other_date_or_keyword_not_attached(self) -> None:
        events = dashboard.annotate_event_sentiment(
            triggers((DAY, "AI")),
            news([(DAY + pd.Timedelta(days=1), "AI", "Positive"), (DAY, "ai 概念股", "Negative")]),
        )

        self.assertEqual(events.iloc[0]["event_sentiment"], UNANALYZED)
        self.assertEqual(events.iloc[0]["news_sentiment"], "無新聞")

    def test_event_without_news(self) -> None:
        events = dashboard.annotate_event_sentiment(triggers((DAY, "AI")), news([]))

        row = events.iloc[0]
        self.assertEqual((row["news_sentiment"], row["event_sentiment"]), ("無新聞", UNANALYZED))
        self.assertEqual((row["analyzed_news_count"], row["positive_news_count"], row["negative_news_count"]), (0, 0, 0))

    def test_sentiment_constants_single_source(self) -> None:
        self.assertIs(dashboard.UNANALYZED, event_study.UNANALYZED)
        self.assertIs(dashboard.ANALYZED_SENTIMENTS, event_study.ANALYZED_SENTIMENTS)
        self.assertEqual(dashboard.SENTIMENT_LABELS[MIXED], "混合")
        self.assertEqual(dashboard.SENTIMENT_LABELS[UNANALYZED], "未分析")

    def test_performance_keeps_event_sentiment_columns(self) -> None:
        events = dashboard.annotate_event_sentiment(triggers((DAY, "AI")), news([(DAY, "AI", "Negative")]))
        prices = pd.DataFrame({
            "date": pd.bdate_range("2026-09-01", periods=15),
            "stock_id": "2330",
            "close": [100 + index for index in range(15)],
            "high": [101 + index for index in range(15)],
            "low": [99 + index for index in range(15)],
        })

        performance = calculate_event_performance(events, prices)

        for column in ("news_sentiment", "event_sentiment", "analyzed_news_count", "positive_news_count", "negative_news_count"):
            self.assertIn(column, performance.columns)
        self.assertEqual(performance.iloc[0]["event_sentiment"], "Negative")


if __name__ == "__main__":
    unittest.main()
