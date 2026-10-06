from __future__ import annotations

import importlib
import inspect
from datetime import date

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from sqlalchemy import or_, select

import trends.event_study
from trends.database import (
    GoogleTrend,
    GoogleTrendNews,
    GoogleTrendsHistory,
    KeywordClassification,
    Stock,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.event_study import (
    build_event_research_overview,
    build_event_research_summary,
    build_event_research_text,
    build_hypothesis_validation,
    calculate_event_performance,
    detect_research_events,
    normalize_breakout_day,
)
from trends.etl import get_system_status, run_full_update
from trends.theme_study import build_theme_definitions, sync_theme_mapping


if "start_date" not in inspect.signature(trends.event_study.build_event_frame).parameters:
    trends.event_study = importlib.reload(trends.event_study)
if "start_date" not in inspect.signature(trends.event_study.build_event_frame).parameters:
    raise RuntimeError(
        f"{trends.event_study.__file__} build_event_frame does not accept start_date; "
        "restart Streamlit with the project .venv after verifying the imported module path."
    )

RESEARCH_START = pd.Timestamp(trends.event_study.RESEARCH_START_DATE)


def configure_app() -> None:
    st.set_page_config(page_title="Trends × Theme Research", page_icon="📈", layout="wide")
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;600;700&display=swap');
        :root { --ink: #20291f; --leaf: #2f6b4f; --paper: #f5f6ef; }
        html, body, [class*="css"] { font-family: 'DM Sans', 'Noto Sans TC', sans-serif; color: var(--ink); }
        .stApp { background: radial-gradient(ellipse at 92% 0%, #e5edd4 0, transparent 35%), var(--paper); }
        h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
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
            GoogleTrendNews.sentiment_score,
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
                GoogleTrend.fetched_at.is_(None)
                & (GoogleTrend.published_at >= RESEARCH_START.to_pydatetime()),
            )
        )
    )
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_prices(stock_id: str | None = None, start_date: date | None = None) -> pd.DataFrame:
    statement = select(Stock).where(
        Stock.date >= (start_date or RESEARCH_START.date())
    ).order_by(Stock.date)
    if stock_id:
        statement = statement.where(Stock.stock_id == stock_id)
    with database_engine().connect() as connection:
        return pd.read_sql(statement, connection)


@st.cache_data(ttl=300)
def load_theme_history(theme_name: str, lookback_days: int = 365) -> pd.DataFrame:
    start_date = (pd.Timestamp.now().normalize() - pd.Timedelta(days=lookback_days - 1)).date()
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


@st.cache_data(ttl=300)
def load_theme_definitions() -> dict[str, dict[str, list[str]]]:
    with database_engine().connect() as connection:
        mapping = pd.read_sql(select(ThemeMapping), connection)
    return build_theme_definitions(mapping)


@st.cache_data(ttl=300)
def load_theme_mapping_rows() -> pd.DataFrame:
    with database_engine().connect() as connection:
        return pd.read_sql(select(ThemeMapping), connection)


def clear_dashboard_caches() -> None:
    load_trend_observations.clear()
    load_prices.clear()
    load_theme_history.clear()
    load_theme_definitions.clear()
    load_theme_mapping_rows.clear()
    build_market_radar.clear()


def show_empty(message: str) -> None:
    st.info(message)


def summarize_sentiment_distribution(news: pd.DataFrame) -> dict[str, float]:
    if news.empty or "news_sentiment" not in news.columns:
        return {"positive_pct": 0.0, "neutral_pct": 0.0, "negative_pct": 0.0}
    sentiment = news["news_sentiment"].fillna("Neutral").str.title()
    counts = sentiment.value_counts(normalize=True) * 100
    distribution = {
        "positive_pct": float(counts.get("Positive", 0.0)),
        "neutral_pct": float(counts.get("Neutral", 0.0)),
        "negative_pct": float(counts.get("Negative", 0.0)),
    }
    return distribution


def format_summary_value(value: object) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def format_event_table_value(value: object, column_name: str | None = None) -> str:
    if value is None or pd.isna(value):
        return "資料尚不足" if column_name and any(token in column_name for token in ("報酬", "漲幅", "跌幅", "反應")) else "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if column_name == "首次反應天數":
        days = pd.to_numeric(value, errors="coerce")
        return f"{int(days)} 天" if pd.notna(days) else "資料尚不足"
    if isinstance(value, (float, int)):
        if column_name and any(token in column_name for token in ("報酬", "漲幅", "跌幅", "反應")):
            if column_name in {"MA20突破日", "MA60突破日"}:
                return normalize_breakout_day(value)
            return f"{float(value):.2%}"
        if column_name == "事件熱度":
            return f"{float(value):.0f}"
        if column_name in {"MA20突破日", "MA60突破日"}:
            return normalize_breakout_day(value)
        return str(value)
    return str(value)


def build_sentiment_verification_sample(news: pd.DataFrame) -> pd.DataFrame:
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
    sample["分析模型"] = "Gemini 3.6 Flash（中文新聞情緒分類）"
    sample["分析時間"] = pd.to_datetime(sample["分析時間"], errors="coerce").dt.strftime("%Y-%m-%d")
    return sample[["新聞標題", "新聞摘要", "情緒標籤", "信心分數", "分析模型", "分析時間"]].head(20)


def build_data_quality_check(performance: pd.DataFrame, news: pd.DataFrame) -> dict[str, object]:
    metrics = {
        "positive_pct": 0.0,
        "neutral_pct": 0.0,
        "negative_pct": 0.0,
        "reaction_days_valid_pct": 100.0,
        "ma20_warning": False,
        "ma60_warning": False,
        "sentiment_warning": False,
    }
    if not news.empty and "news_sentiment" in news.columns:
        sentiment = news["news_sentiment"].fillna("Neutral").str.title()
        counts = sentiment.value_counts(normalize=True) * 100
        metrics["positive_pct"] = float(counts.get("Positive", 0.0))
        metrics["neutral_pct"] = float(counts.get("Neutral", 0.0))
        metrics["negative_pct"] = float(counts.get("Negative", 0.0))
        if metrics["neutral_pct"] > 80:
            metrics["sentiment_warning"] = True
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
    news["date"] = pd.to_datetime(news["fetched_at"], errors="coerce").fillna(
        pd.to_datetime(news["published_at"], errors="coerce")
    ).dt.normalize()
    news["news_sentiment"] = news["news_sentiment"].fillna("Neutral").str.title()
    news["sentiment_score"] = pd.to_numeric(news.get("sentiment_score", pd.Series([None] * len(news))), errors="coerce")
    return news.dropna(subset=["date"])[columns]


def page_project_overview() -> None:
    st.title("Trends & Theme 市場事件分析平台")
    st.caption("熱門話題被大量討論時，相關股票是否仍有投資機會？以 Google Trends、新聞與股價資料建立可解釋的事件研究。")
    st.subheader("研究問題")
    st.write("本平台不預測股價，也不提供交易訊號；目標是驗證熱門事件與市場反應之間是否存在可觀察的關聯，以及反應發生的時間與主題差異。")

    st.subheader("研究假說")
    st.markdown(
        "1. 搜尋熱度明顯上升後，相關股票未來數個交易日出現正報酬的比例可能提高。\n"
        "2. 正面新聞的平均股價反應可能大於負面新聞。\n"
        "3. AI、科技、金融、能源、生技、消費與半導體等主題可能有不同反應速度。\n"
        "4. 搜尋熱度高峰與 MA20、MA60 均線突破之間可能存在時間差。"
    )

    st.subheader("資料流程")
    st.markdown(
        "Google Trends 熱搜\n\n↓\n\n熱門關鍵字與熱度\n\n↓\n\n相關新聞蒐集\n\n↓\n\nAI 關鍵字分類與新聞情緒分析\n\n↓\n\n主題與股票關聯建立\n\n↓\n\n股價資料下載\n\n↓\n\n事件資料集與事件報酬計算\n\n↓\n\n整合圖表與研究結果"
    )

    st.subheader("ETL 與系統架構")
    extract, transform, load = st.columns(3)
    with extract:
        st.markdown("**Extract**")
        st.write("Google Trends：關鍵字、熱度、時間\n\n新聞：標題、摘要、時間、來源\n\n股票：開高低收、成交量")
    with transform:
        st.markdown("**Transform**")
        st.write("關鍵字正規化\n\nAI 主題分類\n\n新聞情緒標註\n\n主題與股票映射\n\n事件觸發與報酬計算")
    with load:
        st.markdown("**Load**")
        st.write("SQLite `data.db`\n\n單機可攜，不依賴雲端資料庫；同一份資料支援收集、整理與視覺化分析。")

    st.graphviz_chart(
        '''digraph architecture {
            rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f4f7f0", color="#53745b"];
            trends [label="Google Trends"];
            news [label="新聞與情緒"];
            stocks [label="股票行情"];
            etl [label="分類、映射、事件 ETL"];
            sqlite [label="SQLite / data.db"];
            radar [label="市場雷達"];
            study [label="事件研究"];
            trends -> etl; news -> etl; stocks -> etl; etl -> sqlite; sqlite -> radar; sqlite -> study;
        }''',
        width="stretch",
    )

    st.subheader("核心資料表關聯")
    st.graphviz_chart(
        '''digraph schema {
            rankdir=LR; node [shape=record, style=filled, fillcolor="#f4f7f0", color="#53745b"];
            trends [label="{google_trends|trend_id|keyword|published_at}"];
            news [label="{google_trends_news|news_id|trend_id|news_sentiment}"];
            classify [label="{keyword_classification|keyword|theme_name|canonical_keyword}"];
            mapping [label="{theme_mapping|theme_name|keyword|stock_id}"];
            history [label="{google_trends_history|keyword|theme_name|trend_date|trend_score}"];
            stocks [label="{stocks|date|stock_id|OHLCV}"];
            events [label="{event_analysis|trend_id|news_id|stock_id|future returns}"];
            trends -> news [label="trend_id"];
            trends -> classify [label="keyword"];
            classify -> mapping [label="theme / keyword"];
            mapping -> history [label="theme / keyword"];
            mapping -> stocks [label="stock_id"];
            trends -> events [label="trend_id"];
            news -> events [label="news_id"];
            stocks -> events [label="stock_id + market date"];
        }''',
        width="stretch",
    )

    st.subheader("作品集價值")
    st.dataframe(pd.DataFrame([
        {"能力面向": "資料工程", "展示內容": "多來源 ETL、自動化收集、SQLite 資料模型"},
        {"能力面向": "資料分析", "展示內容": "事件研究、熱度變化、新聞情緒與股票報酬"},
        {"能力面向": "商業分析", "展示內容": "市場主題辨識、事件影響與市場反應驗證"},
        {"能力面向": "資料視覺化", "展示內容": "多來源整合圖、事件績效表與儀表板資訊設計"},
    ]), hide_index=True)


@st.cache_data(ttl=300)
def build_market_radar() -> pd.DataFrame:
    themes = load_theme_definitions()
    mappings = load_theme_mapping_rows()
    observations = load_trend_observations()
    all_prices = load_prices()
    rows = []
    for theme_name, definition in themes.items():
        keywords = definition["keywords"]
        stock_ids = definition["stock_ids"]
        history = load_theme_history(theme_name, lookback_days=365)
        history["trend_date"] = pd.to_datetime(history.get("trend_date"), errors="coerce")
        recent = history[history["trend_date"] >= pd.Timestamp.now().normalize() - pd.Timedelta(days=6)] if not history.empty else history
        previous = history[
            history["trend_date"].between(
                pd.Timestamp.now().normalize() - pd.Timedelta(days=13),
                pd.Timestamp.now().normalize() - pd.Timedelta(days=7),
            )
        ] if not history.empty else history
        recent_heat = pd.to_numeric(recent.get("trend_score"), errors="coerce").mean() if not recent.empty else float("nan")
        previous_heat = pd.to_numeric(previous.get("trend_score"), errors="coerce").mean() if not previous.empty else float("nan")
        heat_change = recent_heat / previous_heat - 1 if pd.notna(previous_heat) and previous_heat > 0 else pd.NA

        news = prepare_theme_news(observations, keywords)
        latest_theme_date = history["trend_date"].max().normalize() if not history.empty else pd.Timestamp.now().normalize()
        news = news[news["date"].between(latest_theme_date - pd.Timedelta(days=6), latest_theme_date)]
        positive = int(news["news_sentiment"].eq("Positive").sum())
        neutral = int(news["news_sentiment"].eq("Neutral").sum())
        negative = int(news["news_sentiment"].eq("Negative").sum())

        stock_returns = []
        if not all_prices.empty:
            for stock_id in stock_ids:
                prices = all_prices[all_prices["stock_id"].astype(str).eq(str(stock_id))].sort_values("date")
                closes = pd.to_numeric(prices["close"], errors="coerce").dropna()
                if len(closes) >= 11:
                    stock_returns.append(closes.iloc[-1] / closes.iloc[-11] - 1)
        return_10d = float(pd.Series(stock_returns).mean()) if stock_returns else pd.NA

        daily_heat = history.rename(columns={"trend_date": "date", "trend_score": "heat"})
        daily_heat = daily_heat[daily_heat["keyword"].isin(keywords)][["date", "keyword", "heat"]] if not history.empty else pd.DataFrame()
        news_for_events = news[["date", "keyword", "news_id"]] if not news.empty else pd.DataFrame()
        triggers = detect_research_events(daily_heat, news_for_events)
        mapping = mappings[
            mappings["theme_name"].eq(theme_name)
            & mappings["active"].astype(str).str.casefold().isin(("1", "true"))
            & mappings["stock_id"].astype(str).isin(stock_ids)
        ]
        if not triggers.empty and not mapping.empty and not all_prices.empty:
            triggers = triggers.sort_values("event_date", ascending=False).head(50)
            representative_stocks = mapping[["keyword", "stock_id"]].drop_duplicates("keyword")
            triggers = triggers.merge(representative_stocks, on="keyword", how="inner")
            theme_prices = all_prices[all_prices["stock_id"].astype(str).isin(stock_ids)]
            response = calculate_event_performance(triggers, theme_prices)
            response_days = pd.to_numeric(response.get("reaction_days"), errors="coerce").dropna()
            average_reaction = f"{response_days.mean():.1f} 日" if not response_days.empty else "樣本不足"
        else:
            average_reaction = "樣本不足"

        rows.append({
            "主題": theme_name,
            "熱度變化率": heat_change,
            "新聞數": len(news),
            "正面": positive,
            "中性": neutral,
            "負面": negative,
            "相關股票": ", ".join(stock_ids),
            "平均首次反應天數": average_reaction,
            "近 10 日表現": return_10d,
        })
    return pd.DataFrame(rows)


def page_market_radar() -> None:
    st.title("市場雷達")
    st.caption("比較近期主題熱度、新聞情緒與相關股票表現；主題依分類呈現，不代表投資排名。")
    radar = build_market_radar()
    if radar.empty:
        show_empty("尚無主題映射資料，請確認 theme_mapping.csv。")
        return
    st.caption("熱度變化比較最近 7 日與前 7 日平均；近 10 日表現為主題關聯股票等權平均。平均首次反應天數取每主題最近最多 50 個事件、每個關鍵字一檔映射股票；首次反應天數為事件後 1–30 個交易日內，收盤價相對事件日收盤首次變動達 ±1% 的交易日數，僅作為描述性觀察指標。")
    st.dataframe(
        radar,
        column_config={
            "熱度變化率": st.column_config.NumberColumn(format="percent"),
            "近 10 日表現": st.column_config.NumberColumn(format="percent"),
        },
        hide_index=True,
    )


def page_event_study() -> None:
    st.title("事件研究")
    st.caption("觀察新聞與搜尋熱度事件出現後，股價、均線與報酬如何變化。這是歷史關聯研究，不是股價預測。")
    themes = load_theme_definitions()
    if not themes:
        show_empty("尚未設定主題映射，請確認 theme_mapping.csv。")
        return
    mappings = load_theme_mapping_rows()
    with st.container(horizontal=True):
        theme_name = st.selectbox("主題", list(themes), key="study_theme")
        theme_mapping = mappings[mappings["theme_name"].eq(theme_name)]
        stock_ids = list(dict.fromkeys(theme_mapping["stock_id"].dropna().astype(str)))
        if not stock_ids:
            stock_ids = themes[theme_name]["stock_ids"]
        stock_id = st.selectbox("股票", stock_ids, key="study_stock")

    history = load_theme_history(theme_name, lookback_days=365)
    warmup_start = (RESEARCH_START - pd.Timedelta(days=100)).date()
    prices = load_prices(stock_id, warmup_start)
    if history.empty or prices.empty:
        show_empty("此主題或股票尚缺歷史熱度／行情資料，請先執行資料更新。")
        return

    visible_prices = prices[pd.to_datetime(prices["date"], errors="coerce") >= RESEARCH_START]
    date_values = pd.concat([
        pd.to_datetime(history["trend_date"], errors="coerce"),
        pd.to_datetime(visible_prices["date"], errors="coerce"),
    ]).dropna()
    minimum_date = max(date_values.min().date(), RESEARCH_START.date())
    maximum_date = date_values.max().date()
    default_start = max(minimum_date, maximum_date - pd.Timedelta(days=89))
    date_range = st.date_input(
        "日期區間",
        value=(default_start, maximum_date),
        min_value=minimum_date,
        max_value=maximum_date,
        key="study_date_range",
    )
    if not isinstance(date_range, tuple) or len(date_range) != 2:
        show_empty("請選擇完整的開始與結束日期。")
        return
    start_date, end_date = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])

    theme_keywords = themes[theme_name]["keywords"]
    selected_mapping = theme_mapping[
        theme_mapping["stock_id"].astype(str).eq(str(stock_id))
        & theme_mapping["active"].astype(str).str.casefold().isin(("1", "true"))
    ]
    selected_keywords = selected_mapping["keyword"].dropna().astype(str).tolist() or theme_keywords
    history = history[history["keyword"].isin(selected_keywords)].copy()
    history["date"] = pd.to_datetime(history["trend_date"], errors="coerce").dt.normalize()
    daily_heat = history.rename(columns={"trend_score": "heat"})[["date", "keyword", "heat"]]
    observations = load_trend_observations()
    news = prepare_theme_news(observations, selected_keywords)
    news_for_events = news[["date", "keyword", "news_id"]] if not news.empty else news
    triggers = detect_research_events(daily_heat, news_for_events)
    triggers = triggers[triggers["event_date"].between(start_date, end_date)]
    if not triggers.empty:
        triggers["theme"] = theme_name
        triggers["stock_id"] = str(stock_id)
        sentiment_counts = news.groupby(["date", "keyword", "news_sentiment"]).size()
        triggers["news_sentiment"] = triggers.apply(
            lambda row: ", ".join(
                f"{sentiment} {int(count)}"
                for sentiment, count in sentiment_counts.get(
                    (row["event_date"], row["keyword"]), pd.Series(dtype=int)
                ).items()
            ) or "無新聞",
            axis=1,
        )
    performance = calculate_event_performance(triggers, prices)
    summary = build_event_research_summary(performance)
    sentiment_distribution = summarize_sentiment_distribution(news)

    prices["date"] = pd.to_datetime(prices["date"], errors="coerce").dt.normalize()
    prices = prices.sort_values("date").copy()
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices["ma20"] = prices["close"].rolling(20).mean()
    prices["ma60"] = prices["close"].rolling(60).mean()
    chart_prices = prices[prices["date"].between(start_date, end_date)]
    chart_heat = daily_heat[daily_heat["date"].between(start_date, end_date)]
    theme_series = chart_heat.groupby("date", as_index=False)["heat"].mean()
    heat_lookup = theme_series.set_index("date")["heat"] if not theme_series.empty else pd.Series(dtype=float)
    chart_news = news[news["date"].between(start_date, end_date)].copy()
    if not chart_news.empty:
        chart_news["marker_heat"] = chart_news.apply(
            lambda row: heat_lookup.get(row["date"], 100), axis=1
        )

    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(go.Scatter(
        x=chart_prices["date"], y=chart_prices["close"], name="股價",
        mode="lines", line={"color": "#202522", "width": 2.4},
    ), secondary_y=False)
    figure.add_trace(go.Scatter(
        x=chart_prices["date"], y=chart_prices["ma20"], name="MA20",
        mode="lines", line={"color": "#e28a32", "width": 1.7},
    ), secondary_y=False)
    figure.add_trace(go.Scatter(
        x=chart_prices["date"], y=chart_prices["ma60"], name="MA60",
        mode="lines", line={"color": "#89918f", "width": 1.7},
    ), secondary_y=False)
    figure.add_trace(go.Scatter(
        x=theme_series["date"], y=theme_series["heat"], name="Google Trends 熱度",
        mode="lines", line={"color": "#2878b5", "width": 2.2},
    ), secondary_y=True)
    sentiment_colors = {"Positive": "#228b55", "Neutral": "#e2b52d", "Negative": "#d64b45"}
    for sentiment, color in sentiment_colors.items():
        subset = chart_news[chart_news["news_sentiment"].eq(sentiment)]
        if subset.empty:
            continue
        figure.add_trace(go.Scatter(
            x=subset["date"], y=subset["marker_heat"], mode="markers", name=f"新聞：{sentiment}",
            marker={"color": color, "size": 9, "symbol": "circle"},
            customdata=subset[["news_title", "news_source", "keyword"]],
            hovertemplate="%{x|%Y-%m-%d}<br>%{customdata[2]}<br>%{customdata[0]}<br>%{customdata[1]}<extra></extra>",
        ), secondary_y=True)
    event_dates = (
        triggers.sort_values("event_date")["event_date"].drop_duplicates().tail(12)
        if not triggers.empty
        else []
    )
    event_shapes = [
        {
            "type": "line", "xref": "x", "x0": event_date, "x1": event_date,
            "yref": "paper", "y0": 0, "y1": 1,
            "line": {"color": "#5e6e64", "dash": "dash", "width": 1}, "opacity": 0.65,
        }
        for event_date in event_dates
    ]
    figure.update_layout(
        height=560,
        template="plotly_white",
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.12},
        margin={"t": 50, "b": 20},
        shapes=event_shapes,
    )
    figure.update_xaxes(title_text="日期")
    figure.update_yaxes(title_text="股價與均線", secondary_y=False)
    figure.update_yaxes(title_text="搜尋熱度（0–100）", range=[0, 110], secondary_y=True)
    st.plotly_chart(figure, width="stretch")
    st.caption("本研究目的是驗證熱門事件是否會影響後續股價反應，而不是預測交易訊號。垂直虛線標示事件日，新聞點顏色代表情緒，股價與熱度使用不同座標軸。")

    if performance.empty:
        show_empty("此條件下沒有符合事件門檻且可配對行情的事件。")
        return

    st.subheader("研究結果摘要")
    st.markdown(build_event_research_overview(theme_name, start_date, end_date, performance))
    summary_items = [
        ("事件數量", summary["event_count"]),
        ("正報酬比例", summary["positive_return_ratio_pct"]),
        ("負報酬比例", summary["negative_return_ratio_pct"]),
        ("平均首次反應天數", summary["avg_reaction_days"]),
        ("最佳事件", summary["best_event"]),
        ("最差事件", summary["worst_event"]),
        ("平均1日報酬", summary["avg_return_1d_pct"]),
        ("平均3日報酬", summary["avg_return_3d_pct"]),
        ("平均5日報酬", summary["avg_return_5d_pct"]),
        ("平均10日報酬", summary["avg_return_10d_pct"]),
    ]
    metric_cols = st.columns(3)
    for index, (label, value) in enumerate(summary_items):
        with metric_cols[index % 3]:
            rendered = format_summary_value(value)
            if isinstance(value, (int, float)) and not pd.isna(value):
                if label in {"事件數量"}:
                    rendered = str(int(value))
                elif label in {"平均首次反應天數"}:
                    rendered = f"{float(value):.1f} 天"
                elif label.startswith("平均") or label.endswith("比例"):
                    rendered = f"{float(value):.1f}%"
            st.metric(label, rendered)

    st.subheader("Hypothesis Validation")
    for hypothesis in build_hypothesis_validation(performance):
        status_color = "green" if hypothesis["status"] == "成立" else "orange" if hypothesis["status"] == "部分成立" else "red" if hypothesis["status"] == "不成立" else "gray"
        st.markdown(f"### {hypothesis['title']}")
        st.markdown(f"**驗證結果：** <span style='color:{status_color};font-weight:700'>{hypothesis['status']}</span>", unsafe_allow_html=True)
        st.markdown(f"**依據：** {hypothesis['basis']}")

    st.subheader("情緒分析驗證")
    if not news.empty:
        sentiment_cols = st.columns(3)
        with sentiment_cols[0]:
            st.metric("Positive %", f"{sentiment_distribution['positive_pct']:.1f}%")
        with sentiment_cols[1]:
            st.metric("Neutral %", f"{sentiment_distribution['neutral_pct']:.1f}%")
        with sentiment_cols[2]:
            st.metric("Negative %", f"{sentiment_distribution['negative_pct']:.1f}%")
        if sentiment_distribution["neutral_pct"] > 80:
            st.warning("Neutral 占比超過 80%，請檢查情緒分析流程是否失效、是否僅使用英文模型、是否新聞內容未正確傳入分析模組。")
        sentiment_table = build_sentiment_verification_sample(news)
        st.dataframe(sentiment_table, hide_index=True)
    else:
        st.info("目前此範圍內沒有可驗證的新聞情緒資料。")

    st.subheader("事件解讀")
    event_options = performance.sort_values("event_date", ascending=False).copy()
    event_options["label"] = event_options.apply(
        lambda row: f"{row['keyword']} ({pd.to_datetime(row['event_date']).strftime('%Y-%m-%d')})",
        axis=1,
    )
    selected_label = st.selectbox("選擇事件", event_options["label"].tolist(), index=0, key="event_summary_selection")
    selected_event = event_options[event_options["label"].eq(selected_label)].iloc[0]
    st.info(build_event_research_text(selected_event, performance))

    st.subheader("事件研究結論")
    st.markdown(
        "本頁面聚焦於研究問題：熱門事件是否會改變市場對相關股票的後續反應。"
        " 事件訊號不等於交易建議，而是用來驗證 Google Trends、新聞與情緒是否能對股價形成可觀察影響。"
    )

    st.subheader("Data Quality Check")
    quality = build_data_quality_check(performance, news)
    st.metric("有效首次反應天數比例", f"{quality['reaction_days_valid_pct']:.1f}%")
    if quality["sentiment_warning"]:
        st.warning("情緒分析可能異常：Neutral 佔比超過 80%，請檢查中文新聞是否正確送入情緒模型。")
    if quality["ma20_warning"] or quality["ma60_warning"]:
        st.warning("突破日分布異常：MA20 或 MA60 突破日高度集中於同一天，建議檢查均線突破邏輯與事件時間對齊是否正確。")

    st.subheader("事件績效")
    display = performance.rename(columns={
        "event_date": "事件日期", "keyword": "關鍵字", "theme": "主題", "stock_id": "股票",
        "news_sentiment": "新聞情緒", "event_heat": "事件熱度", "return_1d": "1日報酬",
        "return_3d": "3日報酬", "return_5d": "5日報酬", "return_10d": "10日報酬",
        "max_gain_10d": "最大漲幅", "max_loss_10d": "最大跌幅",
        "reaction_days": "首次反應天數",
        "ma20_breakout_day": "MA20突破日",
        "ma60_breakout_day": "MA60突破日",
    }).copy()
    for column in display.columns:
        display[column] = display[column].map(lambda value: format_event_table_value(value, str(column)))
    display = display[[
        "事件日期", "關鍵字", "主題", "股票", "新聞情緒", "事件熱度", "1日報酬", "3日報酬",
        "5日報酬", "10日報酬", "最大漲幅", "最大跌幅", "首次反應天數", "MA20突破日", "MA60突破日",
    ]]
    st.dataframe(display, hide_index=True)


