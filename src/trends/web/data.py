"""Data the pages read: cached database queries, per-stock event datasets and system status.

The Dashboard only reads; the scheduled tasks (RSS, ETL, stocks) write. Caches expire after five
minutes, so a page is at most five minutes behind the database.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import String, func, select, type_coerce

import trends.event_study
from trends.database import (
    DB_PATH,
    LINK_DIRECT,
    LINK_THEME,
    GoogleTrend,
    GoogleTrendNews,
    GoogleTrendsHistory,
    KeywordClassification,
    Stock,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.event_study import calculate_event_performance, detect_research_events, normal_excess_returns
from trends.theme_study import build_theme_definitions, sync_theme_mapping
from trends.timeutil import (
    format_db_datetime,
    parse_db_datetime,
    taipei_day_start_utc,
    taipei_today,
    utc_now,
    utc_series_to_taipei_date,
    utc_to_taipei,
)

RESEARCH_START = pd.Timestamp(trends.event_study.RESEARCH_START_DATE)
ANALYZED_SENTIMENTS = ("Positive", "Neutral", "Negative")
UNANALYZED = "Unanalyzed"
SENTIMENT_COVERAGE_MIN_PCT = 20.0
LOW_SENTIMENT_COVERAGE_MESSAGE = "情緒樣本不足，請謹慎解讀"
# Event returns are compared with this ETF so a rising market is not read as an event effect.
BENCHMARK_STOCK = "0050"
LINK_LABELS = {LINK_DIRECT: "直接", LINK_THEME: "主題"}
ETL_HOURS = (2, 8, 14, 20)
LOG_DIR = DB_PATH.parent / "logs"
LISTED_CACHE = DB_PATH.with_name(f"{DB_PATH.stem}.listed_companies.json")


# ---------- engine and raw queries ----------

@st.cache_resource
def database_engine():
    engine = init_db(get_engine())
    sync_theme_mapping(engine)
    return engine


def _read(statement) -> pd.DataFrame:
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_trend_observations() -> pd.DataFrame:
    """RSS trends joined to their news and classification, from the research start on."""
    statement = (
        select(
            GoogleTrend.id.label("trend_id"),
            GoogleTrend.keyword,
            GoogleTrend.approx_traffic,
            GoogleTrend.published_at,
            GoogleTrend.fetched_at,
            GoogleTrendNews.id.label("news_id"),
            GoogleTrendNews.news_title,
            GoogleTrendNews.news_url,
            GoogleTrendNews.news_source,
            GoogleTrendNews.news_sentiment,
            GoogleTrendNews.sentiment_score,
            GoogleTrendNews.event_type,
            KeywordClassification.canonical_keyword,
            KeywordClassification.theme_name.label("classification_theme"),
            KeywordClassification.sub_theme,
        )
        .outerjoin(GoogleTrendNews, GoogleTrendNews.trend_id == GoogleTrend.id)
        .outerjoin(KeywordClassification, KeywordClassification.keyword == GoogleTrend.keyword)
        .where(
            # Stored values are canonical naive-UTC strings; compare as text against the UTC instant
            # at which the research start day begins in Asia/Taipei.
            type_coerce(func.coalesce(GoogleTrend.published_at, GoogleTrend.fetched_at), String)
            >= format_db_datetime(taipei_day_start_utc(RESEARCH_START.date()))
        )
    )
    return _read(statement)


@st.cache_data(ttl=300)
def load_prices(stock_id: str | None = None, start_date=None) -> pd.DataFrame:
    statement = select(Stock).order_by(Stock.stock_id, Stock.date)
    if stock_id:
        statement = statement.where(Stock.stock_id == stock_id)
    if start_date:
        statement = statement.where(Stock.date >= start_date)
    return _read(statement)


@st.cache_data(ttl=300)
def load_history(keywords: tuple[str, ...]) -> pd.DataFrame:
    if not keywords:
        return pd.DataFrame(columns=["keyword", "theme_name", "trend_date", "trend_score"])
    statement = select(GoogleTrendsHistory).where(
        GoogleTrendsHistory.keyword.in_(keywords),
        GoogleTrendsHistory.trend_date >= (RESEARCH_START - pd.Timedelta(days=30)).date(),
    )
    return _read(statement)


@st.cache_data(ttl=300)
def load_links() -> pd.DataFrame:
    links = _read(select(ThemeMapping).where(ThemeMapping.active == 1))
    links["stock_id"] = links["stock_id"].astype("string")
    return links


@st.cache_data(ttl=300)
def load_theme_definitions() -> dict[str, dict[str, list[str]]]:
    return build_theme_definitions(_read(select(ThemeMapping)))


@st.cache_data(ttl=300)
def table_counts() -> dict[str, int]:
    names = ("google_trends", "google_trends_news", "google_trends_observations", "keyword_classification",
             "theme_mapping", "entity_master", "google_trends_history", "stocks", "theme_daily_stats")
    with database_engine().connect() as connection:
        return {name: int(connection.exec_driver_sql(f"SELECT COUNT(*) FROM {name}").scalar() or 0) for name in names}


# ---------- sentiment helpers (shared with tests through trends.dashboard) ----------

def normalize_sentiment(values: pd.Series) -> pd.Series:
    sentiment = values.astype("object").where(values.notna(), UNANALYZED).astype(str).str.strip().str.title()
    return sentiment.where(sentiment.isin(ANALYZED_SENTIMENTS), UNANALYZED)


def sentiment_coverage_pct(analyzed: int, total: int) -> float | None:
    return analyzed / total * 100 if total else None


def is_low_sentiment_coverage(coverage_pct: float | None) -> bool:
    return coverage_pct is not None and coverage_pct < SENTIMENT_COVERAGE_MIN_PCT


def format_sentiment_coverage(analyzed: int, total: int) -> str:
    coverage = sentiment_coverage_pct(analyzed, total)
    return f"{analyzed:,} / {total:,}（{coverage:.1f}%）" if coverage is not None else f"{analyzed:,} / {total:,}（N/A）"


def summarize_sentiment_distribution(news: pd.DataFrame) -> dict[str, object]:
    """Sentiment percentages use analyzed news only; unanalyzed news is counted separately."""
    if news.empty or "news_sentiment" not in news.columns:
        sentiment = pd.Series(dtype=str)
    else:
        sentiment = normalize_sentiment(news["news_sentiment"])
    counts = sentiment.value_counts()
    total = int(len(sentiment))
    analyzed = int(sum(counts.get(label, 0) for label in ANALYZED_SENTIMENTS))
    coverage = sentiment_coverage_pct(analyzed, total)
    summary: dict[str, object] = {
        "total": total,
        "analyzed": analyzed,
        "unanalyzed": int(counts.get(UNANALYZED, 0)),
        "coverage_pct": coverage,
        "low_coverage": is_low_sentiment_coverage(coverage),
    }
    for label in ANALYZED_SENTIMENTS:
        count = int(counts.get(label, 0))
        summary[f"{label.lower()}_n"] = count
        summary[f"{label.lower()}_pct"] = count / analyzed * 100 if analyzed else None
    return summary


def build_sentiment_verification_sample(news: pd.DataFrame) -> pd.DataFrame:
    if not news.empty and "news_sentiment" in news.columns:
        news = news[normalize_sentiment(news["news_sentiment"]).isin(ANALYZED_SENTIMENTS)]
    if news.empty:
        return pd.DataFrame(columns=["新聞標題", "新聞摘要", "情緒標籤", "信心分數", "分析模型", "分析時間"])
    sample = news[["news_title", "news_source", "news_sentiment", "sentiment_score", "date"]].copy()
    sample = sample.rename(columns={
        "news_title": "新聞標題",
        "news_source": "新聞來源",
        "news_sentiment": "情緒標籤",
        "sentiment_score": "信心分數",
        "date": "分析時間",
    })
    sample["新聞摘要"] = sample["新聞標題"].fillna("新聞內容未提供")
    sample["分析模型"] = "Gemini（中文新聞情緒分類）"
    sample["分析時間"] = pd.to_datetime(sample["分析時間"], errors="coerce").dt.strftime("%Y-%m-%d")
    return sample[["新聞標題", "新聞摘要", "情緒標籤", "信心分數", "分析模型", "分析時間"]].head(20)


def build_data_quality_check(performance: pd.DataFrame, news: pd.DataFrame) -> dict[str, object]:
    sentiment = summarize_sentiment_distribution(news)
    metrics = {
        "positive_pct": sentiment["positive_pct"],
        "neutral_pct": sentiment["neutral_pct"],
        "negative_pct": sentiment["negative_pct"],
        "analyzed_news": sentiment["analyzed"],
        "total_news": sentiment["total"],
        "sentiment_coverage_pct": sentiment["coverage_pct"],
        "low_sentiment_coverage": sentiment["low_coverage"],
        "reaction_days_valid_pct": 100.0,
        "ma20_warning": False,
        "ma60_warning": False,
        "sentiment_warning": bool(sentiment["analyzed"] and sentiment["neutral_pct"] > 80),
    }
    if not performance.empty:
        reaction_days = pd.to_numeric(performance.get("reaction_days", pd.Series(dtype=float)), errors="coerce")
        valid = reaction_days.dropna().between(0, 30)
        metrics["reaction_days_valid_pct"] = float(valid.mean() * 100) if not reaction_days.empty else 100.0
        ma20 = pd.to_numeric(performance.get("ma20_breakout_day", pd.Series(dtype=float)), errors="coerce")
        ma60 = pd.to_numeric(performance.get("ma60_breakout_day", pd.Series(dtype=float)), errors="coerce")
        metrics["ma20_warning"] = bool(ma20.dropna().mode().size and ma20.dropna().value_counts().max() / len(ma20.dropna()) > 0.35)
        metrics["ma60_warning"] = bool(ma60.dropna().mode().size and ma60.dropna().value_counts().max() / len(ma60.dropna()) > 0.35)
    return metrics


def prepare_theme_news(observations: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    columns = ["date", "keyword", "news_id", "news_title", "news_source", "news_url", "news_sentiment", "sentiment_score"]
    if observations.empty or not keywords:
        return pd.DataFrame(columns=columns)
    news = observations[observations["keyword"].isin(keywords)].copy()
    news = news.dropna(subset=["news_id"]).drop_duplicates("news_id")
    # Event time is the RSS pubDate (UTC); the business day is its Asia/Taipei calendar date.
    news["date"] = utc_series_to_taipei_date(news["published_at"].fillna(news["fetched_at"]))
    news["news_sentiment"] = normalize_sentiment(news["news_sentiment"])
    news["sentiment_score"] = pd.to_numeric(news.get("sentiment_score", pd.Series([None] * len(news))), errors="coerce")
    return news.dropna(subset=["date"])[columns]


def prepare_theme_rss(observations: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    """One row per RSS trend of these keywords: its Asia/Taipei day and approx_traffic."""
    columns = ["date", "keyword", "approx_traffic"]
    if observations.empty or not keywords:
        return pd.DataFrame(columns=columns)
    trends = observations[observations["keyword"].isin(keywords)].drop_duplicates("trend_id").copy()
    trends["date"] = utc_series_to_taipei_date(trends["published_at"].fillna(trends["fetched_at"]))
    return trends.dropna(subset=["date"])[columns]


# ---------- stocks ----------

@st.cache_data(ttl=3600)
def stock_names() -> dict[str, str]:
    """Code -> display name: the official TWSE/TPEx short name, else the direct-link keyword."""
    names: dict[str, str] = {}
    links = load_links()
    direct = links[links["link_type"].eq(LINK_DIRECT) & links["stock_id"].notna()]
    for row in direct.sort_values("keyword", key=lambda s: s.str.len()).itertuples():
        names.setdefault(str(row.stock_id), str(row.keyword))
    if LISTED_CACHE.is_file():
        try:
            names.update(json.loads(LISTED_CACHE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return names


def stock_label(stock_id: str) -> str:
    name = stock_names().get(str(stock_id))
    return f"{stock_id} {name}" if name and name != stock_id else str(stock_id)


@st.cache_data(ttl=300)
def stock_options() -> pd.DataFrame:
    """Stocks with at least one active link and prices, with how many keywords point at them."""
    links = load_links()
    links = links[links["stock_id"].notna() & links["link_type"].isin((LINK_DIRECT, LINK_THEME))]
    with database_engine().connect() as connection:
        priced = set(connection.execute(select(Stock.stock_id).distinct()).scalars())
    counts = (links.groupby(["stock_id", "link_type"])["keyword"].nunique().unstack(fill_value=0)
              .reindex(columns=[LINK_DIRECT, LINK_THEME], fill_value=0).reset_index())
    counts = counts[counts["stock_id"].isin(priced) & ~counts["stock_id"].eq(BENCHMARK_STOCK)]
    counts["label"] = counts["stock_id"].map(stock_label)
    counts["total"] = counts[LINK_DIRECT] + counts[LINK_THEME]
    return counts.sort_values(["total", "stock_id"], ascending=[False, True]).reset_index(drop=True)


# ---------- per-stock event dataset ----------

@dataclass
class StockEvents:
    stock_id: str
    prices: pd.DataFrame                 # date, close, ma20, high, low
    heat: pd.DataFrame                   # date, keyword, heat
    news_daily: pd.DataFrame             # date, Positive, Neutral, Negative, Unanalyzed
    events: pd.DataFrame                 # one row per event day (see build_stock_events)
    news: pd.DataFrame                   # every matched news item
    normal: dict[int, float] = field(default_factory=dict)
    keywords: dict[str, str] = field(default_factory=dict)   # keyword -> link type


def _match_keywords(observations: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    """Rows whose RSS keyword or its canonical form is one of ``keywords``; ``keyword`` becomes
    the canonical form (theme_mapping stores canonical keywords, RSS stores raw ones)."""
    canonical = observations["canonical_keyword"].fillna(observations["keyword"])
    mask = observations["keyword"].isin(keywords) | canonical.isin(keywords)
    rows = observations[mask].copy()
    rows["keyword"] = canonical[mask].where(canonical[mask].isin(keywords), rows["keyword"])
    rows["date"] = utc_series_to_taipei_date(rows["published_at"].fillna(rows["fetched_at"]))
    return rows.dropna(subset=["date"])


@st.cache_data(ttl=300, show_spinner="整理這檔股票的熱搜、新聞與股價…")
def build_stock_events(stock_id: str, link_types: tuple[str, ...]) -> StockEvents | None:
    links = load_links()
    links = links[links["stock_id"].eq(stock_id) & links["link_type"].isin(link_types)]
    keyword_types = links.drop_duplicates("keyword").set_index("keyword")["link_type"].to_dict()
    keywords = list(keyword_types)
    warmup = (RESEARCH_START - pd.Timedelta(days=100)).date()
    prices = load_prices(stock_id, warmup).copy()
    if prices.empty or not keywords:
        return None
    prices["date"] = pd.to_datetime(prices["date"]).dt.normalize()
    prices = prices.sort_values("date")
    prices["ma20"] = prices["close"].rolling(20).mean()

    observations = _match_keywords(load_trend_observations(), keywords)
    news = observations.dropna(subset=["news_id"]).drop_duplicates("news_id").copy()
    news["news_sentiment"] = normalize_sentiment(news["news_sentiment"])
    trends_rows = observations.drop_duplicates("trend_id")

    history = load_history(tuple(keywords)).copy()
    history["date"] = pd.to_datetime(history["trend_date"]).dt.normalize()
    heat = history.groupby(["date", "keyword"], as_index=False)["trend_score"].mean().rename(columns={"trend_score": "heat"})

    triggers = detect_research_events(
        heat, news[["date", "keyword", "news_id"]], daily_rss=trends_rows[["date", "keyword", "approx_traffic"]],
    )
    triggers = triggers[triggers["event_date"] >= RESEARCH_START]
    benchmark = load_prices(BENCHMARK_STOCK, warmup)
    events = pd.DataFrame()
    if not triggers.empty:
        triggers = triggers.assign(stock_id=stock_id, link_type=triggers["keyword"].map(keyword_types))
        performance = calculate_event_performance(triggers, load_prices(stock_id, warmup), benchmark)
        events = _merge_event_days(performance, news)

    news_daily = (news.groupby(["date", "news_sentiment"]).size().unstack(fill_value=0)
              .reindex(columns=[*ANALYZED_SENTIMENTS, UNANALYZED], fill_value=0).reset_index())
    normal = normal_excess_returns(load_prices(stock_id, warmup), benchmark, RESEARCH_START, prices["date"].max())
    return StockEvents(stock_id, prices, heat, news_daily, events, news, normal, keyword_types)


def _merge_event_days(performance: pd.DataFrame, news: pd.DataFrame) -> pd.DataFrame:
    """Collapse same-day events of several keywords into one numbered event with its news."""
    if performance.empty:
        return pd.DataFrame()
    performance = performance.copy()
    performance["event_date"] = pd.to_datetime(performance["event_date"]).dt.normalize()
    rows = []
    for day, group in performance.groupby("event_date"):
        first = group.iloc[0]
        keywords = list(dict.fromkeys(group["keyword"]))
        day_news = news[news["date"].eq(day) & news["keyword"].isin(keywords)]
        rows.append({
            "event_date": day,
            "market_date": first["market_date"],
            "keywords": keywords,
            "link_types": sorted(set(group["link_type"].dropna())),
            "triggers": sorted({t for value in group["trigger"].dropna() for t in str(value).split("、")}),
            "traffic": pd.to_numeric(group.get("rss_traffic"), errors="coerce").max(),
            **{c: first[c] for c in ("return_1d", "return_5d", "return_10d", "excess_1d", "excess_5d", "excess_10d") if c in group},
            "news_count": int(len(day_news)),
            "positive": int(day_news["news_sentiment"].eq("Positive").sum()),
            "negative": int(day_news["news_sentiment"].eq("Negative").sum()),
            "event_types": [t for t in day_news["event_type"].dropna().value_counts().index if t != "其他"][:3],
            "headlines": day_news[["news_title", "news_source", "news_url", "news_sentiment"]].head(5).to_dict("records"),
        })
    events = pd.DataFrame(rows).sort_values("event_date").reset_index(drop=True)
    events.insert(0, "number", range(1, len(events) + 1))
    return events


# ---------- market radar ----------

@st.cache_data(ttl=300)
def build_market_radar() -> pd.DataFrame:
    """Per theme: search-interest change (last 7 vs previous 7 days), last 7 days' news by tone,
    and the curated basket's equal-weight 10-day return. The catch-all 「其他」 is left out."""
    themes = {name: d for name, d in load_theme_definitions().items() if name != "其他"}
    observations = load_trend_observations()
    all_prices = load_prices()
    today = pd.Timestamp(taipei_today())
    rows = []
    for theme_name, definition in themes.items():
        keywords = definition["keywords"]
        history = load_history(tuple(keywords)).copy()
        history["trend_date"] = pd.to_datetime(history["trend_date"])
        recent = history[history["trend_date"] >= today - pd.Timedelta(days=6)]["trend_score"].mean()
        previous = history[history["trend_date"].between(today - pd.Timedelta(days=13), today - pd.Timedelta(days=7))]["trend_score"].mean()
        news = _match_keywords(observations, keywords).dropna(subset=["news_id"]).drop_duplicates("news_id")
        news = news[news["date"] >= today - pd.Timedelta(days=6)]
        sentiment = summarize_sentiment_distribution(news)
        basket = [s for s in definition["stock_ids"] if s]
        returns = []
        for stock_id in basket:
            closes = all_prices[all_prices["stock_id"].eq(stock_id)].sort_values("date")["close"].dropna()
            if len(closes) >= 11:
                returns.append(closes.iloc[-1] / closes.iloc[-11] - 1)
        rows.append({
            "theme": theme_name,
            "heat_change": recent / previous - 1 if pd.notna(previous) and previous > 0 else None,
            "heat_recent": recent,
            "news": sentiment["total"],
            "positive": sentiment["positive_n"],
            "neutral": sentiment["neutral_n"],
            "negative": sentiment["negative_n"],
            "return_10d": float(pd.Series(returns).mean()) if returns else None,
            "stocks": basket,
        })
    return pd.DataFrame(rows)


