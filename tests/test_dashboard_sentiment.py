from __future__ import annotations

import unittest

import pandas as pd

from trends.dashboard import (
    LOW_SENTIMENT_COVERAGE_MESSAGE,
    UNANALYZED,
    build_data_quality_check,
    build_sentiment_verification_sample,
    format_sentiment_coverage,
    is_low_sentiment_coverage,
    prepare_theme_news,
    sentiment_coverage_pct,
    summarize_sentiment_distribution,
)


def _news(sentiments: list[object]) -> pd.DataFrame:
    return pd.DataFrame({
        "news_title": [f"title {index}" for index in range(len(sentiments))],
        "news_source": "source",
        "news_sentiment": sentiments,
        "sentiment_score": [0.8 if value is not None else None for value in sentiments],
        "date": pd.Timestamp("2026-09-01"),
    })


class SentimentDistributionTests(unittest.TestCase):
    def test_percentages_use_analyzed_news_only(self) -> None:
        summary = summarize_sentiment_distribution(_news(["Positive", "Positive", "Negative", "Neutral", None, None]))

        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["analyzed"], 4)
        self.assertEqual(summary["unanalyzed"], 2)
        self.assertEqual((summary["positive_n"], summary["neutral_n"], summary["negative_n"]), (2, 1, 1))
        self.assertAlmostEqual(summary["positive_pct"], 50.0)
        self.assertAlmostEqual(summary["neutral_pct"], 25.0)
        self.assertAlmostEqual(summary["negative_pct"], 25.0)
        self.assertAlmostEqual(summary["coverage_pct"], 4 / 6 * 100)

    def test_unanalyzed_news_is_not_counted_as_neutral(self) -> None:
        summary = summarize_sentiment_distribution(_news(["Positive"] * 9 + [None] * 387))

        self.assertEqual(summary["neutral_n"], 0)
        self.assertAlmostEqual(summary["positive_pct"], 100.0)
        self.assertAlmostEqual(summary["coverage_pct"], 9 / 396 * 100)
        self.assertTrue(summary["low_coverage"])

    def test_no_analyzed_news_returns_none_percentages(self) -> None:
        summary = summarize_sentiment_distribution(_news([None, None]))

        self.assertEqual(summary["analyzed"], 0)
        self.assertIsNone(summary["positive_pct"])
        self.assertIsNone(summary["neutral_pct"])
        self.assertIsNone(summary["negative_pct"])
        self.assertEqual(summary["coverage_pct"], 0.0)
        self.assertTrue(summary["low_coverage"])

    def test_empty_news_has_no_coverage(self) -> None:
        summary = summarize_sentiment_distribution(pd.DataFrame())

        self.assertEqual(summary["total"], 0)
        self.assertIsNone(summary["coverage_pct"])
        self.assertFalse(summary["low_coverage"])

    def test_unknown_labels_are_treated_as_unanalyzed(self) -> None:
        summary = summarize_sentiment_distribution(_news(["positive", "Mixed", ""]))

        self.assertEqual(summary["positive_n"], 1)
        self.assertEqual(summary["unanalyzed"], 2)


class SentimentCoverageTests(unittest.TestCase):
    def test_coverage_threshold_is_twenty_percent(self) -> None:
        self.assertTrue(is_low_sentiment_coverage(19.9))
        self.assertFalse(is_low_sentiment_coverage(20.0))
        self.assertFalse(is_low_sentiment_coverage(None))

    def test_coverage_formatting(self) -> None:
        self.assertAlmostEqual(sentiment_coverage_pct(120, 5923), 120 / 5923 * 100)
        self.assertIsNone(sentiment_coverage_pct(0, 0))
        self.assertEqual(format_sentiment_coverage(120, 5923), "120 / 5,923（2.0%）")
        self.assertEqual(format_sentiment_coverage(0, 0), "0 / 0（N/A）")
        self.assertEqual(LOW_SENTIMENT_COVERAGE_MESSAGE, "情緒樣本不足，請謹慎解讀")


class SentimentPresentationTests(unittest.TestCase):
    def test_prepare_theme_news_marks_missing_sentiment_as_unanalyzed(self) -> None:
        observations = pd.DataFrame({
            "keyword": ["AI", "AI", "AI"],
            "news_id": [1, 2, 3],
            "news_title": ["a", "b", "c"],
            "news_source": "source",
            "news_url": ["u1", "u2", "u3"],
            "news_sentiment": ["Positive", None, "negative"],
            "sentiment_score": [0.9, None, 0.7],
            "fetched_at": "2026-09-01 10:00:00",
            "published_at": "2026-09-01 09:00:00",
        })

        news = prepare_theme_news(observations, ["AI"])

        self.assertEqual(news["news_sentiment"].tolist(), ["Positive", UNANALYZED, "Negative"])
        self.assertNotIn("Neutral", news["news_sentiment"].tolist())

    def test_verification_sample_lists_analyzed_news_only(self) -> None:
        sample = build_sentiment_verification_sample(_news(["Positive", None, "Neutral", None]))

        self.assertEqual(sample["新聞標題"].tolist(), ["title 0", "title 2"])
        self.assertTrue(build_sentiment_verification_sample(_news([None, None])).empty)

    def test_quality_check_neutral_warning_uses_analyzed_news(self) -> None:
        mostly_unanalyzed = build_data_quality_check(pd.DataFrame(), _news(["Positive"] + [None] * 9))
        mostly_neutral = build_data_quality_check(pd.DataFrame(), _news(["Neutral"] * 9 + ["Positive"]))

        self.assertFalse(mostly_unanalyzed["sentiment_warning"])
        self.assertTrue(mostly_unanalyzed["low_sentiment_coverage"])
        self.assertEqual((mostly_unanalyzed["analyzed_news"], mostly_unanalyzed["total_news"]), (1, 10))
        self.assertTrue(mostly_neutral["sentiment_warning"])
        self.assertFalse(mostly_neutral["low_sentiment_coverage"])


if __name__ == "__main__":
    unittest.main()
