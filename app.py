from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import select

from trends.alpha_signal import parse_traffic, score_signals
from trends.database import GoogleTrend, GoogleTrendNews, Stock, get_engine, init_db
from trends.event_study import build_event_frame, summarize_event_study
from trends.keyword_mapping import load_keyword_mapping
from trends.sentiment import analyze_pending_news
from trends.stock_collector import fetch_and_store


st.set_page_config(page_title="Trends × 台股事件研究", page_icon="📈", layout="wide")
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
        return pd.read_sql(select(GoogleTrend), connection)


@st.cache_data(ttl=300)
def load_news() -> pd.DataFrame:
    with database_engine().connect() as connection:
        return pd.read_sql(
            select(
                GoogleTrendNews.id,
                GoogleTrendNews.trend_id,
                GoogleTrendNews.news_title,
                GoogleTrendNews.news_source,
                GoogleTrendNews.news_url,
                GoogleTrendNews.news_sentiment,
                GoogleTrend.published_at,
                GoogleTrend.keyword,
            ).join(GoogleTrend, GoogleTrend.id == GoogleTrendNews.trend_id),
            connection,
        )


@st.cache_data(ttl=300)
def load_prices(stock_id: str | None = None) -> pd.DataFrame:
    statement = select(Stock).order_by(Stock.date)
    if stock_id:
        statement = statement.where(Stock.stock_id == stock_id)
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_events() -> pd.DataFrame:
    return build_event_frame(database_engine())


def show_empty(message: str) -> None:
    st.info(message)


def page_home() -> None:
    st.title("Google Trends × 台股")
    st.caption("事件驅動研究工作台　/　台灣市場・每日更新")
    trends = load_trends()
    events = load_events()
    prices = load_prices()
    latest_fetch = pd.to_datetime(trends["fetched_at"], errors="coerce").max() if not trends.empty else None
    metrics = st.columns(4)
    metrics[0].metric("追蹤關鍵字", f"{trends['keyword'].nunique():,}" if not trends.empty else "0")
    metrics[1].metric("已配對事件", f"{len(events):,}")
    metrics[2].metric("涵蓋股票", f"{prices['stock_id'].nunique():,}" if not prices.empty else "0")
    metrics[3].metric("最近 RSS 抓取", latest_fetch.strftime("%m/%d %H:%M") if pd.notna(latest_fetch) else "尚無資料")

    st.subheader("熱門關鍵字")
    if trends.empty:
        show_empty("尚無 Google Trends RSS 資料。先執行既有的 RSS 排程，資料會寫入根目錄 data.db。")
        return
    ranking = trends.copy()
    ranking["熱度"] = ranking["approx_traffic"].map(parse_traffic)
    ranking = ranking.sort_values("published_at").drop_duplicates("keyword", keep="last")
    ranking = ranking.sort_values("熱度", ascending=False).head(15)
    chart = go.Figure(go.Bar(x=ranking["熱度"], y=ranking["keyword"], orientation="h", marker_color="#2f6b4f"))
    chart.update_layout(height=440, yaxis={"autorange": "reversed"}, margin={"l": 10, "r": 20, "t": 10, "b": 10})
    st.plotly_chart(chart, use_container_width=True)
    st.dataframe(ranking[["keyword", "approx_traffic", "published_at"]], hide_index=True, use_container_width=True)


def page_stocks() -> None:
    st.title("股票分析")
    prices = load_prices()
    if prices.empty:
        show_empty("尚無股價資料。使用左側「更新股價」載入追蹤清單，或執行每日行情收集器。")
        return
    stock_id = st.selectbox("股票代號", sorted(prices["stock_id"].unique()))
    frame = prices[prices["stock_id"] == stock_id].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    mappings = load_keyword_mapping()
    related = [alias for alias, mapped_id in mappings.items() if mapped_id == stock_id]
    trends = load_trends()
    trends = trends[trends["keyword"].isin(related)].copy() if not trends.empty else trends

    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Candlestick(x=frame["date"], open=frame["open"], high=frame["high"], low=frame["low"], close=frame["close"], name="OHLC"),
        secondary_y=False,
    )
    if not trends.empty:
        trends["熱度"] = trends["approx_traffic"].map(parse_traffic)
        figure.add_trace(
            go.Scatter(x=pd.to_datetime(trends["fetched_at"]), y=trends["熱度"], mode="lines+markers", name="Trends 熱度", line={"color": "#db795e"}),
            secondary_y=True,
        )
    figure.update_layout(height=560, xaxis_rangeslider_visible=False, legend={"orientation": "h", "y": 1.08})
    figure.update_yaxes(title_text="股價", secondary_y=False)
    figure.update_yaxes(title_text="RSS 熱度（估算）", secondary_y=True)
    st.plotly_chart(figure, use_container_width=True)
    st.dataframe(frame.sort_values("date", ascending=False).head(20), hide_index=True, use_container_width=True)


