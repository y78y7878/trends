from __future__ import annotations

import unittest

import pandas as pd

from trends.event_study import (
    MIXED,
    UNANALYZED,
    build_event_research_overview,
    build_event_research_summary,
    build_event_research_text,
    build_hypothesis_validation,
    calculate_event_performance,
    derive_event_sentiment,
    detect_research_events,
)


class ResearchEventTests(unittest.TestCase):
    def test_heat_growth_and_news_volume_create_events(self) -> None:
        dates = pd.date_range("2026-09-01", periods=9, freq="D")
        heat = pd.DataFrame({"date": dates, "keyword": "AI", "heat": [20, 20, 20, 20, 20, 20, 20, 40, 20]})
        news = pd.DataFrame({
            "date": [*dates[1:8], dates[8], dates[8], dates[8]],
            "keyword": "AI",
            "news_id": list(range(1, 11)),
        })

        events = detect_research_events(heat, news)

        event = events.loc[events["event_date"].eq(dates[8])].iloc[0]
        self.assertEqual(events["event_date"].dt.normalize().tolist(), [dates[8], dates[7]])
        self.assertEqual(event["trigger"], "新聞量增加")
        self.assertEqual(event["news_count"], 3)
        self.assertEqual(events.iloc[1]["trigger"], "搜尋熱度上升")

    def test_news_spike_from_zero_baseline_creates_event(self) -> None:
        dates = pd.date_range("2026-09-01", periods=8, freq="D")
        heat = pd.DataFrame({"date": dates, "keyword": "AI", "heat": 0})
        news = pd.DataFrame({"date": [dates[-1]] * 3, "keyword": "AI", "news_id": [1, 2, 3]})

        events = detect_research_events(heat, news)

        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["trigger"], "新聞量增加")

    def test_heat_spike_without_news_creates_event(self) -> None:
        dates = pd.date_range("2026-09-01", periods=8, freq="D")
        heat = pd.DataFrame({"date": dates, "keyword": "AI", "heat": [20, 20, 20, 20, 20, 20, 20, 40]})

        events = detect_research_events(heat, pd.DataFrame())

        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["trigger"], "搜尋熱度上升")

    def test_event_returns_use_trading_days_and_ten_day_extremes(self) -> None:
        dates = pd.bdate_range("2026-09-01", periods=12)
        prices = pd.DataFrame({
            "date": dates,
            "stock_id": "2330",
            "close": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111],
            "high": [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 120, 112],
            "low": [99, 99, 101, 102, 103, 104, 105, 106, 107, 108, 91, 110],
        })
        events = pd.DataFrame([{"event_date": dates[0], "keyword": "AI", "stock_id": "2330"}])

        result = calculate_event_performance(events, prices).iloc[0]

        self.assertAlmostEqual(result["return_1d"], 0.01)
        self.assertAlmostEqual(result["return_3d"], 0.03)
        self.assertAlmostEqual(result["max_gain_10d"], 0.20)
        self.assertAlmostEqual(result["max_loss_10d"], -0.09)

    def test_summary_and_narrative_are_generated(self) -> None:
        performance = pd.DataFrame([
            {
                "keyword": "ChatGPT",
                "event_date": pd.Timestamp("2026-10-02"),
                "event_heat": 100.0,
                "heat_change": 1.22,
                "return_1d": 0.02,
                "return_3d": 0.052,
                "return_5d": 0.06,
                "return_10d": 0.09,
                "reaction_days": 2,
                "news_sentiment": "Positive",
                "stock_id": "2330",
                "ma20_breakout_day": 2,
                "ma60_breakout_day": None,
            },
            {
                "keyword": "OpenAI",
                "event_date": pd.Timestamp("2026-10-03"),
                "event_heat": 70.0,
                "heat_change": -0.2,
                "return_1d": -0.01,
                "return_3d": -0.04,
                "return_5d": -0.03,
                "return_10d": -0.07,
                "reaction_days": 3,
                "news_sentiment": "Negative",
                "stock_id": "2330",
                "ma20_breakout_day": None,
                "ma60_breakout_day": None,
            },
        ])

        summary = build_event_research_summary(performance)
        narrative = build_event_research_text(performance.iloc[0], performance)

        self.assertEqual(summary["event_count"], 2)
        self.assertAlmostEqual(summary["avg_return_1d_pct"], 0.5)
        self.assertAlmostEqual(summary["avg_return_3d_pct"], 0.6)
        self.assertIn("符合熱度上升後股價正向反應假說", narrative)
        self.assertIn("熱度由", narrative)
        self.assertIn("MA20", narrative)


