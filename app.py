from __future__ import annotations

import importlib
import inspect

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import or_, select

import trends.event_study

from trends.alpha_signal import parse_traffic, score_signals
from trends.database import GoogleTrend, GoogleTrendNews, Stock, get_engine, init_db
from trends.keyword_mapping import load_keyword_mapping, match_keyword
from trends.sentiment import analyze_pending_news
from trends.stock_collector import fetch_and_store


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


st.set_page_config(page_title="Trends × 台股事件研究", page_icon="📈", layout="wide")


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
    return init_db(get_engine())


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
        )
        .outerjoin(GoogleTrendNews, GoogleTrendNews.trend_id == GoogleTrend.id)
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
    trends = load_trends()
    clusters = load_event_clusters()
    prices = load_prices()
    today = pd.Timestamp.now().normalize()
    matched_clusters = clusters[clusters["stock_ids"].map(bool)] if not clusters.empty else clusters
    first_seen = pd.to_datetime(matched_clusters["first_seen"], errors="coerce") if not matched_clusters.empty else pd.Series(dtype="datetime64[ns]")
    new_events = int(first_seen.dt.normalize().eq(today).sum()) if not first_seen.empty else 0
    covered_stock_ids = matched_clusters["stock_ids"].explode().dropna().unique() if not matched_clusters.empty else []
    with st.container(horizontal=True):
        st.metric("追蹤關鍵字數", f"{trends['keyword'].nunique():,}" if not trends.empty else "0")
        st.metric("已配對事件數", f"{len(matched_clusters):,}")
        st.metric("涵蓋股票數", f"{len(covered_stock_ids):,}")
        st.metric("今日新增事件數", f"{new_events:,}")

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


