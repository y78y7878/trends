from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from trends.database import ThemeMapping, get_engine, init_db
from trends.stock_collector import DEFAULT_STOCKS, download_prices, stock_pool, to_yfinance_ticker


class FakeTicker:
    def __init__(self, ticker: str, available: set[str], requested: list[str]) -> None:
        self.ticker, self.available = ticker, available
        requested.append(ticker)

    def history(self, **_options) -> pd.DataFrame:
        if self.ticker not in self.available:
            return pd.DataFrame()
        index = pd.DatetimeIndex(["2026-10-30"], name="Date")
        return pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [10]}, index=index)


class StockCollectorTests(unittest.TestCase):
    def test_taiwan_codes_including_letter_suffixed_etfs_use_twse_suffix(self) -> None:
        self.assertEqual(to_yfinance_ticker("2330"), "2330.TW")
        self.assertEqual(to_yfinance_ticker("00631l"), "00631L.TW")
        self.assertEqual(to_yfinance_ticker("NVDA"), "NVDA")

    def test_otc_code_falls_back_to_tpex(self) -> None:
        requested: list[str] = []
        with patch("trends.stock_collector.yf.Ticker", lambda ticker: FakeTicker(ticker, {"00953B.TWO"}, requested)):
            prices = download_prices("00953B")

        self.assertEqual(requested, ["00953B.TW", "00953B.TWO"])
        self.assertEqual(prices["stock_id"].tolist(), ["00953B"])

    def test_pool_adds_stocks_of_active_links_only(self) -> None:
        with TemporaryDirectory() as directory:
            engine = init_db(get_engine(Path(directory) / "stocks.db"))
            with engine.begin() as connection:
                connection.execute(ThemeMapping.__table__.insert(), [
                    {"theme_name": "其他", "keyword": "華航", "stock_id": "2610", "category": "其他", "active": 1},
                    {"theme_name": "科技類", "keyword": "Uber", "stock_id": "9999", "category": "科技類", "active": 0},
                ])
            pool = stock_pool(engine)
            engine.dispose()

        self.assertIn("2610", pool)
        self.assertNotIn("9999", pool)
        self.assertTrue(set(DEFAULT_STOCKS) <= set(pool))


if __name__ == "__main__":
    unittest.main()