# ---------- research ----------

@st.cache_data(ttl=3600, show_spinner="計算研究結果…")
def research_results() -> dict[str, object]:
    from trends.research import study

    result = study()
    return {"paths": result["paths"], "units": {k: len(v) for k, v in result["units"].items()},
            "events": len(result["events"]), "period_end": result["period"][1]}


# ---------- system status ----------

def _last_run(log_name: str, task: str) -> datetime | None:
    path = LOG_DIR / log_name
    if not path.is_file():
        return None
    pattern = re.compile(rf"^\[(\d{{4}}/\d{{2}}/\d{{2}})\s+(\d{{1,2}}:\d{{2}}:\d{{2}})\] {task} exit")
    last = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
        match = pattern.match(line)
        if match:
            last = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y/%m/%d %H:%M:%S")
    return last


@st.cache_data(ttl=60)
def system_status() -> dict[str, object]:
    """Is the pipeline running? Green when RSS arrived within the hour and the ETL ran within
    seven hours (it runs every six); pending items are shown but do not change the light."""
    with database_engine().connect() as connection:
        latest_rss = parse_db_datetime(connection.execute(select(func.max(GoogleTrend.fetched_at))).scalar())
        pending_news = connection.execute(
            select(func.count()).select_from(GoogleTrendNews).where(GoogleTrendNews.news_sentiment.is_(None))
        ).scalar() or 0
        keywords = connection.scalar(select(func.count(func.distinct(GoogleTrend.keyword)))) or 0
        news_total = connection.scalar(select(func.count()).select_from(GoogleTrendNews)) or 0
        stock_date = connection.execute(select(func.max(Stock.date))).scalar()
    now_utc = utc_now()
    now_taipei = utc_to_taipei(now_utc).replace(tzinfo=None)
    rss_minutes = (now_utc - latest_rss).total_seconds() / 60 if latest_rss else None
    last_etl = _last_run("etl.log", "etl")
    etl_hours = (now_taipei - last_etl).total_seconds() / 3600 if last_etl else None
    upcoming = [now_taipei.replace(hour=h, minute=0, second=0, microsecond=0) for h in ETL_HOURS]
    next_etl = next((t for t in upcoming if t > now_taipei), upcoming[0] + timedelta(days=1))
    if rss_minutes is None or rss_minutes > 24 * 60:
        light, label = "red", "🔴 超過 24 小時沒有新資料"
    elif rss_minutes > 60 or etl_hours is None or etl_hours > 7:
        light, label = "yellow", "🟡 自動更新延遲中"
    else:
        light, label = "green", "🟢 自動更新正常"
    return {
        "light": light, "label": label,
        "latest_rss": utc_to_taipei(latest_rss).replace(tzinfo=None) if latest_rss else None,
        "last_etl": last_etl, "next_etl": next_etl, "pending_news": int(pending_news),
        "keywords": int(keywords), "news_total": int(news_total), "stock_date": stock_date,
    }


def clear_caches() -> None:
    st.cache_data.clear()