def _performance(rows: list[dict[str, object]]) -> pd.DataFrame:
    defaults = {
        "keyword": "AI",
        "event_date": pd.Timestamp("2026-10-01"),
        "return_1d": pd.NA,
        "return_3d": pd.NA,
        "return_5d": pd.NA,
        "return_10d": pd.NA,
        "reaction_days": pd.NA,
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


class ResearchOverviewTests(unittest.TestCase):
    def overview(self, performance: pd.DataFrame) -> str:
        return build_event_research_overview(
            "科技類", pd.Timestamp("2026-09-01"), pd.Timestamp("2026-10-06"), performance
        )

    def test_missing_ten_day_returns_show_insufficient_data(self) -> None:
        text = self.overview(_performance([
            {"return_1d": 0.01, "reaction_days": 2},
            {"return_1d": -0.02, "reaction_days": 4},
        ]))

        self.assertIn("正報酬事件：資料不足", text)
        self.assertIn("負報酬事件：資料不足", text)
        self.assertIn("平均10日報酬：資料不足", text)
        self.assertIn("平均1日報酬：-0.5%", text)
        self.assertIn("平均首次反應天數：3.0 天", text)

    def test_missing_reaction_days_show_insufficient_data(self) -> None:
        text = self.overview(_performance([
            {"return_1d": 0.01, "return_3d": 0.02, "return_5d": 0.03, "return_10d": 0.04},
        ]))

        self.assertIn("平均首次反應天數：資料不足", text)
        self.assertIn("平均10日報酬：4.0%", text)

    def test_single_event_without_any_returns_does_not_raise(self) -> None:
        text = self.overview(_performance([{}]))

        self.assertIn("事件數量：1", text)
        for label in ("平均1日報酬", "平均3日報酬", "平均5日報酬", "平均10日報酬", "平均首次反應天數"):
            self.assertIn(f"{label}：資料不足", text)

    def test_negative_ratio_excludes_zero_returns(self) -> None:
        text = self.overview(_performance([
            {"return_10d": 0.05},
            {"return_10d": 0.0},
            {"return_10d": -0.02},
        ]))

        self.assertIn("正報酬事件：33.3%", text)
        self.assertIn("負報酬事件：33.3%", text)


class EventTableFormatTests(unittest.TestCase):
    def test_first_reaction_days_render_as_days(self) -> None:
        from trends.dashboard import format_event_table_value

        self.assertEqual(format_event_table_value(2, "首次反應天數"), "2 天")
        self.assertEqual(format_event_table_value(3.0, "首次反應天數"), "3 天")
        self.assertEqual(format_event_table_value(pd.NA, "首次反應天數"), "資料尚不足")
        self.assertEqual(format_event_table_value(0.05, "10日報酬"), "5.00%")


class DeriveEventSentimentTests(unittest.TestCase):
    def test_derive_positive_negative_neutral_majority(self) -> None:
        self.assertEqual(derive_event_sentiment({"Positive": 3, "Negative": 1, "Neutral": 1}), "Positive")
        self.assertEqual(derive_event_sentiment({"Positive": 1, "Negative": 2}), "Negative")
        self.assertEqual(derive_event_sentiment({"Positive": 1, "Neutral": 4, "Negative": 2}), "Neutral")

    def test_derive_tie_is_mixed(self) -> None:
        self.assertEqual(derive_event_sentiment({"Positive": 2, "Negative": 2}), MIXED)
        self.assertEqual(derive_event_sentiment({"Positive": 1, "Neutral": 1}), MIXED)
        self.assertEqual(derive_event_sentiment({"Positive": 1, "Neutral": 1, "Negative": 1}), MIXED)

    def test_derive_without_analysed_news_is_unanalyzed(self) -> None:
        self.assertEqual(derive_event_sentiment({}), UNANALYZED)
        self.assertEqual(derive_event_sentiment({UNANALYZED: 4}), UNANALYZED)
        self.assertEqual(derive_event_sentiment({"Positive": 0, "Negative": 0, "Neutral": 0}), UNANALYZED)

    def test_derive_ignores_unanalyzed_count(self) -> None:
        self.assertEqual(derive_event_sentiment({"Positive": 1, UNANALYZED: 5}), "Positive")


def _h2_events(rows: list[tuple[str, float | None]], display: str | None = None, analysed: int | None = None) -> pd.DataFrame:
    frame = pd.DataFrame([
        {
            "keyword": f"k{index}",
            "event_date": pd.Timestamp("2026-09-01") + pd.Timedelta(days=index),
            "event_sentiment": sentiment,
            "news_sentiment": display or f"{sentiment} 2, 未分析 1",
            "return_10d": value,
        }
        for index, (sentiment, value) in enumerate(rows)
    ])
    if analysed is not None:
        frame["analyzed_news_count"] = analysed
    return frame


def _h2(performance: pd.DataFrame) -> dict[str, object]:
    return build_hypothesis_validation(performance)[1]


class HypothesisTwoTests(unittest.TestCase):
    def test_h2_uses_event_sentiment_not_display_string(self) -> None:
        performance = _h2_events([("Positive", 0.05)] * 5 + [("Negative", -0.02)] * 5)

        result = _h2(performance)

        self.assertIn("Positive 2, 未分析 1", performance["news_sentiment"].tolist())
        self.assertEqual(result["status"], "成立")
        self.assertIn("Positive 事件 5 個", result["basis"])

    def test_h2_threshold_requires_five_per_group(self) -> None:
        below = _h2(_h2_events([("Positive", 0.05)] * 5 + [("Negative", -0.02)] * 4))
        enough = _h2(_h2_events([("Positive", 0.05)] * 5 + [("Negative", -0.02)] * 5))

        self.assertEqual(below["status"], "資料不足")
        self.assertIn("Negative 事件 4 個", below["basis"])
        self.assertEqual(enough["status"], "成立")

    def test_h2_counts_only_events_with_return_10d(self) -> None:
        result = _h2(_h2_events([("Positive", 0.05)] * 5 + [("Negative", -0.02)] * 4 + [("Negative", None)] * 3))

        self.assertEqual(result["status"], "資料不足")
        self.assertIn("Negative 事件 4 個", result["basis"])

    def test_h2_established_and_not_established(self) -> None:
        higher = _h2(_h2_events([("Positive", 0.05)] * 5 + [("Negative", -0.02)] * 5))
        lower = _h2(_h2_events([("Positive", -0.03)] * 5 + [("Negative", 0.01)] * 5))
        equal = _h2(_h2_events([("Positive", 0.01)] * 5 + [("Negative", 0.01)] * 5))

        self.assertEqual(higher["status"], "成立")
        self.assertIn("平均 10 日報酬 5.0%", higher["basis"])
        self.assertEqual(lower["status"], "不成立")
        self.assertEqual(equal["status"], "不成立")

    def test_h2_excludes_neutral_mixed_unanalyzed(self) -> None:
        rows = [("Positive", 0.05)] * 4 + [("Neutral", 0.1), (MIXED, 0.1), (UNANALYZED, 0.1)] + [("Negative", -0.02)] * 5

        result = _h2(_h2_events(rows))

        self.assertEqual(result["status"], "資料不足")
        self.assertIn("Positive 事件 4 個、Negative 事件 5 個", result["basis"])

    def test_h2_basis_reports_sample_sizes(self) -> None:
        performance = _h2_events([(UNANALYZED, 0.01)] * 3, display="無新聞", analysed=0)

        result = _h2(performance)

        self.assertEqual(result["status"], "資料不足")
        self.assertEqual(
            result["basis"],
            "Positive 事件 0 個、Negative 事件 0 個（各需至少 5 個有 10 日報酬的事件）；"
            "3 個事件中 0 個有已分析新聞，其餘為未分析或無新聞。樣本不足，無法比較。",
        )

    def test_h2_legacy_input_without_event_sentiment(self) -> None:
        legacy = _h2_events([("Positive", 0.05)] * 5 + [("Negative", -0.02)] * 5).drop(columns=["event_sentiment"])
        legacy["news_sentiment"] = ["positive"] * 5 + ["Negative"] * 5
        display_only = legacy.assign(news_sentiment=["Positive 2"] * 5 + ["Negative 1"] * 5)

        self.assertEqual(_h2(legacy)["status"], "成立")
        self.assertEqual(_h2(display_only)["status"], "資料不足")

    def test_h1_h3_unchanged(self) -> None:
        performance = pd.DataFrame({
            "keyword": ["AI", "AI", "HBM", "HBM"],
            "event_date": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]),
            "theme": "科技類",
            "event_sentiment": [UNANALYZED] * 4,
            "news_sentiment": ["無新聞"] * 4,
            "return_10d": [0.02, -0.01, 0.03, 0.04],
            "reaction_days": [1, 2, 3, 2],
        })

        h1, _, h3 = build_hypothesis_validation(performance)

        self.assertEqual(h1, {
            "title": "假說1：當搜尋熱度顯著提升時，股票較容易出現正報酬。",
            "status": "成立",
            "basis": "4 個事件中，75.0% 於 10 日內產生正報酬。",
        })
        self.assertEqual(h3, {
            "title": "假說3：不同主題存在不同市場反應速度。",
            "status": "成立",
            "basis": "科技類：2天",
        })


if __name__ == "__main__":
    unittest.main()