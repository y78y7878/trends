from __future__ import annotations

import unittest

import pandas as pd

from trends.event_study import (
    build_event_research_summary,
    build_event_research_text,
    calculate_event_performance,
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


if __name__ == "__main__":
    unittest.main()