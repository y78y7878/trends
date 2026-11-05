"""Does attention predict volatility beyond what the price already shows?

Volatility clusters: a stock that moved a lot today tends to keep moving, attention or not. So
an attention event is only useful if it predicts *extra* volatility after controlling for
today's move and recent volatility. Two checks, read-only on data.db and ptt_stock.db:

1. Matched comparison: event days vs days without any attention event (none within ±5 trading
   days) whose same-day move was of similar size (same decile of |AR| / the stock's normal).
2. Regression on every stock-day: future volatility ~ today's |AR| + past 20-day volatility +
   RSS + PTT + pytrends dummies, standard errors clustered by date (stocks move together).

Volatility = |abnormal return| (stock minus 0050), in multiples of the stock's normal level (its
average |AR| over the period), so a value of 1.2 means 20% more than usual for that stock.
Signals use only what is known by the close of day t (pytrends' day-t value is known on t+1, so
its event is moved one day later). Usage::

    .venv\\Scripts\\python.exe tools\\volatility_study.py
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from ptt_pilot_study import START, attention_signals, load_frames, price_table  # noqa: E402
from trends.research import BENCHMARK  # noqa: E402

PROFILE = range(-5, 21)
BIG_MOVE = 2.0           # a "big day": |AR| at least twice the stock's usual level
SIGNALS = {"rss": "RSS 熱搜", "ptt": "PTT 討論爆量", "heat": "pytrends 熱度上升（延後一天）"}


def event_matrix(events: pd.DataFrame, days: pd.DatetimeIndex, stocks: list[str], lag: int = 0) -> pd.DataFrame:
    """Boolean trading-day x stock matrix: True on the trading day an event becomes usable."""
    matrix = pd.DataFrame(False, index=days, columns=stocks)
    for row in events.itertuples():
        if row.stock_id not in matrix:
            continue
        position = days.searchsorted(pd.Timestamp(row.date)) + lag
        if position < len(days):
            matrix.iat[position, matrix.columns.get_loc(row.stock_id)] = True
    return matrix


def clustered_ols(y: np.ndarray, x: np.ndarray, groups: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """OLS with standard errors clustered by ``groups`` (here: trading date)."""
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ x.T @ y
    residual = y - x @ beta
    meat = np.zeros((x.shape[1], x.shape[1]))
    for group in np.unique(groups):
        mask = groups == group
        score = x[mask].T @ residual[mask]
        meat += np.outer(score, score)
    n, k, g = len(y), x.shape[1], len(np.unique(groups))
    correction = g / (g - 1) * (n - 1) / (n - k)
    return beta, np.sqrt(np.diag(xtx_inv @ meat @ xtx_inv) * correction)


def main() -> None:
    frames = load_frames()
    # The usual level needs ~60 trading days of history before the first event; load_frames
    # only keeps 60 calendar days, so read prices from further back.
    with sqlite3.connect(f"file:{(TOOLS.parent / 'data.db').as_posix()}?mode=ro", uri=True) as connection:
        frames["prices"] = pd.read_sql(
            "SELECT date, stock_id, adj_close FROM stocks WHERE date >= date(?, '-150 days')", connection, params=(START,))
    wide = price_table(frames["prices"])
    ar = wide.pct_change(fill_method=None).sub(wide[BENCHMARK].pct_change(fill_method=None), axis=0)
    signals = attention_signals(frames, wide, verbose=False)
    stocks = signals["universe"]
    abs_ar = ar[stocks].abs()
    days = abs_ar.index
    period = days >= pd.Timestamp(START)
    # Usual level = the stock's mean |AR| over t-60..t-21: history only, and not overlapping the
    # 20-day "recent volatility" control, so neither side borrows from the future.
    usual = abs_ar.shift(21).rolling(40, min_periods=25).mean()
    rel = abs_ar / usual                                         # multiples of the stock's usual level
    normal = abs_ar[period].mean()

    events = {
        "rss": event_matrix(signals["rss"], days, stocks),
        "ptt": event_matrix(signals["ptt"], days, stocks),
        "heat": event_matrix(signals["heat"], days, stocks, lag=1),
    }
    any_event = sum(m.astype(int) for m in events.values()) > 0
    near_event = any_event.rolling(11, center=True, min_periods=1).max().astype(bool)

    # Stock-day panel.
    future5 = rel.shift(-1).rolling(5).mean().shift(-4)          # mean of t+1..t+5
    future10 = rel.shift(-6).rolling(5).mean().shift(-4)         # mean of t+6..t+10
    past20 = rel.shift(1).rolling(20, min_periods=15).mean()     # mean of t-20..t-1
    big5 = (rel.shift(-1).rolling(5).max().shift(-4) >= BIG_MOVE)
    panel = pd.DataFrame({
        "today": rel.stack(), "past20": past20.stack(), "future5": future5.stack(), "future10": future10.stack(),
        "big5": big5.stack(), "near": near_event.stack(),
        **{name: matrix.stack() for name, matrix in events.items()},
    }).reset_index(names=["date", "stock_id"])
    panel = panel[(panel["date"] >= pd.Timestamp(START))].dropna(subset=["today", "past20", "future5", "future10"])
    panel["bucket"] = pd.qcut(panel["today"], 10, labels=False, duplicates="drop")
    print(f"stock-days: {len(panel):,} ({panel['stock_id'].nunique()} stocks, {panel['date'].nunique()} trading days, "
          f"{panel['date'].min():%Y-%m-%d} .. {panel['date'].max():%Y-%m-%d})")
    print(f"normal daily |AR| across stocks: median {normal.median():.2%}")

    print("\n== 1. 配對比較：事件日 vs 當天漲跌幅相近、但前後 5 天都沒有事件的日子 ==")
    print("   （數值 = 平常波動的倍數；差 = 事件減對照，t 以每個事件與其對照組的差計算）")
    controls = panel[~panel["near"]].groupby("bucket")[["today", "future5", "future10", "big5"]].mean()
    for name, label in SIGNALS.items():
        hits = panel[panel[name]].join(controls, on="bucket", rsuffix="_ctrl")
        if hits.empty:
            continue
        line = [f"  {label:<26} n={len(hits):4d}  當天 {hits['today'].mean():.2f}x（對照 {hits['today_ctrl'].mean():.2f}x）"]
        for column, text in (("future5", "後 1-5 日"), ("future10", "後 6-10 日")):
            diff = hits[column] - hits[f"{column}_ctrl"]
            t = diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff)))
            line.append(f"{text} {hits[column].mean():.2f}x vs {hits[f'{column}_ctrl'].mean():.2f}x（差 {diff.mean():+.2f}, t={t:+.1f}）")
        line.append(f"5 日內有單日波動 ≥ 平常 2 倍 {hits['big5'].mean():.0%} vs {hits['big5_ctrl'].mean():.0%}")
        print("  ".join(line))

    print("\n== 2. 迴歸：未來波動 ~ 今天波動 + 過去 20 日波動 + 三種事件（標準誤依日期分群）==")
    x_cols = ["today", "past20", "rss", "ptt", "heat"]
    x = np.column_stack([np.ones(len(panel))] + [panel[c].astype(float).to_numpy() for c in x_cols])
    groups = panel["date"].to_numpy()
    for target, text in (("future5", "後 1-5 日"), ("future10", "後 6-10 日")):
        beta, se = clustered_ols(panel[target].to_numpy(dtype=float), x, groups)
        terms = "  ".join(f"{name} {b:+.3f} (t={b / s:+.1f})" for name, b, s in zip(["常數", *x_cols], beta, se))
        print(f"  {text}: {terms}")
    print("   （平常水準 = 該股 t-60 至 t-21 日的平均 |AR|，只用過去資料）\n   今天波動、過去波動的係數 = 每多 1 倍平常波動，未來波動多幾倍；事件係數 = 控制前兩者後，事件額外多出的波動倍數")

    print("\n== 3. 波動多久才回到平常？（事件日前後的平均波動倍數；對照 = 當天漲跌幅相近、無事件的日子）==")
    bucket_of = panel.set_index(["date", "stock_id"])["bucket"]
    rel_values = rel.to_numpy()
    col = {s: i for i, s in enumerate(stocks)}
    control_days = panel[~panel["near"]]
    for name, label in SIGNALS.items():
        rows = []
        hits = panel[panel[name]]
        for row in hits.itertuples():
            p = days.get_loc(row.date)
            if p + max(PROFILE) >= len(days) or p + min(PROFILE) < 0:
                continue
            rows.append(rel_values[p + min(PROFILE):p + max(PROFILE) + 1, col[row.stock_id]])
        # Control profile: per same-day-move bucket, the average path of up to 400 event-free
        # days, weighted by how many events fall in each bucket.
        weights = hits["bucket"].value_counts()
        ctrl_profile = np.zeros(len(PROFILE))
        for bucket, weight in weights.items():
            pool = control_days[control_days["bucket"].eq(bucket)]
            pool = pool.sample(min(400, len(pool)), random_state=0)
            paths = []
            for row in pool.itertuples():
                p = days.get_loc(row.date)
                if p + max(PROFILE) < len(days) and p + min(PROFILE) >= 0:
                    paths.append(rel_values[p + min(PROFILE):p + max(PROFILE) + 1, col[row.stock_id]])
            if paths:
                ctrl_profile += weight / weights.sum() * np.nanmean(np.array(paths), axis=0)
        if not rows:
            continue
        mean = np.nanmean(np.array(rows), axis=0)
        ctrl = ctrl_profile
        shown = [o for o in PROFILE if o in (-5, -1, 0, 1, 2, 3, 5, 10, 15, 20)]
        print(f"  {label}（{len(rows)} 個）")
        print("    事件: " + "  ".join(f"{o:+d}:{mean[o - min(PROFILE)]:.2f}" for o in shown))
        print("    對照: " + "  ".join(f"{o:+d}:{ctrl[o - min(PROFILE)]:.2f}" for o in shown))


if __name__ == "__main__":
    main()