def main() -> None:
    configure_app()
    pages = st.navigation(
        [
            st.Page(page_project_overview, title="專案介紹", icon=":material/info:"),
            st.Page(page_market_radar, title="市場雷達", icon=":material/radar:"),
            st.Page(page_event_study, title="事件研究", icon=":material/query_stats:"),
        ],
        position="top",
    )

    with st.sidebar:
        st.title("TRENDS / THEME")
        status = get_system_status(database_engine())
        st.caption(f"資料狀態：{status['health_label']}")
        trend_time = status["latest_trend_at"]
        st.caption(f"最後更新：{trend_time:%Y-%m-%d %H:%M}" if trend_time else "尚無 Trends 資料")
        st.caption(
            f"關鍵字 {int(status['total_keywords']):,}｜新聞 {int(status['total_news']):,}\n\n"
            f"股票行情 {int(status['stock_rows']):,} 筆｜主題 {int(status['theme_count']):,} 個"
        )
        if st.button("更新研究資料", type="primary", icon=":material/refresh:", width="stretch"):
            try:
                with st.spinner("更新行情、關鍵字分類與新聞情緒…"):
                    result = run_full_update(database_engine())
                clear_dashboard_caches()
                st.session_state["last_update_result"] = result
                st.rerun()
            except Exception as error:
                clear_dashboard_caches()
                st.error(f"資料更新未完成：{error}")
        update_result = st.session_state.pop("last_update_result", None)
        if update_result:
            st.success(
                f"已更新行情 {update_result['stock_rows']:,} 筆，分類關鍵字 {update_result['classified_keywords']:,} 筆，"
                f"分析新聞 {update_result['classified_news']:,} 篇"
            )

    pages.run()