"""Event study shared by tools/hypothesis_study.py and the Dashboard's findings page.

Event: the first Asia/Taipei day a keyword (its canonical form) is on the RSS list after at least
``QUIET_DAYS`` days off it. Day 0 = first trading day on/after the event date. Abnormal return
(AR) = stock return minus 0050's (adj_close), Taiwan non-ETF stocks only. Units: direct = one
(stock, day 0); theme = one (theme, day 0) = mean over the theme's curated basket. Baseline = the
same stock's / basket's average AR over every trading day of the period, so a stock that simply
beat 0050 during the period is not read as an event effect.
"""
from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from trends.database import DB_PATH

QUIET_DAYS = 3
BENCHMARK = "0050"
RESEARCH_START = "2026-08-21"
PATH_OFFSETS = range(-5, 11)


def load_frames(db_path: Path = DB_PATH, start: str = RESEARCH_START) -> dict[str, pd.DataFrame]:
    connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
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
    """One row per (canonical keyword, event date) with the day's max RSS traffic and news tone."""
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
    return pd.DataFrame(events, columns=["keyword", "date", "traffic", "tone"])


def price_table(prices: pd.DataFrame) -> pd.DataFrame:
    wide = prices.pivot(index="date", columns="stock_id", values="adj_close").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide


def taiwan_links(links: pd.DataFrame) -> pd.DataFrame:
    stock = links["stock_id"].astype(str)
    return links[stock.str.fullmatch(r"\d{4,6}[A-Z]?") & ~stock.str.startswith("00")]


def event_units(events: pd.DataFrame, links: pd.DataFrame, wide: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Event units with their day-0 position: direct (stock, day0) and theme (theme, day0, basket)."""
    days = wide.index
    pairs = events.merge(taiwan_links(links), on="keyword")
    positions = days.searchsorted(pairs["date"])
    pairs = pairs.assign(day0_pos=positions)[positions < len(days)]
    pairs = pairs[pairs["stock_id"].isin(wide.columns)]
    pairs["day0"] = days[pairs["day0_pos"]]
    direct = pairs[pairs["link_type"] == "direct"].drop_duplicates(["stock_id", "day0"])
    theme = (pairs[pairs["link_type"] == "theme"]
             .drop_duplicates(["theme_name", "day0", "stock_id"])
             .groupby(["theme_name", "day0", "day0_pos"])
             .agg(stocks=("stock_id", lambda s: sorted(set(s))), keywords=("keyword", lambda s: sorted(set(s))),
                  tone=("tone", "sum"), traffic=("traffic", "max"))
             .reset_index())
    return {"direct": direct, "theme": theme}


def cumulative_paths(units: dict[str, pd.DataFrame], wide: pd.DataFrame, start: str = RESEARCH_START) -> pd.DataFrame:
    """Average cumulative AR from the close before day 0 (offset -1 = 0%) for offsets -5..10.

    ``car`` is the raw abnormal path, ``car_adjusted`` subtracts the stock's/basket's normal daily
    AR times the number of days, ``se`` is the standard error of ``car_adjusted`` across events.
    """
    daily = wide.pct_change(fill_method=None).sub(wide[BENCHMARK].pct_change(fill_method=None), axis=0)
    period_mean = daily[daily.index >= pd.Timestamp(start)].mean()
    values = daily.to_numpy()
    columns = {stock: i for i, stock in enumerate(daily.columns)}
    rows = []
    for kind, frame in units.items():
        paths, normals = [], []
        for row in frame.itertuples():
            stocks = [row.stock_id] if kind == "direct" else row.stocks
            idx = [columns[s] for s in stocks]
            lo, hi = row.day0_pos + min(PATH_OFFSETS), row.day0_pos + max(PATH_OFFSETS)
            if lo < 1 or hi >= len(values):
                continue
            with warnings.catch_warnings():                                    # all-NaN days (holidays) stay NaN
                warnings.simplefilter("ignore", RuntimeWarning)
                window = np.nanmean(values[lo:hi + 1, idx], axis=1)          # daily AR, offsets -5..10
            paths.append(window)
            normals.append(float(np.nanmean(period_mean.iloc[idx])))
        if not paths:
            continue
        paths, normals = np.array(paths), np.array(normals)
        zero = -min(PATH_OFFSETS)                                            # index of offset 0
        for i, offset in enumerate(PATH_OFFSETS):
            if offset >= 0:
                segment = paths[:, zero:i + 1]
                days_in = i - zero + 1
            else:
                segment = -paths[:, i + 1:zero]                                # before day 0: walk backwards
                days_in = -(zero - i - 1)
            car = np.nansum(segment, axis=1)
            adjusted = car - normals * days_in
            rows.append({
                "kind": kind, "offset": offset, "car": float(np.nanmean(car)),
                "car_adjusted": float(np.nanmean(adjusted)),
                "se": float(np.nanstd(adjusted, ddof=1) / np.sqrt(np.sum(~np.isnan(adjusted)))) if len(adjusted) > 1 else np.nan,
                "events": int(len(paths)),
            })
    return pd.DataFrame(rows)


def study(db_path: Path = DB_PATH, start: str = RESEARCH_START) -> dict[str, object]:
    frames = load_frames(db_path, start)
    events = build_events(frames["trends"], frames["classes"], frames["news"])
    wide = price_table(frames["prices"])
    units = event_units(events, frames["links"], wide)
    return {
        "events": events, "wide": wide, "units": units,
        "paths": cumulative_paths(units, wide, start),
        "period": (start, wide.index.max()),
    }
