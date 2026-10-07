from __future__ import annotations

import pandas as pd


RETURN_DAYS = (1, 3, 5, 10)


def add_price_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Add past/future close returns and volume change, grouped by stock."""
    if prices.empty:
        return prices.copy()

    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values(["stock_id", "date"]).reset_index(drop=True)
    grouped_close = frame.groupby("stock_id", sort=False)["close"]
    for days in RETURN_DAYS:
        frame[f"return_{days}d"] = grouped_close.transform(lambda series: series / series.shift(days) - 1)
        frame[f"future_return_{days}d"] = grouped_close.transform(lambda series: series.shift(-days) / series - 1)
    frame["volume_change"] = frame.groupby("stock_id", sort=False)["volume"].transform(
        lambda series: series.pct_change(fill_method=None)
    )
    return frame.replace([float("inf"), float("-inf")], pd.NA)