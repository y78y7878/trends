"""Does a trending keyword move its stocks? Direct links vs theme baskets, against 0050.

Re-run monthly as data accumulates (first run 2026-11-03: no post-event effect, see
docs/hypothesis_study.md). Read-only on data.db. Usage::

    .venv\\Scripts\\python.exe tools\\hypothesis_study.py              # after the trend is visible
    .venv\\Scripts\\python.exe tools\\hypothesis_study.py --mode same  # includes the event day's move

Method
- Event: the first Asia/Taipei day a keyword (its canonical form) is on the RSS list after at
  least 3 days off it.
- Day 0 = first trading day on/after the event date. ``after`` (default) measures from day 0's
  close, the first price at which the trend is visible; ``same`` from the close before day 0,
  which also contains the move that often *caused* the search.
- AR_N = stock return over N trading days minus 0050's (adj_close). Taiwan non-ETF stocks only.
- Units: direct = one (stock, day); theme = one (theme, day) = mean AR of its curated basket, so
  several industry words of one theme on one day count once.
- Baseline: the same stocks/baskets' average AR_N from *every* trading day of the period. The
  event effect is the excess over it; without this, a basket that simply beat 0050 during the
  period looks like an event effect.
- p-values use the normal approximation to the t statistic (fine for n >= 30).
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parents[1] / "data.db"
WINDOWS = (1, 3, 5, 10)
QUIET_DAYS = 3
BENCHMARK = "0050"


def ttest(values: pd.Series) -> tuple[float, float]:
    x = values.dropna().to_numpy(dtype=float)
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan"), float("nan")
    t = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    return t, math.erfc(abs(t) / math.sqrt(2))


def load(start: str) -> dict[str, pd.DataFrame]:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        query = lambda sql, *params: pd.read_sql(sql, connection, params=params)
        return {
            "trends": query("SELECT trend_id, keyword, approx_traffic, published_at FROM google_trends WHERE published_at >= ?", start),
            "classes": query("SELECT keyword, canonical_keyword FROM keyword_classification"),
            "links": query("SELECT theme_name, keyword, stock_id, link_type FROM theme_mapping WHERE active = 1 AND stock_id IS NOT NULL"),
            "news": query("SELECT trend_id, news_sentiment FROM google_trends_news WHERE news_sentiment IS NOT NULL"),
            "prices": query("SELECT date, stock_id, adj_close FROM stocks WHERE date >= date(?, '-60 days')", start),
        }
    finally:
        connection.close()


def build_events(trends: pd.DataFrame, classes: pd.DataFrame, news: pd.DataFrame) -> pd.DataFrame:
    trends = trends.merge(classes, on="keyword", how="left")
    trends["canonical"] = trends["canonical_keyword"].fillna(trends["keyword"])
    trends["date"] = (pd.to_datetime(trends["published_at"]) + pd.Timedelta(hours=8)).dt.normalize()
    trends["traffic"] = trends["approx_traffic"].astype(str).str.replace(",", "").str.extract(r"(\d+)")[0].astype(float)
    tone = news.groupby("trend_id")["news_sentiment"].agg(lambda s: (s == "Positive").sum() - (s == "Negative").sum())
    trends["tone"] = trends["trend_id"].map(tone).fillna(0)
    days = trends.groupby(["canonical", "date"]).agg(traffic=("traffic", "max"), tone=("tone", "sum")).reset_index()
    events = []
    for keyword, group in days.groupby("canonical"):
        previous = None
        for row in group.sort_values("date").itertuples():
            if previous is None or (row.date - previous).days > QUIET_DAYS:
                events.append({"keyword": keyword, "date": row.date, "traffic": row.traffic, "tone": row.tone})
            previous = row.date
    return pd.DataFrame(events)


def forward_ar(wide: pd.DataFrame, mode: str) -> dict[int, pd.DataFrame]:
    """AR_N indexed by the day-0 date: excess return from the base close to N trading days on."""
    market = wide[BENCHMARK]
    offset = 1 if mode == "same" else 0      # base = close before day 0, or day 0's close
    out = {}
    for n in WINDOWS:
        stock = wide.shift(offset - n) / wide.shift(offset) - 1
        bench = market.shift(offset - n) / market.shift(offset) - 1
        out[n] = stock.sub(bench, axis=0)
    return out


def summarize(label: str, frame: pd.DataFrame, baseline: dict[int, pd.Series]) -> None:
    print(f"\n== {label} (events {len(frame)}) ==")
    print(f"  {'':>6} {'event':>8} {'normal':>8} {'excess':>8} {'t':>6} {'p':>6} {'up':>5} {'n':>5}")
    for n in WINDOWS:
        column = f"ar{n}"
        excess = (frame[column] - baseline[n]).dropna()
        t, p = ttest(excess)
        print(f"  {column:>6} {frame[column].mean():+8.2%} {baseline[n].dropna().mean():+8.2%} {excess.mean():+8.2%} "
              f"{t:+6.2f} {p:6.3f} {np.mean(frame[column].dropna() > 0):5.0%} {len(excess):5d}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("after", "same"), default="after")
    parser.add_argument("--start", default="2026-08-21", help="research start (Dashboard RESEARCH_START)")
    args = parser.parse_args()

    data = load(args.start)
    events = build_events(data["trends"], data["classes"], data["news"])
    links = data["links"]
    links = links[links["stock_id"].str.fullmatch(r"\d{4,6}[A-Z]?") & ~links["stock_id"].str.startswith("00")]
    wide = data["prices"].pivot(index="date", columns="stock_id", values="adj_close").sort_index()
    wide.index = pd.to_datetime(wide.index)
    ar = forward_ar(wide, args.mode)
    trading_days = wide.index
    period = trading_days[trading_days >= pd.Timestamp(args.start)]

    pairs = events.merge(links, on="keyword")
    pairs["day0"] = [trading_days[trading_days.searchsorted(d)] if trading_days.searchsorted(d) < len(trading_days) else pd.NaT
                     for d in pairs["date"]]
    pairs = pairs.dropna(subset=["day0"])
    pairs = pairs[pairs["stock_id"].isin(wide.columns)]
    for n in WINDOWS:
        pairs[f"ar{n}"] = [ar[n].at[d, s] for d, s in zip(pairs["day0"], pairs["stock_id"])]

    # Direct: baseline per stock = its mean AR_N over all period days.
    direct = pairs[pairs["link_type"] == "direct"].drop_duplicates(["stock_id", "day0"]).copy()
    direct_base = {n: direct["stock_id"].map(ar[n].loc[period].mean()) for n in WINDOWS}

    # Theme: one observation per (theme, day0) basket; baseline = the basket's mean AR_N.
    theme_pairs = pairs[pairs["link_type"] == "theme"].drop_duplicates(["theme_name", "day0", "stock_id"])
    cols = [f"ar{n}" for n in WINDOWS]
    theme = theme_pairs.groupby(["theme_name", "day0"]).agg(
        **{c: (c, "mean") for c in cols}, tone=("tone", "sum"), traffic=("traffic", "max"),
    ).reset_index()
    baskets = theme_pairs.groupby("theme_name")["stock_id"].unique()
    basket_base = {n: {t: ar[n].loc[period, list(stocks)].mean(axis=1).mean() for t, stocks in baskets.items()} for n in WINDOWS}
    theme_base = {n: theme["theme_name"].map(basket_base[n]) for n in WINDOWS}

    print(f"data.db: {DB}\nmode: {args.mode}   period: {args.start} .. {wide.index.max():%Y-%m-%d}   "
          f"events: {len(events)} keyword-days")
    summarize("DIRECT: the company itself trends", direct, direct_base)
    summarize("THEME: an industry word trends -> its curated basket (one per theme-day)", theme, theme_base)

    daily = ar[1].loc[period, sorted(direct["stock_id"].unique())].abs().stack()
    print(f"\n== Attention: |AR1| on event days {direct['ar1'].abs().mean():.2%} vs all days {daily.mean():.2%} ==")
    for label, frame, base in (("DIRECT", direct, direct_base), ("THEME", theme, theme_base)):
        print(f"\n-- {label} by news tone (positive minus negative headlines), excess AR5 / AR10 --")
        for name, mask in (("positive", frame["tone"] > 0), ("neutral", frame["tone"] == 0), ("negative", frame["tone"] < 0)):
            if mask.sum() >= 3:
                e5, e10 = (frame["ar5"] - base[5])[mask], (frame["ar10"] - base[10])[mask]
                print(f"  {name:>8} n={mask.sum():4d}  {e5.mean():+.2%} (t={ttest(e5)[0]:+.2f})  {e10.mean():+.2%} (t={ttest(e10)[0]:+.2f})")


if __name__ == "__main__":
    main()
