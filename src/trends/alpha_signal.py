from __future__ import annotations

import re

import numpy as np
import pandas as pd


def parse_traffic(value: object) -> float:
    match = re.search(r"([\d,.]+)\s*([KMB]?)", str(value).upper().replace(",", ""))
    if not match:
        return 0.0
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return float(match.group(1)) * multiplier


def score_signals(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events.assign(signal_score=pd.Series(dtype=float), signal=pd.Series(dtype=str))

    signals = events.copy()
    traffic = signals["approx_traffic"].map(parse_traffic)
    heat = traffic.rank(pct=True).fillna(0) * 100
    volume = pd.to_numeric(signals["volume_change"], errors="coerce").fillna(0).clip(-1, 3)
    volume_score = ((volume + 1) / 4 * 100).clip(0, 100)
    sentiment = signals["news_sentiment"].map({"Positive": 100, "Neutral": 50, "Negative": 0}).fillna(50)
    signals["event_date"] = pd.to_datetime(signals["event_date"], errors="coerce")
    signals["historical_win_rate"] = 50.0
    ordered = signals.sort_values("event_date")
    for _, group in ordered.groupby("stock_id", sort=False):
        returns = pd.to_numeric(group["future_return_5d"], errors="coerce")
        prior_rates = returns.gt(0).where(returns.notna()).shift(1).expanding(min_periods=1).mean().fillna(0.5) * 100
        signals.loc[group.index, "historical_win_rate"] = prior_rates.to_numpy()
    historical = signals["historical_win_rate"]
    signals["signal_score"] = (heat * 0.35 + volume_score * 0.25 + sentiment * 0.15 + historical * 0.25).round(1)
    signals["signal"] = pd.cut(
        signals["signal_score"],
        bins=[-np.inf, 40, 70, np.inf],
        labels=["🟢 可觀察", "🟡 已反應", "🔴 過熱"],
        right=False,
    ).astype(str)
    return signals.sort_values("signal_score", ascending=False)