def page_news() -> None:
    st.title("新聞事件分析")
    events = load_events()
    if events.empty:
        show_empty("尚無可分析的事件。需有匹配到關鍵字的 RSS 新聞，以及對應股票行情。")
        news = load_news()
        if not news.empty:
            st.dataframe(news, hide_index=True, use_container_width=True)
        return
    timeline = go.Figure(
        go.Scatter(
            x=events["event_date"], y=events["stock_id"], mode="markers",
            marker={"size": 11, "color": events["news_sentiment"].map({"Positive": "#5d9c6b", "Neutral": "#d2a844", "Negative": "#db795e"}).fillna("#879084")},
            text=events["news_title"], hovertemplate="%{x|%Y-%m-%d}<br>%{y}<br>%{text}<extra></extra>",
        )
    )
    timeline.update_layout(height=300, xaxis_title="事件日期", yaxis_title="股票", margin={"l": 10, "r": 10, "t": 10, "b": 10})
    st.plotly_chart(timeline, use_container_width=True)
    display = events.sort_values("event_date", ascending=False)
    st.dataframe(
        display[["event_date", "stock_id", "keyword", "news_title", "news_source", "news_sentiment", "news_url"]],
        column_config={"news_url": st.column_config.LinkColumn("新聞連結", display_text="開啟")},
        hide_index=True,
        use_container_width=True,
    )
    if st.button("分析尚未分類的新聞情緒", icon=":material/mood:"):
        with st.spinner("載入多語情緒模型並分析新聞標題…"):
            count = analyze_pending_news(database_engine())
        load_news.clear()
        load_events.clear()
        st.success(f"完成 {count} 筆新聞情緒分類")
        st.rerun()


def page_alpha() -> None:
    st.title("Alpha Signal")
    st.caption("研究排序指標，不構成投資建議。高分代表關注度與市場反應較強，不代表預期報酬較高。")
    signals = score_signals(load_events())
    if signals.empty:
        show_empty("事件樣本不足，尚無信號可計算。")
        return
    st.dataframe(
        signals[["event_date", "stock_id", "keyword", "news_sentiment", "signal_score", "signal", "future_return_5d"]],
        column_config={"future_return_5d": st.column_config.NumberColumn("事件後 5 日報酬", format="percent")},
        hide_index=True,
        use_container_width=True,
    )


def page_conclusion() -> None:
    st.title("研究結論")
    events = load_events()
    summary = summarize_event_study(events)
    count = len(events)
    five_day = summary.loc[summary["holding_days"] == 5].iloc[0]
    if count:
        best = summary.dropna(subset=["avg_return_pct"]).sort_values("avg_return_pct", ascending=False).iloc[0]
        metrics = st.columns(4)
        metrics[0].metric("總事件數", f"{count:,}")
        metrics[1].metric("5 日勝率", f"{five_day['win_rate_pct']:.1f}%" if pd.notna(five_day["win_rate_pct"]) else "N/A")
        metrics[2].metric("最佳持有天數", f"{int(best['holding_days'])} 日")
        metrics[3].metric("最佳平均報酬", f"{best['avg_return_pct']:.2f}%")
    else:
        show_empty("資料累積後會顯示事件數、勝率、最佳持有天數及平均報酬。")
    st.subheader("持有期間統計")
    st.dataframe(
        summary,
        column_config={
            "avg_return_pct": st.column_config.NumberColumn("平均報酬 (%)", format="%.2f"),
            "win_rate_pct": st.column_config.NumberColumn("勝率 (%)", format="%.1f"),
            "max_gain_pct": st.column_config.NumberColumn("最大漲幅 (%)", format="%.2f"),
            "max_loss_pct": st.column_config.NumberColumn("最大跌幅 (%)", format="%.2f"),
        },
        hide_index=True,
        use_container_width=True,
    )


PAGES = {
    "首頁": page_home,
    "股票分析": page_stocks,
    "新聞事件分析": page_news,
    "Alpha Signal": page_alpha,
    "研究結論": page_conclusion,
}


with st.sidebar:
    st.title("TRENDS / EQUITY")
    selected_page = st.radio("研究頁面", list(PAGES), label_visibility="collapsed")
    st.divider()
    if st.button("更新股價", icon=":material/sync:", use_container_width=True):
        with st.spinner("下載最近行情…"):
            row_count = fetch_and_store()
        load_prices.clear()
        load_events.clear()
        st.success(f"更新 {row_count:,} 筆")
        st.rerun()
    st.caption("行情排程：台北時間每日 18:00\n情緒分類模型首次執行時需下載")

PAGES[selected_page]()