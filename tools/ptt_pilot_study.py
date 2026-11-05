"""Pilot: does PTT Stock board discussion move before prices, unlike Google Trends RSS?

Reads ptt_stock.db (from tools/ptt_backfill.py) and data.db, read-only. Compares three
"attention" signals with the same abnormal-return method as research.py (minus 0050, minus the
stock's normal AR, Taiwan non-ETF stocks):

- rss:  first day a company keyword is on the Google Trends RSS list after 3 quiet days.
- ptt:  first day a stock's PTT mentions (titles + daily chat-thread comments) reach
        max(MIN_MENTIONS, SPIKE x its prior 20-day average), after 3 quiet days.
- heat: first day a company keyword's pytrends interest is >= 1.5x its prior 7-day mean
        (a stage before the RSS list; the daily value is only known the next day).

Per event: pre = AR over the 5 trading days before day 0, day0 = AR on day 0, post = AR from
day 0's close to day 5 / day 10 (what a trader who saw the signal by the close could get).
Also checks [標的] posts' 多/空 calls from the first close after posting, and whether PTT spikes
come before or after RSS events. Usage::

    .venv\\Scripts\\python.exe tools\\ptt_pilot_study.py
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trends.entity_resolution_etl import AMBIGUOUS_COMPANY_WORDS, YEAR_LIKE  # noqa: E402
from trends.research import BENCHMARK, build_events, load_frames, price_table, taiwan_links  # noqa: E402

START = "2026-08-21"
QUIET_DAYS = 3
MIN_MENTIONS = 5
SPIKE = 3.0
TARGET = re.compile(r"^\[標的\]\s*(\d{4,6})")


def ttest(values) -> tuple[float, float, int]:
    x = pd.Series(values, dtype=float).dropna().to_numpy()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan"), float("nan"), len(x)
    t = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    return t, math.erfc(abs(t) / math.sqrt(2)), len(x)


def taipei(utc: pd.Series) -> pd.Series:
    return pd.to_datetime(utc) + pd.Timedelta(hours=8)


# ---------- stock name matching ----------

def build_matcher(stocks: list[str], links: pd.DataFrame) -> tuple[re.Pattern, dict[str, str]]:
    listed = json.loads((ROOT / "data.listed_companies.json").read_text(encoding="utf-8"))
    alias: dict[str, str] = {}
    direct = links[links["link_type"].eq("direct")]
    for stock in stocks:
        names = {listed.get(stock, "")} | set(direct.loc[direct["stock_id"].eq(stock), "keyword"])
        for name in names:
            name = re.sub(r"(股份有限公司|-KY)$", "", str(name)).strip()
            if len(name) >= 2 and name not in AMBIGUOUS_COMPANY_WORDS and not name.isdigit():
                alias.setdefault(name, stock)
        if not YEAR_LIKE.fullmatch(stock):
            alias[stock] = stock
    names = sorted(alias, key=len, reverse=True)
    pattern = re.compile("|".join(
        rf"(?<!\d){re.escape(n)}(?!\d)" if n.isdigit() else re.escape(n) for n in names
    ))
    return pattern, alias


def mentions(texts: pd.DataFrame, pattern: re.Pattern, alias: dict[str, str]) -> pd.DataFrame:
    """One row per (stock, text) mention; a text naming a stock twice counts once."""
    rows = []
    for when, text in zip(texts["when"], texts["text"]):
        found = {alias[m.group(0)] for m in pattern.finditer(str(text))}
        rows.extend((when, stock) for stock in found)
    return pd.DataFrame(rows, columns=["when", "stock_id"])


def spike_events(daily: pd.DataFrame) -> pd.DataFrame:
    """daily: stock_id x calendar date counts -> first spike days after QUIET_DAYS quiet days."""
    events = []
    for stock, series in daily.items():
        prior = series.shift(1).rolling(20, min_periods=10).mean()
        hits = series[(series >= MIN_MENTIONS) & (series >= SPIKE * prior)]
        last = None
        for day in hits.index:
            if last is None or (day - last).days > QUIET_DAYS:
                events.append({"stock_id": stock, "date": day, "count": int(series[day])})
            last = day
    return pd.DataFrame(events, columns=["stock_id", "date", "count"])


# ---------- abnormal returns per event ----------

def components(events: pd.DataFrame, daily_ar: pd.DataFrame, normal: pd.Series) -> pd.DataFrame:
    days = daily_ar.index
    values, columns = daily_ar.to_numpy(), {s: i for i, s in enumerate(daily_ar.columns)}
    rows = []
    for event in events.itertuples():
        if event.stock_id not in columns:
            continue
        p = days.searchsorted(pd.Timestamp(event.date))
        if p < 5 or p + 10 >= len(days):
            continue
        ar = values[:, columns[event.stock_id]] - normal[event.stock_id]
        rows.append({
            "key": event.Index, "stock_id": event.stock_id, "day0": days[p],
            "pre": np.nansum(ar[p - 5:p]), "day0_ar": ar[p],
            "post5": np.nansum(ar[p + 1:p + 6]), "post10": np.nansum(ar[p + 1:p + 11]),
        })
    return pd.DataFrame(rows)


def report(name: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        print(f"  {name:<34} no events")
        return
    parts = []
    for column, label in (("pre", "前5日"), ("day0_ar", "當天"), ("post5", "後5日"), ("post10", "後10日")):
        t, p, n = ttest(frame[column])
        parts.append(f"{label} {frame[column].mean():+6.2%} (t={t:+5.2f})")
    print(f"  {name:<34} n={len(frame):4d}  " + "  ".join(parts))


def main() -> None:
    frames = load_frames()
    wide = price_table(frames["prices"])
    daily_ar = wide.pct_change(fill_method=None).sub(wide[BENCHMARK].pct_change(fill_method=None), axis=0)
    period = daily_ar[daily_ar.index >= pd.Timestamp(START)]
    normal = period.mean()
    links = taiwan_links(frames["links"])
    universe = sorted(set(links["stock_id"]) & set(wide.columns))
    direct = links[links["link_type"].eq("direct")]

    ptt = sqlite3.connect(f"file:{(ROOT / 'ptt_stock.db').as_posix()}?mode=ro", uri=True)
    posts = pd.read_sql("SELECT post_id, posted_at, category, title, push_score FROM ptt_posts WHERE posted_at >= ?", ptt,
                        params=(f"{START} 00:00:00",))
    comments = pd.read_sql("SELECT commented_at, text FROM ptt_comments WHERE commented_at IS NOT NULL", ptt)
    print(f"PTT: {len(posts):,} posts, {len(comments):,} chat-thread comments; universe {len(universe)} stocks")

    pattern, alias = build_matcher(universe, frames["links"])
    texts = pd.concat([
        pd.DataFrame({"when": taipei(posts["posted_at"]), "text": posts["title"]}),
        pd.DataFrame({"when": taipei(comments["commented_at"]), "text": comments["text"]}),
    ])
    found = mentions(texts, pattern, alias)
    found["date"] = found["when"].dt.normalize()
    print(f"mentions: {len(found):,} across {found['stock_id'].nunique()} stocks; "
          f"top: {found['stock_id'].value_counts().head(6).to_dict()}")
    calendar = pd.date_range(START, found["date"].max(), freq="D")
    daily = found.groupby(["date", "stock_id"]).size().unstack(fill_value=0).reindex(calendar, fill_value=0)

    # Signals.
    ptt_events = spike_events(daily)
    rss = build_events(frames["trends"], frames["classes"], frames["news"]).merge(direct[["keyword", "stock_id"]], on="keyword")
    rss = rss.drop_duplicates(["stock_id", "date"])
    history = pd.read_sql("SELECT keyword, trend_date, trend_score FROM google_trends_history",
                          sqlite3.connect(f"file:{(ROOT / 'data.db').as_posix()}?mode=ro", uri=True))
    history = history.merge(direct[["keyword", "stock_id"]].drop_duplicates(), on="keyword")
    heat_rows = []
    for (keyword, stock), group in history.groupby(["keyword", "stock_id"]):
        series = group.assign(trend_date=pd.to_datetime(group["trend_date"])).set_index("trend_date")["trend_score"].sort_index()
        prior = series.shift(1).rolling(7, min_periods=3).mean()
        hits = series[(series >= 1.5 * prior) & (prior > 0) & (series.index >= pd.Timestamp(START))]
        last = None
        for day in hits.index:
            if last is None or (day - last).days > QUIET_DAYS:
                heat_rows.append({"stock_id": stock, "date": day})
            last = day
    heat = pd.DataFrame(heat_rows).drop_duplicates(["stock_id", "date"])

    print("\n== 三種訊號：事件前後的超額報酬（扣 0050、扣平常水準；後 N 日從事件當天收盤起算）==")
    results = {}
    for name, events in (("RSS 熱搜（公司名稱）", rss), ("PTT 討論爆量", ptt_events), ("pytrends 熱度開始上升", heat)):
        results[name] = components(events, daily_ar, normal)
        report(name, results[name])
    heat_next = components(heat.assign(date=heat["date"] + pd.Timedelta(days=1)), daily_ar, normal)
    report("pytrends（隔天才知道 → 延後一天）", heat_next)

    print("\n== PTT 討論和 RSS 熱搜誰先？（同一檔股票，RSS 事件前後 7 天內最近的 PTT 爆量日）==")
    lags = []
    by_stock = ptt_events.groupby("stock_id")["date"].apply(list).to_dict()
    for row in rss.itertuples():
        candidates = [(d - row.date).days for d in by_stock.get(row.stock_id, []) if abs((d - row.date).days) <= 7]
        if candidates:
            lags.append(min(candidates, key=abs))
    lags = pd.Series(lags, dtype=float)
    if not lags.empty:
        print(f"  有對得上的事件 {len(lags)} / {len(rss)}；PTT 較早 {np.mean(lags < 0):.0%}、同一天 {np.mean(lags == 0):.0%}、"
              f"較晚 {np.mean(lags > 0):.0%}；中位數 {lags.median():+.0f} 天")
    offsets = range(-5, 6)
    profile = []
    for row in rss.itertuples():
        if row.stock_id not in daily:
            continue
        base = daily[row.stock_id]
        level = base[(base.index < row.date - pd.Timedelta(days=7)) & (base.index >= row.date - pd.Timedelta(days=37))].mean()
        profile.append([base.get(row.date + pd.Timedelta(days=o), np.nan) / (level + 0.5) for o in offsets])
    if profile:
        means = np.nanmean(np.array(profile, dtype=float), axis=0)
        print("  RSS 事件前後的 PTT 提及量（相對平常的倍數）：" + "  ".join(f"{o:+d}:{m:.1f}x" for o, m in zip(offsets, means)))

    print("\n== PTT 討論時段（盤中 09:00-13:30 才能當天反應）==")
    hours = found["when"].dt.hour + found["when"].dt.minute / 60
    weekday = found["when"].dt.dayofweek < 5
    print(f"  平日盤前 {np.mean(weekday & (hours < 9)):.0%}、盤中 {np.mean(weekday & (hours >= 9) & (hours < 13.5)):.0%}、"
          f"盤後 {np.mean(weekday & (hours >= 13.5)):.0%}、週末 {np.mean(~weekday):.0%}")

    print("\n== [標的] 文的多空方向（從發文後第一個收盤起算，扣 0050、扣平常水準）==")
    targets = posts[posts["title"].str.match(TARGET) & ~posts["title"].str.startswith(("Re:", "Fw:"))].copy()
    targets["stock_id"] = targets["title"].str.extract(TARGET)[0]
    targets["side"] = np.select([targets["title"].str.contains("空"), targets["title"].str.contains("多")], ["空", "多"], "其他")
    targets["when"] = taipei(targets["posted_at"])
    targets["date"] = [w.normalize() if (w.dayofweek < 5 and w.hour + w.minute / 60 < 13.5) else w.normalize() + pd.Timedelta(days=1)
                       for w in targets["when"]]
    print(f"  標的文 {len(targets)} 篇：{targets['side'].value_counts().to_dict()}；股票不在分析範圍內的會略過")
    calls = components(targets, daily_ar, normal)
    if not calls.empty:
        calls = calls.join(targets[["side", "push_score"]], on="key").drop_duplicates(["stock_id", "day0", "side"])
    if calls.empty:
        print("  沒有可以計算報酬的標的文")
        return
    for side in ("多", "空"):
        report(f"標的文：看{side}", calls[calls["side"].eq(side)])
    report("標的文：推文 ≥ 50（熱門）", calls[calls["push_score"] >= 50])


if __name__ == "__main__":
    main()