def page_stock_events() -> None:
    st.title("趨勢群組分析")
    clusters = load_event_clusters()
    if not clusters.empty:
        clusters = clusters[clusters["stock_ids"].map(bool)].copy()
    if clusters.empty:
        show_empty("研究期間內尚無趨勢事件群組。")
        return

    prices = load_prices()
    available_stock_ids = set(prices["stock_id"].astype(str)) if not prices.empty else set()
    all_aligned_returns = trends.event_study.build_event_aligned_returns(
        clusters,
        prices,
        start_date=RESEARCH_START_DATE,
    )
    groups_with_day1 = set(
        all_aligned_returns.loc[all_aligned_returns["day_offset"] == 1, "cluster_id"]
    )
    cluster_lookup = clusters.set_index("event_cluster_id", drop=False)
    cluster_ids = clusters["event_cluster_id"].tolist()
    default_index = next(
        (
            index for index, cluster in enumerate(clusters.itertuples(index=False))
            if (
                set(cluster.stock_ids or []).intersection(available_stock_ids)
                and cluster.event_cluster_id in groups_with_day1
            )
        ),
        0,
    )
    selected_cluster_id = st.selectbox(
        "趨勢事件群組",
        cluster_ids,
        index=default_index,
        format_func=lambda cluster_id: (
            f"{cluster_id} · {cluster_lookup.loc[cluster_id, 'event_type']} · "
            f"{cluster_lookup.loc[cluster_id, 'keyword']} · "
            f"{pd.Timestamp(cluster_lookup.loc[cluster_id, 'first_seen']):%Y-%m-%d %H:%M}"
        ),
    )
    selected_cluster = cluster_lookup.loc[selected_cluster_id]
    stock_ids = selected_cluster["stock_ids"] or []
    st.markdown(f"**{selected_cluster_id} · {selected_cluster['keyword']}**")
    with st.container(horizontal=True):
        st.metric("事件分類", selected_cluster["event_type"] or "未分類")
        st.metric("首次熱搜", pd.Timestamp(selected_cluster["first_seen"]).strftime("%Y-%m-%d %H:%M"))
        st.metric("最後熱搜", pd.Timestamp(selected_cluster["last_seen"]).strftime("%Y-%m-%d %H:%M"))
        st.metric("持續時間", f"{selected_cluster['duration_hours']:.2f} 小時")
        st.metric("觀測次數", f"{int(selected_cluster['occurrence_count']):,}")
        st.metric("新聞數量", f"{int(selected_cluster['news_count']):,}")
        st.metric("最高熱度", f"{selected_cluster['max_traffic']:,.0f}")
    related_stocks = ", ".join(
        f"{stock_id} {get_stock_name(stock_id)}" for stock_id in stock_ids
    )
    st.markdown(f"**相關股票群：** {related_stocks or '無配對股票'}")

    news = load_trend_observations()
    news = news[news["trend_id"].isin(selected_cluster["trend_ids"])].copy()
    news["news_key"] = news["news_url"].fillna("").astype(str).str.strip()
    fallback_key = news["news_title"].fillna("").astype(str).str.strip() + "|" + news["news_source"].fillna("").astype(str).str.strip()
    news["news_key"] = news["news_key"].where(news["news_key"].ne(""), fallback_key)
    news = news[news["news_key"].str.strip("|").ne("")].drop_duplicates("news_key")
    st.subheader("群組新聞")
    if news.empty:
        show_empty("此事件群組沒有可顯示的新聞項目。")
    else:
        st.dataframe(
            news[["news_title", "news_source", "news_url"]].rename(columns={
                "news_title": "新聞標題", "news_source": "新聞來源", "news_url": "新聞連結",
            }),
            column_config={"新聞連結": st.column_config.LinkColumn(display_text="開啟")},
            hide_index=True,
        )

    aligned_returns = all_aligned_returns[
        all_aligned_returns["cluster_id"] == selected_cluster_id
    ].copy()
    comparison = trends.event_study.build_return_comparison(aligned_returns, selected_cluster_id)
    st.subheader("事件後股票反應")
    if comparison.empty:
        show_empty("行情尚未涵蓋完整事件後交易日，暫無可比較的報酬。")
    else:
        comparison_display = comparison.copy()
        comparison_display.insert(
            1,
            "股票名稱",
            comparison_display["stock_id"].map(get_stock_name),
        )
        comparison_display = comparison_display.rename(columns={
            "stock_id": "股票代號", "day1": "Day1", "day3": "Day3", "day5": "Day5", "day10": "Day10",
        })
        day1_leader = comparison.dropna(subset=["day1"]).head(1)
        day10_leader = comparison.dropna(subset=["day10"]).sort_values("day10", ascending=False).head(1)
        with st.container(horizontal=True):
            st.metric(
                "Day1 反應最快",
                f"{day1_leader.iloc[0]['stock_id']} {get_stock_name(day1_leader.iloc[0]['stock_id'])}"
                if not day1_leader.empty else "N/A",
                format_percent(day1_leader.iloc[0]["day1"]) if not day1_leader.empty else None,
            )
            st.metric(
                "Day10 報酬最高",
                f"{day10_leader.iloc[0]['stock_id']} {get_stock_name(day10_leader.iloc[0]['stock_id'])}"
                if not day10_leader.empty else "N/A",
                format_percent(day10_leader.iloc[0]["day10"]) if not day10_leader.empty else None,
            )
        st.dataframe(
            comparison_display,
            column_config={
                column: st.column_config.NumberColumn(format="percent")
                for column in ("Day1", "Day3", "Day5", "Day10")
            },
            hide_index=True,
        )

    if aligned_returns.empty:
        show_empty("這個事件群組目前沒有可對齊的研究期間行情。")
    else:
        return_figure = go.Figure()
        for stock_id, stock_path in aligned_returns.groupby("stock_id", sort=False):
            stock_path = stock_path.sort_values("day_offset")
            return_figure.add_trace(go.Scatter(
                x=stock_path["day_offset"],
                y=stock_path["relative_return"],
                mode="lines+markers",
                name=f"{stock_id} {get_stock_name(stock_id)}",
                customdata=stock_path["market_date"].astype(str),
                hovertemplate="%{fullData.name}<br>Day%{x}<br>交易日：%{customdata}<br>相對報酬：%{y:.2%}<extra></extra>",
            ))
        return_figure.update_layout(
            height=460,
            xaxis={"title": "事件後交易日", "tickmode": "array", "tickvals": [0, 1, 3, 5, 10]},
            yaxis={"title": "相對 Day0 報酬", "tickformat": ".1%", "zeroline": True},
            hovermode="closest",
            legend={"orientation": "h", "y": 1.08},
        )
        st.plotly_chart(return_figure, width="stretch")

        with st.expander("Event Alignment 明細"):
            aligned_display = aligned_returns.rename(columns={
                "cluster_id": "事件群組ID", "stock_id": "股票代號", "event_date": "事件日期",
                "market_date": "交易日", "day_offset": "交易日偏移", "close": "收盤價",
                "base_close": "Day0基準價", "relative_return": "相對報酬",
            })
            st.dataframe(
                aligned_display,
                column_config={"相對報酬": st.column_config.NumberColumn(format="percent")},
                hide_index=True,
            )


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
    "趨勢群組分析": page_stock_events,
    "還來得及上車嗎？": page_observation,
}


with st.sidebar:
    st.title("TRENDS / EQUITY")
    selected_page = st.radio("研究頁面", list(PAGES), label_visibility="collapsed")
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
    st.caption("行情排程：台北時間每日 18:00\n情緒分類模型首次執行時需下載")

PAGES[selected_page]()