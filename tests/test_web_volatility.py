from __future__ import annotations

import unittest

import pandas as pd

from trends.web.data import volatility_table
from trends.web.ui import volatility_level


def _prices(moves: list[float], dates: pd.DatetimeIndex) -> pd.DataFrame:
    closes = [100.0]
    for move in moves:
        closes.append(closes[-1] * (1 + move))
    return pd.DataFrame({"date": dates, "close": closes, "adj_close": closes})


class VolatilityTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = pd.bdate_range("2026-06-01", periods=81)
        self.benchmark = _prices([0.0] * 80, self.dates)

    def test_ratio_compares_last_20_days_with_the_40_before(self) -> None:
        # 60 calm days moving 1% a day, then 20 days moving 2% a day.
        stock = _prices([0.01, -0.01] * 30 + [0.02, -0.02] * 10, self.dates)
        table = volatility_table(stock, self.benchmark)
        last = table.iloc[-1]
        self.assertAlmostEqual(last["recent"], 0.02, places=3)
        self.assertAlmostEqual(last["usual"], 0.01, places=3)
        self.assertAlmostEqual(last["ratio"], 2.0, places=1)

    def test_before_and_after_exclude_day_zero(self) -> None:
        moves = [0.01, -0.01] * 30 + [0.10] + [0.03, -0.03] * 2 + [0.03] + [0.01, -0.01] * 7
        table = volatility_table(_prices(moves, self.dates), self.benchmark)
        day0 = self.dates[61]                      # the +10% day
        self.assertAlmostEqual(table.at[day0, "before"], 1.0, places=2)
        self.assertAlmostEqual(table.at[day0, "after5"], 3.0, places=1)

    def test_uses_benchmark_trading_days_only(self) -> None:
        stock = _prices([0.01, -0.01] * 40, self.dates)
        extra = pd.DataFrame({"date": [pd.Timestamp("2026-06-06")], "close": [500.0], "adj_close": [500.0]})  # a Saturday
        table = volatility_table(pd.concat([stock, extra]), self.benchmark)
        self.assertNotIn(pd.Timestamp("2026-06-06"), table.index)
        self.assertLess(table["move"].max(), 0.011)

    def test_levels(self) -> None:
        self.assertEqual(volatility_level(1.6)[0], "明顯比平常動盪")
        self.assertEqual(volatility_level(1.0)[0], "和平常差不多")
        self.assertEqual(volatility_level(0.5)[0], "比平常平靜")


if __name__ == "__main__":
    unittest.main()
