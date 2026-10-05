from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from trends.theme_study import (
    build_subtheme_daily_heat,
    build_theme_cross_correlations,
    correlation_confidence,
)


class ThemeLagAnalysisTests(unittest.TestCase):
    def test_subtheme_heat_aggregates_mapped_keywords(self) -> None:
        daily_heat = pd.DataFrame([
            {"date": "2025-01-01", "keyword": "ChatGPT", "heat": 60},
            {"date": "2025-01-01", "keyword": "Gemini", "heat": 40},
            {"date": "2025-01-01", "keyword": "Unmapped", "heat": 100},
        ])
        theme_mapping = pd.DataFrame([
            {"sub_theme": "AI模型", "keyword": "ChatGPT", "active": 1},
            {"sub_theme": "AI模型", "keyword": "Gemini", "active": 1},
            {"sub_theme": "停用主題", "keyword": "Unmapped", "active": 0},
        ])

        result = build_subtheme_daily_heat(daily_heat, theme_mapping)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["theme_name"], "AI模型")
        self.assertEqual(result.iloc[0]["heat"], 50)
        self.assertEqual(result.iloc[0]["keyword_count"], 2)

    def test_theme_cross_correlation_finds_leading_lag(self) -> None:
        rng = np.random.default_rng(11)
        count = 120
        dates = pd.date_range("2025-01-01", periods=count, freq="B")
        heat_values = rng.normal(size=count)
        returns = np.r_[np.zeros(3), heat_values[:-3]] * 0.002
        prices = pd.DataFrame({
            "date": dates,
            "stock_id": "3231",
            "close": 100 * np.cumprod(1 + returns),
        })
        daily_heat = pd.DataFrame([
            {"date": day, "keyword": keyword, "heat": value}
            for day, value in zip(dates, heat_values)
            for keyword in ("keyword-a", "keyword-b")
        ])
        theme_mapping = pd.DataFrame([
            {"sub_theme": "AI模型", "keyword": keyword, "stock_id": "3231", "active": 1}
            for keyword in ("keyword-a", "keyword-b")
        ])

        result = build_theme_cross_correlations(
            daily_heat, prices, theme_mapping, min_lag=-5, max_lag=5
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["theme_name"], "AI模型")
        self.assertEqual(result.iloc[0]["stock_id"], "3231")
        self.assertEqual(result.iloc[0]["best_lag"], 3)
        self.assertGreaterEqual(result.iloc[0]["sample_count"], 90)
        self.assertEqual(result.iloc[0]["confidence_grade"], "A")

    def test_confidence_grade_thresholds(self) -> None:
        self.assertIsNone(correlation_confidence(29, 0.9))
        self.assertEqual(correlation_confidence(30, 0.3), "C")
        self.assertEqual(correlation_confidence(60, -0.4), "B")
        self.assertEqual(correlation_confidence(90, 0.6), "A")


if __name__ == "__main__":
    unittest.main()
