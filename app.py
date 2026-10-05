from __future__ import annotations

import importlib
import inspect

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from sqlalchemy import or_, select

import trends.event_study

from trends.alpha_signal import parse_traffic, score_signals
from trends.database import (
    GoogleTrend,
    GoogleTrendNews,
    GoogleTrendsHistory,
    KeywordClassification,
    Stock,
    ThemeDailyStats,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.keyword_auto_classification import build_keyword_quality_summary
from trends.keyword_mapping import load_keyword_mapping, match_keyword
from trends.keyword_classification import classify_pending_keywords, classify_pending_news
from trends.entity_resolution_etl import build_entity_coverage, run_entity_resolution_etl
from trends.sentiment import analyze_pending_news
from trends.stock_collector import fetch_and_store
from trends.theme_study import (
    build_daily_theme_heat,
    build_cross_correlations,
    build_heat_price_correlations,
    build_post_news_returns,
    build_relative_returns,
    build_stock_technicals,
    build_theme_coverage,
    build_theme_definitions,
    build_theme_news_timeline,
    build_unclassified_keywords,
    sync_theme_mapping,
)


if "start_date" not in inspect.signature(trends.event_study.build_event_frame).parameters:
    trends.event_study = importlib.reload(trends.event_study)
if "start_date" not in inspect.signature(trends.event_study.build_event_frame).parameters:
    raise RuntimeError(
        f"{trends.event_study.__file__} build_event_frame does not accept start_date; "
        "restart Streamlit with the project .venv after verifying the imported module path."
    )

RESEARCH_START_DATE = trends.event_study.RESEARCH_START_DATE
RESEARCH_START = pd.Timestamp(RESEARCH_START_DATE)
EVENT_CLUSTER_GAP = pd.Timedelta(hours=24)


st.set_page_config(page_title="Trends × Theme Research", page_icon="📈", layout="wide")


@st.cache_resource
def report_event_study_source(module_path: str, function_signature: str) -> str:
    print(f"trends.event_study loaded from: {module_path}")
    print(f"build_event_frame signature: {function_signature}")
    return module_path


report_event_study_source(
    str(trends.event_study.__file__),
    str(inspect.signature(trends.event_study.build_event_frame)),
)
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;600;700&display=swap');
    :root { --ink: #20291f; --leaf: #2f6b4f; --lime: #d5ed78; --coral: #db795e; --paper: #f5f6ef; }
    html, body, [class*="css"] { font-family: 'DM Sans', 'Noto Sans TC', sans-serif; color: var(--ink); }
    .stApp { background: radial-gradient(ellipse at 92% 0%, #e5edd4 0, transparent 35%), var(--paper); }
    h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
    [data-testid="stMetric"] { background: #fff; border: 1px solid #e1e6da; border-left: 3px solid var(--leaf); padding: 12px 16px; border-radius: 4px; }
    [data-testid="stSidebar"] { background: #20291f; }
    [data-testid="stSidebar"] * { color: #f5f6ef; }
    div[data-testid="stDataFrame"] { border: 1px solid #e1e6da; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def database_engine():
    engine = init_db(get_engine())
    sync_theme_mapping(engine)
    return engine


@st.cache_data(ttl=300)
def load_trends() -> pd.DataFrame:
    with database_engine().connect() as connection:
        statement = select(GoogleTrend).where(
            or_(
                GoogleTrend.fetched_at >= RESEARCH_START.to_pydatetime(),
                GoogleTrend.fetched_at.is_(None) & (GoogleTrend.published_at >= RESEARCH_START.to_pydatetime()),
            )
        )
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_trend_observations() -> pd.DataFrame:
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
            GoogleTrendNews.event_type,
            KeywordClassification.canonical_keyword,
            KeywordClassification.theme_name.label("classification_theme"),
            KeywordClassification.sub_theme,
        )
        .outerjoin(GoogleTrendNews, GoogleTrendNews.trend_id == GoogleTrend.id)
        .outerjoin(KeywordClassification, KeywordClassification.keyword == GoogleTrend.keyword)
        .where(
            or_(
                GoogleTrend.fetched_at >= RESEARCH_START.to_pydatetime(),
                GoogleTrend.fetched_at.is_(None) & (GoogleTrend.published_at >= RESEARCH_START.to_pydatetime()),
            )
        )
    )
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_prices(stock_id: str | None = None) -> pd.DataFrame:
    statement = select(Stock).where(Stock.date >= RESEARCH_START.date()).order_by(Stock.date)
    if stock_id:
        statement = statement.where(Stock.stock_id == stock_id)
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_events() -> pd.DataFrame:
    return trends.event_study.build_event_frame(database_engine(), start_date=RESEARCH_START.date())


@st.cache_data(ttl=300)
def load_theme_history(theme_name: str) -> pd.DataFrame:
    start_date = (pd.Timestamp.now().normalize() - pd.Timedelta(days=89)).date()
    statement = (
        select(GoogleTrendsHistory)
        .where(
            GoogleTrendsHistory.theme_name == theme_name,
            GoogleTrendsHistory.trend_date >= start_date,
        )
        .order_by(GoogleTrendsHistory.trend_date)
    )
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


def load_theme_definitions() -> dict[str, dict[str, list[str]]]:
    with database_engine().connect() as connection:
        mapping = pd.read_sql(select(ThemeMapping), connection)
    return build_theme_definitions(mapping)


def load_theme_mapping_rows() -> pd.DataFrame:
    with database_engine().connect() as connection:
        return pd.read_sql(select(ThemeMapping), connection)


@st.cache_data(ttl=300)
def load_theme_daily_stats() -> pd.DataFrame:
    start_date = (pd.Timestamp.now().normalize() - pd.Timedelta(days=29)).date()
    statement = select(ThemeDailyStats).where(ThemeDailyStats.stat_date >= start_date).order_by(
        ThemeDailyStats.stat_date, ThemeDailyStats.theme_name
    )
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_keyword_quality_summary() -> dict[str, float | int]:
    return build_keyword_quality_summary(database_engine())


def show_empty(message: str) -> None:
    st.info(message)


def build_event_clusters(observations: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "event_cluster_id", "keyword", "event_type", "stock_ids", "first_seen", "last_seen",
        "duration_hours", "occurrence_count", "max_traffic", "news_count",
        "published_at", "news_title", "news_source", "news_url", "trend_ids",
    ]
    if observations.empty:
        return pd.DataFrame(columns=columns)

    frame = observations.copy()
    frame["published_at"] = pd.to_datetime(frame["published_at"], errors="coerce")
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], errors="coerce")
    frame["observed_at"] = frame["fetched_at"].fillna(frame["published_at"])
    frame = frame.dropna(subset=["trend_id", "keyword", "observed_at"])
    frame = frame[frame["observed_at"] >= RESEARCH_START]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    mappings = load_keyword_mapping()
    frame["normalized_keyword"] = frame["keyword"].map(
        lambda keyword: normalize_keyword(keyword, mappings)
    )
    classifications = frame["normalized_keyword"].map(
        lambda keyword: match_keyword(keyword, mappings)
    )
    frame[["event_type", "stock_ids"]] = pd.DataFrame(
        [(event_type, stock_ids) for _, event_type, stock_ids, _ in classifications],
        index=frame.index,
    )
    trend_rows = frame.drop_duplicates("trend_id").sort_values(
        ["normalized_keyword", "observed_at", "trend_id"]
    )
    output: list[dict] = []
    for keyword, keyword_rows in trend_rows.groupby("normalized_keyword", sort=False):
        cluster_numbers = keyword_rows["observed_at"].diff().ge(EVENT_CLUSTER_GAP).cumsum()
        for _, group in keyword_rows.groupby(cluster_numbers, sort=False):
            trend_ids = group["trend_id"].tolist()
            cluster_observations = frame[frame["trend_id"].isin(trend_ids)].copy()
            cluster_observations["news_key"] = cluster_observations["news_url"].fillna("").astype(str).str.strip()
            fallback_key = (
                cluster_observations["news_title"].fillna("").astype(str).str.strip()
                + "|"
                + cluster_observations["news_source"].fillna("").astype(str).str.strip()
            )
            cluster_observations["news_key"] = cluster_observations["news_key"].where(
                cluster_observations["news_key"].ne(""), fallback_key
            )
            news_rows = cluster_observations[cluster_observations["news_key"].str.strip("|").ne("")]
            titles = news_rows["news_title"].dropna().astype(str).drop_duplicates().tolist()
            sources = news_rows["news_source"].dropna().astype(str).drop_duplicates().tolist()
            urls = news_rows["news_url"].dropna().astype(str).drop_duplicates()
            first_seen = group["observed_at"].iloc[0]
            last_seen = group["observed_at"].iloc[-1]
            first_row = group.iloc[0]
            output.append({
                "event_cluster_id": f"EC-{int(first_row['trend_id'])}",
                "keyword": keyword,
                "event_type": first_row["event_type"],
                "stock_ids": first_row["stock_ids"],
                "first_seen": first_seen,
                "last_seen": last_seen,
                "duration_hours": (last_seen - first_seen).total_seconds() / 3600,
                "occurrence_count": len(group),
                "max_traffic": max((parse_traffic(value) for value in group["approx_traffic"]), default=0),
                "news_count": news_rows["news_key"].nunique(),
                "published_at": first_row["published_at"],
                "news_title": " / ".join(titles[:3]),
                "news_source": " / ".join(sources[:3]),
                "news_url": urls.iloc[0] if not urls.empty else None,
                "trend_ids": trend_ids,
            })
    return pd.DataFrame(output, columns=columns).sort_values("first_seen", ascending=False)


@st.cache_data(ttl=300)
def load_event_clusters() -> pd.DataFrame:
    return build_event_clusters(load_trend_observations())


@st.cache_data(ttl=300)
def load_clustered_events() -> pd.DataFrame:
    events = load_events()
    clusters = load_event_clusters()
    if events.empty or clusters.empty:
        return events.iloc[0:0].copy()
    trend_clusters = clusters[["event_cluster_id", "trend_ids"]].explode("trend_ids")
    selected = events.merge(trend_clusters, left_on="trend_id", right_on="trend_ids", how="inner")
    selected = selected.sort_values(["event_date", "trend_id", "news_id"]).drop_duplicates("event_cluster_id")
    cluster_details = clusters.drop(columns="trend_ids").rename(columns={
        "keyword": "cluster_keyword", "stock_id": "cluster_stock_id", "published_at": "cluster_published_at",
        "news_title": "cluster_news_title", "news_source": "cluster_news_source", "news_url": "cluster_news_url",
    })
    selected = selected.merge(cluster_details, on="event_cluster_id", how="left")
    selected["keyword"] = selected["cluster_keyword"]
    selected["published_at"] = selected["cluster_published_at"]
    selected["approx_traffic"] = selected["max_traffic"]
    selected["news_title"] = selected["cluster_news_title"]
    selected["news_source"] = selected["cluster_news_source"]
    selected["news_url"] = selected["cluster_news_url"]
    return selected


def get_stock_name(stock_id: str, mappings: pd.DataFrame | None = None) -> str:
    keyword_mapping = mappings if mappings is not None else load_keyword_mapping()
    names = keyword_mapping.loc[
        (keyword_mapping["type"] == "company")
        & (keyword_mapping["stock_id"] == stock_id)
        & keyword_mapping["keyword"].map(lambda name: any(ord(character) > 127 for character in name)),
        "keyword",
    ].drop_duplicates()
    preferred = [name for name in names if not any(term in name for term in ("股價", "妖股", "掃描器"))]
    return min(preferred or names.tolist(), key=len) if preferred or not names.empty else stock_id


def normalize_keyword(keyword: object, mappings: pd.DataFrame | None = None) -> str:
    value = str(keyword).strip()
    if not value:
        return value
    keyword_mapping = mappings if mappings is not None else load_keyword_mapping()
    canonical, event_type, stock_ids, _ = match_keyword(value, keyword_mapping)
    if event_type == "company" and stock_ids:
        return get_stock_name(stock_ids[0], keyword_mapping)
    return canonical


def format_percent(value: object) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    return f"{numeric:+.2%}" if pd.notna(numeric) else "N/A"


def build_event_ranking(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=[
            "事件群組ID", "事件分類", "關鍵字", "相關股票", "首次觀測", "最後觀測",
            "持續小時", "出現次數", "最高熱度", "新聞數量",
        ])
    ranking = events[events["stock_ids"].map(bool)].copy()
    ranking["相關股票"] = ranking["stock_ids"].map(
        lambda stock_ids: ", ".join(f"{stock_id} {get_stock_name(stock_id)}" for stock_id in stock_ids)
    )
    ranking = ranking.rename(columns={
        "event_cluster_id": "事件群組ID", "event_type": "事件分類", "keyword": "關鍵字",
        "first_seen": "首次觀測", "last_seen": "最後觀測", "duration_hours": "持續小時",
        "occurrence_count": "出現次數", "max_traffic": "最高熱度", "news_count": "新聞數量",
    })
    columns = [
        "事件群組ID", "事件分類", "關鍵字", "相關股票", "首次觀測", "最後觀測",
        "持續小時", "出現次數", "最高熱度", "新聞數量",
    ]
    return ranking[columns].sort_values("最高熱度", ascending=False)


def build_stock_ranking(events: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=["股票", "最近5日漲幅", "事件數", "最近熱搜時間"])
    stock_events = events.explode("stock_ids").rename(columns={"stock_ids": "stock_id"})
    stock_events = stock_events.dropna(subset=["stock_id"])
    ranking = stock_events.groupby("stock_id", as_index=False).agg(
        event_count=("event_cluster_id", "nunique"), latest_event=("last_seen", "max")
    )
    returns: dict[str, float] = {}
    if not prices.empty:
        price_frame = prices.copy()
        price_frame["date"] = pd.to_datetime(price_frame["date"], errors="coerce")
        for stock_id, stock_prices in price_frame.groupby("stock_id"):
            closes = pd.to_numeric(stock_prices.sort_values("date")["close"], errors="coerce").dropna()
            if len(closes) > 5:
                returns[stock_id] = closes.iloc[-1] / closes.iloc[-6] - 1
    ranking["five_day_return"] = ranking["stock_id"].map(returns)
    ranking["股票"] = ranking["stock_id"].map(lambda stock_id: f"{stock_id} {get_stock_name(stock_id)}")
    ranking = ranking.rename(columns={
        "five_day_return": "最近5日漲幅", "event_count": "事件數", "latest_event": "最近熱搜時間"
    })
    return ranking[["股票", "最近5日漲幅", "事件數", "最近熱搜時間"]].sort_values(
        ["事件數", "最近5日漲幅"], ascending=False
    )


def build_observation_ranking(events: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "股票", "關鍵字", "總分", "熱度分數", "持續時間分數", "新聞數量分數",
        "勝率分數", "成交量分數", "觀察狀態", "歷史勝率", "5日平均報酬", "最新事件時間",
    ]
    if events.empty:
        return pd.DataFrame(columns=columns)

    frame = score_signals(events).copy()
    frame["heat"] = frame["approx_traffic"].map(parse_traffic)
    frame["future_return_5d"] = pd.to_numeric(frame["future_return_5d"], errors="coerce")
    frame["volume_change"] = pd.to_numeric(frame["volume_change"], errors="coerce")
    ranking = frame.groupby(["stock_id", "keyword"], as_index=False).agg(
        heat=("heat", "max"),
        duration_hours=("duration_hours", "max"),
        news_count=("news_count", "sum"),
        historical_win_rate=("historical_win_rate", "last"),
        avg_return_5d=("future_return_5d", "mean"),
        volume_change=("volume_change", "mean"),
        latest_event=("last_seen", "max"),
    )
    ranking["熱度分數"] = (ranking["heat"] / (ranking["heat"] + 5000) * 100).fillna(0)
    ranking["持續時間分數"] = ranking["duration_hours"].clip(0, 24).fillna(0) / 24 * 100
    ranking["新聞數量分數"] = (ranking["news_count"] / (ranking["news_count"] + 5) * 100).fillna(0)
    ranking["勝率分數"] = ranking["historical_win_rate"].fillna(50).clip(0, 100)
    ranking["成交量分數"] = ((ranking["volume_change"].fillna(0).clip(-1, 3) + 1) / 4) * 100
    ranking["總分"] = (
        ranking["熱度分數"] * 0.25
        + ranking["持續時間分數"] * 0.20
        + ranking["新聞數量分數"] * 0.20
        + ranking["勝率分數"] * 0.20
        + ranking["成交量分數"] * 0.15
    ).round().clip(0, 100).astype(int)
    ranking["觀察狀態"] = ranking["總分"].map(
        lambda score: "🟢 可觀察" if score >= 80 else "🟡 注意風險" if score >= 60 else "🔴 已過熱"
    )
    ranking["股票"] = ranking["stock_id"].map(lambda stock_id: f"{stock_id} {get_stock_name(stock_id)}")
    ranking = ranking.rename(columns={
        "keyword": "關鍵字", "historical_win_rate": "歷史勝率",
        "avg_return_5d": "5日平均報酬", "latest_event": "最新事件時間",
    })
    return ranking[columns].sort_values("總分", ascending=False)


def page_dashboard() -> None:
    st.title("市場總覽")
    st.caption("事件驅動投資研究平台")
    quality_summary = load_keyword_quality_summary()
    trends = load_trends()
    clusters = load_event_clusters()
    prices = load_prices()
    entity_summary, unresolved_entities = build_entity_coverage(database_engine())
    today = pd.Timestamp.now().normalize()
    matched_clusters = clusters[clusters["stock_ids"].map(bool)] if not clusters.empty else clusters
    first_seen = pd.to_datetime(matched_clusters["first_seen"], errors="coerce") if not matched_clusters.empty else pd.Series(dtype="datetime64[ns]")
    new_events = int(first_seen.dt.normalize().eq(today).sum()) if not first_seen.empty else 0
    covered_stock_ids = matched_clusters["stock_ids"].explode().dropna().unique() if not matched_clusters.empty else []
    with st.container(horizontal=True):
        st.metric("總關鍵字數", f"{quality_summary.get('total_keywords', 0):,}")
        st.metric("已分類數", f"{quality_summary.get('classified_keywords', 0):,}")
        st.metric("未分類數", f"{quality_summary.get('unclassified_keywords', 0):,}")
        st.metric("分類覆蓋率", f"{quality_summary.get('coverage_rate', 0):.1%}")
    with st.container(horizontal=True):
        st.metric("追蹤關鍵字數", f"{trends['keyword'].nunique():,}" if not trends.empty else "0")
        st.metric("已配對事件數", f"{len(matched_clusters):,}")
        st.metric("涵蓋股票數", f"{len(covered_stock_ids):,}")
        st.metric("今日新增事件數", f"{new_events:,}")

    st.subheader("Entity Coverage")
    with st.container(horizontal=True):
        st.metric("總關鍵字數", f"{entity_summary['total_keywords']:,}")
        st.metric("已辨識實體數", f"{entity_summary['identified_entities']:,}")
        st.metric("未辨識實體數", f"{entity_summary['unidentified_entities']:,}")
        st.metric("辨識率", f"{entity_summary['recognition_rate']:.1%}")
    st.subheader("未辨識實體 TOP 100")
    if unresolved_entities.empty:
        show_empty("目前沒有未辨識實體。")
    else:
        st.dataframe(
            unresolved_entities.rename(columns={"keyword": "關鍵字", "trend_count": "出現次數"}),
            hide_index=True,
        )

    st.subheader("熱門事件排行榜")
    event_ranking = build_event_ranking(matched_clusters)
    if event_ranking.empty:
        show_empty("尚無已配對事件，請先匯入 Google Trends RSS 與新聞資料。")
    else:
        st.dataframe(event_ranking.head(15), hide_index=True)

    st.subheader("熱門股票排行榜")
    stock_ranking = build_stock_ranking(matched_clusters, prices)
    if stock_ranking.empty:
        show_empty("目前沒有可排名的事件股票。")
    else:
        st.dataframe(
            stock_ranking.head(15),
            column_config={"最近5日漲幅": st.column_config.NumberColumn(format="percent")},
            hide_index=True,
        )


def page_theme_study() -> None:
    st.title("主題研究")
    st.caption("Theme Analysis · 主題熱度、關鍵字驅動、股票敏感度與新聞後報酬")
    themes = load_theme_definitions()
    if not themes:
        show_empty("尚未設定主題映射，請確認 theme_mapping.csv。")
        return

    theme_name = st.selectbox("研究主題", list(themes))
    theme = themes[theme_name]
    keywords = theme["keywords"]
    stock_ids = theme["stock_ids"]
    selected_keywords = st.multiselect(
        "關鍵字篩選", keywords, default=keywords[:5], key=f"theme_keywords_{theme_name}"
    )
    selected_stock_ids = st.multiselect(
        "股票篩選", stock_ids, default=stock_ids, key=f"theme_stocks_{theme_name}"
    )
    indicators = st.multiselect(
        "技術指標",
        ["MA5", "MA20", "MA60", "Volume", "RSI", "MACD"],
        default=["MA5", "MA20", "MA60"],
        key="theme_indicators",
    )

    history = load_theme_history(theme_name)
    all_prices = load_prices()
    prices = all_prices[all_prices["stock_id"].astype(str).isin(selected_stock_ids)].copy() if not all_prices.empty else all_prices
    observations = load_trend_observations()
    daily_heat = build_daily_theme_heat(history, selected_keywords)
    news = build_theme_news_timeline(observations, selected_keywords)
    relative_returns = build_relative_returns(prices, selected_stock_ids)
    technical_frame = build_stock_technicals(prices, selected_stock_ids)
    same_day = build_heat_price_correlations(daily_heat, prices, selected_stock_ids)
    cross_correlations = build_cross_correlations(daily_heat, prices, selected_stock_ids, -10, 10)
    mapping_rows = load_theme_mapping_rows()
    coverage = build_theme_coverage(observations, mapping_rows)
    unclassified = build_unclassified_keywords(observations, mapping_rows)
    theme_counts = coverage[coverage["theme_name"] == theme_name]
    event_count = int(theme_counts.iloc[0]["event_count"]) if not theme_counts.empty else 0
    news_count = int(theme_counts.iloc[0]["news_count"]) if not theme_counts.empty else 0

    latest_date = daily_heat["date"].max() if not daily_heat.empty else pd.NaT
    if pd.notna(latest_date):
        recent_start = latest_date - pd.Timedelta(days=6)
        current_week = daily_heat[daily_heat["date"].between(recent_start, latest_date)]
        previous_week = daily_heat[
            daily_heat["date"].between(latest_date - pd.Timedelta(days=13), latest_date - pd.Timedelta(days=7))
        ]
        average_heat = current_week.groupby("date")["heat"].mean().mean()
        popular_keywords = current_week.groupby("keyword")["heat"].mean().sort_values(ascending=False)
        hot_keyword = popular_keywords.index[0] if not popular_keywords.empty else "N/A"
        average_previous_heat = previous_week.groupby("date")["heat"].mean().mean()
        heat_change = average_heat / average_previous_heat - 1 if pd.notna(average_previous_heat) and average_previous_heat > 0 else None
    else:
        current_week = pd.DataFrame(columns=["date", "keyword", "heat"])
        previous_week = current_week
        average_heat = float("nan")
        hot_keyword = "N/A"
        heat_change = None

    sensitivity = same_day.dropna(subset=["pearson"]).copy()
    if not sensitivity.empty:
        stock_sensitivity = sensitivity.groupby("stock_id")["pearson"].apply(lambda values: values.abs().mean())
        hot_stock_id = stock_sensitivity.idxmax()
        hot_stock = f"{hot_stock_id} {get_stock_name(hot_stock_id)}"
    else:
        hot_stock = "樣本不足"
    positive_mask = (
        pd.to_numeric(cross_correlations["best_lag"], errors="coerce").gt(0)
        & pd.to_numeric(cross_correlations["best_correlation"], errors="coerce").gt(0)
    )
    positive_lags = pd.to_numeric(cross_correlations.loc[positive_mask, "best_lag"], errors="coerce")
    average_lead = f"+{positive_lags.mean():.1f} 天" if not positive_lags.empty else "尚無正向領先樣本"

    with st.container(horizontal=True):
        st.metric("近 7 日平均熱度", f"{average_heat:.1f}" if pd.notna(average_heat) else "N/A", format_percent(heat_change))
        st.metric("熱門關鍵字", hot_keyword)
        st.metric("熱門股票", hot_stock)
        st.metric("主題事件數", f"{event_count:,}")
        st.metric("新聞數量", f"{news_count:,}")
        st.metric("熱度平均領先", average_lead)

    st.subheader("Google Trends × News × 股價同步時間軸")
    st.caption("依序觀察 Google Trends 熱度、新聞發布與股票相對報酬，對照事件發生的先後關係。")
    with st.container(horizontal=True):
        show_ma5 = st.checkbox("MA5", value=False, key=f"show_ma5_{theme_name}")
        show_ma20 = st.checkbox("MA20", value=False, key=f"show_ma20_{theme_name}")
        show_ma60 = st.checkbox("MA60", value=False, key=f"show_ma60_{theme_name}")
    if daily_heat.empty:
        show_empty("尚無歷史熱度；執行 `python -m trends.trend_history_collector` 回補近 90 天資料。")
    else:
        synchronized_figure = make_subplots(specs=[[{"secondary_y": True}]])
        for keyword, keyword_rows in daily_heat.groupby("keyword", sort=False):
            synchronized_figure.add_trace(go.Scatter(
                x=keyword_rows["date"], y=keyword_rows["heat"], mode="lines", name=keyword,
                line={"width": 2.5},
                hovertemplate=f"{keyword}<br>%{{x|%Y-%m-%d}}<br>熱度：%{{y}}<extra></extra>",
            ), secondary_y=False)
        if not news.empty:
            news_markers = news.copy()
            sentiment_palette = {"Positive": "#2e7d32", "Neutral": "#f9a825", "Negative": "#d32f2f"}
            for sentiment, color in sentiment_palette.items():
                subset = news_markers[news_markers["news_sentiment"].fillna("Neutral").eq(sentiment)]
                if subset.empty:
                    continue
                marker_y = subset["date"].map(lambda _: 100)
                synchronized_figure.add_trace(go.Scatter(
                    x=subset["date"], y=marker_y, mode="markers", name=f"新聞：{sentiment}",
                    marker={"symbol": "diamond", "size": 10, "color": color, "line": {"width": 1, "color": "#ffffff"}},
                    customdata=subset[["news_title", "news_source", "news_sentiment", "event_type"]],
                    hovertemplate=(
                        "日期：%{x|%Y-%m-%d}<br>"
                        "新聞標題：%{customdata[0]}<br>"
                        "來源：%{customdata[1]}<br>"
                        "情緒：%{customdata[2]}<br>"
                        "事件類型：%{customdata[3]}<extra></extra>"
                    ),
                ), secondary_y=False)
        if not relative_returns.empty:
            for stock_id, stock_path in technical_frame.groupby("stock_id", sort=False):
                stock_path = stock_path.sort_values("date")
                synchronized_figure.add_trace(go.Scatter(
                    x=stock_path["date"], y=stock_path["base_100"], mode="lines",
                    name=f"{stock_id} 相對報酬", line={"width": 2.5},
                    hovertemplate=f"{stock_id}<br>%{{x|%Y-%m-%d}}<br>相對報酬：%{{y:.2f}}<extra></extra>",
                ), secondary_y=True)
                for enabled, indicator, column in (
                    (show_ma5, "MA5", "ma5"),
                    (show_ma20, "MA20", "ma20"),
                    (show_ma60, "MA60", "ma60"),
                ):
                    if enabled:
                        synchronized_figure.add_trace(go.Scatter(
                            x=stock_path["date"], y=stock_path[column], mode="lines",
                            name=f"{stock_id} {indicator}", line={"dash": "dot", "width": 1.2},
                            hovertemplate=f"{stock_id} {indicator}<br>%{{x|%Y-%m-%d}}<br>值：%{{y:.2f}}<extra></extra>",
                        ), secondary_y=True)
        synchronized_figure.update_layout(
            height=560,
            xaxis_title="日期",
            template="plotly_white",
            hovermode="closest",
            legend={"orientation": "h", "y": 1.12},
        )
        synchronized_figure.update_yaxes(title_text="Google Trends 熱度 / 新聞事件", secondary_y=False)
        synchronized_figure.update_yaxes(title_text="股價相對報酬（Base = 100）", secondary_y=True)
        st.plotly_chart(synchronized_figure, width="stretch")

    st.subheader("新聞事件列表")
    if not news.empty:
        news_display = news.copy()
        news_display["新聞日期"] = pd.to_datetime(news_display["date"]).dt.strftime("%Y-%m-%d")
        news_display = news_display.rename(columns={
            "keyword": "關鍵字", "news_title": "新聞標題", "news_source": "新聞來源",
            "news_url": "新聞連結", "news_sentiment": "情緒", "event_type": "事件類型",
        })
        st.dataframe(news_display[["新聞日期", "關鍵字", "新聞標題", "新聞來源", "事件類型", "情緒", "新聞連結"]],
            column_config={"新聞連結": st.column_config.LinkColumn(display_text="開啟")},
            hide_index=True,
        )
    else:
        show_empty("此主題目前沒有匹配的 RSS 新聞。")

    st.subheader("關鍵字與股票 Pearson Heatmap")
    if same_day.empty:
        show_empty("歷史熱度或股票日報酬不足，暫無法計算相關係數。")
    else:
        heatmap_values = same_day.pivot(index="stock_id", columns="keyword", values="pearson").reindex(
            index=selected_stock_ids, columns=selected_keywords
        )
        heatmap = go.Figure(go.Heatmap(
            z=heatmap_values.to_numpy(), x=heatmap_values.columns.tolist(),
            y=[f"{stock_id} {get_stock_name(stock_id)}" for stock_id in heatmap_values.index],
            zmin=-1, zmax=1, colorscale="RdYlGn", colorbar={"title": "Pearson"},
            hovertemplate="股票：%{y}<br>關鍵字：%{x}<br>Pearson：%{z:.2f}<extra></extra>",
        ))
        heatmap.update_layout(height=max(320, 34 * len(selected_stock_ids)), xaxis_title="關鍵字", yaxis_title="股票")
        st.plotly_chart(heatmap, width="stretch")

    st.subheader("Lag Analysis · Cross Correlation")
    st.caption("比較每日熱度與股票日報酬，掃描 Lag -10 到 +10 個交易日。正值表示關鍵字熱度領先股價報酬；負值表示股價先行。最佳相關依絕對值選取，每組至少 5 筆有效樣本。")
    if cross_correlations.empty:
        show_empty("所選關鍵字與股票沒有足夠的歷史資料可計算 Lag。")
    else:
        lag_display = same_day.merge(cross_correlations, on=["stock_id", "keyword"], how="outer")
        lag_display.insert(0, "股票", lag_display["stock_id"].map(lambda stock_id: f"{stock_id} {get_stock_name(stock_id)}"))
        lag_display["最佳 Lag"] = lag_display["best_lag"].map(
            lambda value: f"{int(value):+d}" if pd.notna(value) else "N/A"
        )
        lag_display = lag_display.rename(columns={
            "keyword": "關鍵字", "pearson": "Pearson", "best_correlation": "最佳相關",
            "sample_count_y": "Lag 樣本數", "sample_count_x": "同日樣本數",
        })
        lag_display = lag_display[["股票", "關鍵字", "Pearson", "最佳 Lag", "最佳相關", "Lag 樣本數"]].sort_values(
            "最佳相關", key=lambda values: values.abs(), ascending=False, na_position="last"
        )
        st.dataframe(lag_display, column_config={
            column: st.column_config.NumberColumn(format="%.2f")
            for column in ("Pearson", "最佳相關")
        }, hide_index=True)
        strongest = cross_correlations.dropna(subset=["best_lag", "best_correlation"]).sort_values(
            "best_correlation", key=lambda values: values.abs(), ascending=False
        ).head(1)
        if not strongest.empty:
            strongest_row = strongest.iloc[0]
            lag = int(strongest_row["best_lag"])
            stock_name = get_stock_name(strongest_row["stock_id"])
            direction = "同向" if strongest_row["best_correlation"] > 0 else "反向"
            lag_meaning = (
                f"{strongest_row['keyword']} 熱度領先 {stock_name} {lag} 個交易日，兩者{direction}相關"
                if lag > 0 else
                f"{stock_name} 股價報酬領先 {strongest_row['keyword']} 熱度 {abs(lag)} 個交易日，兩者{direction}相關"
                if lag < 0 else f"熱度與 {stock_name} 股價報酬以同日相關最高，兩者{direction}相關"
            )
            st.caption(f"最佳組合解讀：{lag_meaning}，相關係數 {strongest_row['best_correlation']:.2f}。這是歷史相關，不代表因果或預測。")

    st.subheader("新聞時間軸與新聞後進場空間")
    if news.empty:
        show_empty("此主題目前沒有匹配的 RSS 新聞。")
    else:
        post_news = build_post_news_returns(news, prices, selected_stock_ids)
        news_display = news.merge(post_news, on=["date", "keyword", "news_title", "news_source"], how="left")
        news_display = news_display.rename(columns={
            "date": "日期", "keyword": "關鍵字", "news_title": "新聞標題", "news_source": "新聞來源",
            "news_url": "新聞連結", "avg_future_return": "新聞後5日平均報酬", "stock_count": "有效股票數",
        })
        st.dataframe(news_display, column_config={
            "新聞連結": st.column_config.LinkColumn(display_text="開啟"),
            "新聞後5日平均報酬": st.column_config.NumberColumn(format="percent"),
        }, hide_index=True)
        st.caption("新聞後報酬以新聞日期之後的下一個交易日收盤作為觀察起點，計算其後 5 個交易日平均報酬；尚未走完觀察期的新聞會留白。")

    st.subheader("主題覆蓋率")
    coverage_display = coverage.rename(columns={"theme_name": "主題", "event_count": "RSS 事件數", "news_count": "新聞數量"})
    st.dataframe(coverage_display, hide_index=True)
    st.subheader("Theme Daily Stats")
    daily_stats = load_theme_daily_stats()
    if daily_stats.empty:
        show_empty("尚無每日主題統計；執行 `python -m trends.etl` 建立 Analytics Layer。")
    else:
        st.dataframe(daily_stats.rename(columns={
            "stat_date": "日期", "theme_name": "主題", "keyword_count": "熱度關鍵字數",
            "news_count": "新聞數", "event_count": "事件數", "avg_trend_score": "平均熱度",
            "max_trend_score": "最高熱度", "stock_count": "股票數",
        }), hide_index=True)
    st.subheader("未分類關鍵字 TOP 100")
    if unclassified.empty:
        show_empty("目前沒有未分類的 RSS 關鍵字。")
    else:
        st.dataframe(unclassified.rename(columns={"keyword": "關鍵字", "event_count": "出現次數"}), hide_index=True)


def page_observation() -> None:
    st.title("還來得及上車嗎？")
    st.caption("總分 = 熱度 25% + 持續時間 20% + 新聞數量 20% + 勝率 20% + 成交量 15%；各分項換算至 0–100，僅供研究觀察。")
    events = load_clustered_events()
    ranking = build_observation_ranking(events)
    st.subheader("投資觀察評分")
    if ranking.empty:
        show_empty("事件樣本不足，尚無可計算的觀察評分。")
    else:
        st.dataframe(
            ranking,
            column_config={
                "總分": st.column_config.ProgressColumn("總分", min_value=0, max_value=100),
                **{
                    column: st.column_config.NumberColumn(format="%.1f")
                    for column in ("熱度分數", "持續時間分數", "新聞數量分數", "勝率分數", "成交量分數")
                },
                "歷史勝率": st.column_config.NumberColumn(format="%.1f%%"),
                "5日平均報酬": st.column_config.NumberColumn(format="percent"),
            },
            hide_index=True,
        )

    summary = trends.event_study.summarize_event_study(events)
    count = len(events)
    five_day = summary.loc[summary["holding_days"] == 5].iloc[0]
    ten_day = summary.loc[summary["holding_days"] == 10].iloc[0]
    available = summary.dropna(subset=["avg_return_pct"])
    best_days = f"{int(available.sort_values('avg_return_pct', ascending=False).iloc[0]['holding_days'])} 日" if not available.empty else "N/A"
    with st.container(horizontal=True):
        st.metric("總事件數", f"{count:,}")
        st.metric("平均5日報酬", f"{five_day['avg_return_pct']:.2f}%" if pd.notna(five_day["avg_return_pct"]) else "N/A")
        st.metric("平均10日報酬", f"{ten_day['avg_return_pct']:.2f}%" if pd.notna(ten_day["avg_return_pct"]) else "N/A")
        st.metric("最佳持有天數", best_days)
        st.metric("5日勝率", f"{five_day['win_rate_pct']:.1f}%" if pd.notna(five_day["win_rate_pct"]) else "N/A")
    st.subheader("研究統計")
    st.dataframe(summary, hide_index=True)


PAGES = {
    "市場總覽": page_dashboard,
    "主題研究": page_theme_study,
    "還來得及上車嗎？": page_observation,
}


with st.sidebar:
    st.title("TRENDS / THEME")
    selected_page = st.radio("研究頁面", list(PAGES), index=1, label_visibility="collapsed")
    st.divider()
    if st.button("更新股價", icon=":material/sync:", width="stretch"):
        with st.spinner("下載最近行情…"):
            row_count = fetch_and_store()
        load_prices.clear()
        load_events.clear()
        load_clustered_events.clear()
        st.success(f"更新 {row_count:,} 筆")
        st.rerun()
    if st.button("分析未分類新聞", icon=":material/mood:", width="stretch"):
        with st.spinner("分析新聞情緒…"):
            count = analyze_pending_news(database_engine())
        load_events.clear()
        load_clustered_events.clear()
        st.success(f"完成 {count} 筆新聞情緒分類")
        st.rerun()
    if st.button("分類 RSS 關鍵字與新聞", icon=":material/auto_awesome:", width="stretch"):
        with st.spinner("先辨識實體，再分類關鍵字與新聞…"):
            entity_count = run_entity_resolution_etl(database_engine(), limit=100)
            keyword_count = classify_pending_keywords(database_engine(), limit=100, resolve_entities=False)
            try:
                news_count = classify_pending_news(database_engine(), limit=100)
            except RuntimeError as error:
                news_count = 0
                st.warning(str(error))
            from trends.etl import refresh_theme_daily_stats

            refresh_theme_daily_stats(database_engine())
        load_trend_observations.clear()
        load_theme_daily_stats.clear()
        st.success(f"辨識 {entity_count} 個實體，完成 {keyword_count} 個關鍵字、{news_count} 篇新聞")
        st.rerun()
    st.caption("行情排程：台北時間每日 18:00\n情緒分類模型首次執行時需下載")

PAGES[selected_page]()