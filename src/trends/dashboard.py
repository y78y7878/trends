from __future__ import annotations

import importlib
import inspect
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from sqlalchemy import String, func, select, type_coerce

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
from trends.timeutil import (
    format_db_datetime,
    taipei_day_start_utc,
    taipei_today,
    utc_series_to_taipei_date,
    utc_to_taipei,
)


if "start_date" not in inspect.signature(trends.event_study.build_event_frame).parameters:
    trends.event_study = importlib.reload(trends.event_study)
if "start_date" not in inspect.signature(trends.event_study.build_event_frame).parameters:
    raise RuntimeError(
        f"{trends.event_study.__file__} build_event_frame does not accept start_date; "
        "restart Streamlit with the project .venv after verifying the imported module path."
    )

RESEARCH_START = pd.Timestamp(trends.event_study.RESEARCH_START_DATE)
ANALYZED_SENTIMENTS = ("Positive", "Neutral", "Negative")
UNANALYZED = "Unanalyzed"
SENTIMENT_LABELS = {UNANALYZED: "未分析"}
SENTIMENT_COVERAGE_MIN_PCT = 20.0
LOW_SENTIMENT_COVERAGE_MESSAGE = "情緒樣本不足，請謹慎解讀"


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
            # Stored values are canonical naive-UTC strings; compare as text against the UTC instant
            # at which the research start day begins in Asia/Taipei.
            type_coerce(func.coalesce(GoogleTrend.published_at, GoogleTrend.fetched_at), String)
            >= format_db_datetime(taipei_day_start_utc(RESEARCH_START.date()))
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
    start_date = taipei_today() - timedelta(days=lookback_days - 1)
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
    sample["分析模型"] = "Gemini 3.6 Flash（中文新聞情緒分類）"
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


def render_overview_cards(cards: list[tuple[str, str]]) -> None:
    for column, (title, body) in zip(st.columns(len(cards)), cards):
        with column, st.container(border=True):
            st.markdown(f"**{title}**")
            st.markdown(body)


def page_project_overview() -> None:
    st.title("Python 自動化資料分析平台")
    st.markdown("#### 從資料收集、清洗、AI 分析到 Dashboard 展示的自動化資料系統")
    st.markdown("**專案成果摘要**")
    render_overview_cards([
        ("✔ 開發期間：至今約1.5 個月", "尚未完成所有功能，持續開發中"),
        ("✔ Python", "Pandas 資料處理與自動化"),
        ("✔ ETL", "自動收集、清洗，集中存入 SQLite"),
    ])
    render_overview_cards([
        ("✔ Dashboard", "Streamlit 互動式報表"),
        ("✔ AI 分析", "Gemini 自動分類與情緒判讀"),
        ("✔ Git 版本控制", "完整保留開發與修正紀錄"),
    ])

    st.divider()
    st.subheader("我的能力成長路線")
    st.graphviz_chart(
        '''digraph growth {
            rankdir=LR; node [shape=box, style="rounded,filled", color="#53745b"]; edge [color="#53745b"];
            warehouse [label="倉管／出貨管理", fillcolor="#e9ece4"];
            erp [label="ERP 維護", fillcolor="#e9ece4"];
            vba [label="Excel VBA 自動化", fillcolor="#dfe9d3"];
            python [label="Python 資料處理", fillcolor="#d3e4c9"];
            etl [label="ETL 流程設計", fillcolor="#c6dcbd"];
            dashboard [label="Dashboard 開發", fillcolor="#b6d2ad"];
            ai [label="AI 資料分析", fillcolor="#2f6b4f", fontcolor="#ffffff"];
            warehouse -> erp -> vba -> python -> etl -> dashboard -> ai;
        }''',
        width="stretch",
    )
    st.markdown(
        "過去主要負責訂單管理、出貨流程與 ERP 維護。"
        "利用 Excel VBA 自動化訂單與出貨流程，將每日人工處理時間由約 3 至 4 小時縮短至 5 至 10 分鐘。\n\n"
        "後續進一步學習 Python，將自動化能力擴展至資料工程、Dashboard 與 AI 應用。"
    )

    st.divider()
    st.subheader("為什麼開發這個專案？")
    st.markdown("從接觸股票市場以來，每當看到某檔股票的相關新聞時，腦中都會出現幾個問題：")
    st.markdown(
        "> 現在看到新聞才知道這件事，是不是已經太晚了？\n>\n"
        "> 這則新聞出現時，市場是否早就已經反應？\n>\n"
        "> 一般投資人在看到新聞的當下，收到的資訊可能已經是二手、甚至三手以上的資訊。"
    )
    st.markdown(
        "因此開始思考：是否能透過資料分析的方式，驗證熱門話題、新聞討論熱度與市場反應之間是否存在規律。\n\n"
        "在職訓期間學習 Python、資料分析、資料庫、視覺化報表與 AI 應用後，決定把這個長期存在的疑問轉換成實際專案。\n\n"
        "本專案希望建立一套能夠「自動收集資料 → 分析資料 → 呈現資料」的流程，"
        "並透過客觀數據觀察：當話題升溫後，市場是否產生明顯反應。\n\n"
        "希望透過數據驗證，協助一般人更理性理解新聞、市場關注度與市場反應之間的關係。"
    )
    st.markdown(
        "本專案研究的重點不是單純觀察哪些關鍵字上榜，而是觀察：哪些話題出現異常升溫，"
        "以及這些異常訊號之後，市場是否產生明顯反應。"
    )

    st.divider()
    st.subheader("系統全貌")
    st.graphviz_chart(
        r'''digraph pipeline {
            rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f4f7f0", color="#53745b", margin="0.25,0.15"];
            edge [color="#53745b"];
            collect [label="① 自動收集\n\nGoogle Trends 熱搜 RSS（含相關新聞）\nGoogle Trends 每日搜尋熱度\n(pytrends)\n股票行情\n(yfinance)"];
            clean [label="② 清洗整合\n\n去除重複\n時間統一\n日期對齊", fillcolor="#dfe9d3"];
            store [label="③ 集中儲存\n\nSQLite"];
            analyze [label="④ AI 分析\n\nGemini 主題分類\n情緒分析", fillcolor="#dfe9d3"];
            present [label="⑤ Dashboard\n\n市場雷達\n事件研究"];
            collect -> clean -> store -> analyze -> present;
        }''',
        width="stretch",
    )
    st.info("整個流程可透過排程自動執行，大幅降低資料收集、整理與分析的人工作業成本。", icon=":material/schedule:")

    st.divider()
    st.subheader("成果展示")
    st.markdown("從畫面可以直接得到的資訊：")
    radar, study = st.columns(2)
    with radar, st.container(border=True):
        st.markdown("**市場雷達：現在市場在關注什麼？**")
        st.markdown(
            "- **各主題搜尋熱度與新聞量概況**：觀察不同主題近期的搜尋熱度變化、新聞討論量與相關股票表現\n"
            "- **搜尋熱度變化**：比較近 7 日與前 7 日的 Google 搜尋熱度差異\n"
            "- **新聞情緒分布**：正面、中性、負面新聞各占多少\n"
            "- **對應股票表現**：相關股票近 10 日的漲跌"
        )
        st.caption(
            "注意：搜尋熱度並不等於事件。只有當搜尋熱度相較於過去平均明顯上升，"
            "或新聞量明顯增加時，系統才會判定為事件。"
        )
    with study, st.container(border=True):
        st.markdown("**事件研究：事件發生後，市場怎麼反應？**")
        st.markdown(
            "- **事件發生日**：搜尋熱度或新聞量明顯暴增的日期\n"
            "- **搜尋熱度變化**：事件前後的關注度起伏\n"
            "- **情緒變化**：當時新聞偏正面還是偏負面\n"
            "- **市場反應**：事件後 1、3、5、10 個交易日的股價變化"
        )
        st.caption(
            "事件（Event）不等於熱搜關鍵字。事件定義為：搜尋熱度相較過去平均明顯提升，"
            "或相關新聞量異常增加時，所產生的異常訊號。"
        )
    st.caption("事件研究將股價、搜尋熱度、新聞情緒與事件日期整合於同一張圖，幫助快速理解事件發生後的變化。請由上方導覽列切換至「市場雷達」與「事件研究」查看實際畫面。")

    st.divider()
    st.subheader("資料品質與 ETL")
    render_overview_cards([
        ("1. 去除重複", "同一筆熱搜中的相同新聞網址只保留一筆，避免重複收集造成分析失真"),
        ("2. 時間統一", "國際標準時間統一換算為台灣日期"),
        ("3. 日期對齊", "非交易日自動對齊至下一個交易日"),
        ("4. 未分析分離", "未分析資料不等於中性"),
    ])
    st.markdown("> **資料工程的核心不是取得資料，而是讓資料值得被信任。**")

    st.divider()
    st.subheader("AI 實務應用")
    render_overview_cards([
        ("AI 協助分類", "大量熱門關鍵字自動歸入科技類、金融類、生技醫療類等主題，不必人工逐筆歸類"),
        ("AI 協助判讀", "AI 根據新聞標題與新聞來源，協助判斷新聞傾向為：\n- 正面\n- 中性\n- 負面"),
        ("AI 提升效率", "大量新聞批次交給 AI 處理，不必逐篇閱讀整理"),
        ("AI 降低人工成本", "人員只需抽查與判斷結果；AI 用量依額度分批控管"),
    ])
    st.caption("品質控管：畫面會顯示 AI 已分析的比例，尚未分析的資料另外標示，不會自行補上結果。")
    st.markdown("> **不只使用 AI，更管理 AI 結果品質與使用成本。**")

    st.divider()
    st.subheader("問題與解決方案")
    render_overview_cards([
        ("案例 1：重複資料", "**問題：** 重複資料\n\n**解法：** 唯一條件與去重機制\n\n**成果：** 重複執行仍保持正確"),
        ("案例 2：日期偏移", "**問題：** 日期偏移\n\n**解法：** 統一國際時間與台灣時間的換算規則\n\n**成果：** 事件歸屬正確"),
        ("案例 3：情緒失真", "**問題：** 未分析資料造成情緒失真\n\n**解法：** 區分「尚未分析」與「中性」\n\n**成果：** 提升資料可信度"),
    ])

    st.divider()
    st.subheader("成果與價值")
    st.markdown("**這套架構可直接套用於：**")
    render_overview_cards([
        ("訂單分析", "找出訂單量變化與異常波動"),
        ("客訴分析", "分析客訴主題與情緒變化"),
        ("庫存分析", "觀察庫存變化與缺貨風險"),
        ("ERP 資料分析", "自動整合多來源資料"),
        ("BI 報表系統", "提供管理者定期更新且易於理解的決策參考資訊"),
    ])

    st.markdown("#### 我的成長目標")
    with st.container(border=True):
        st.markdown(
            "過去在倉管、出貨管理與 ERP 維護工作中，習慣在追求正確性的前提下持續改善流程效率。"
            "當時主要使用 Excel 處理資料與報表，雖然能幫助公司降低作業成本，但分析深度與應用範圍仍有限。"
        )
        st.markdown("因此開始學習，目前已實際應用：")
        st.markdown("- Python\n- VS Code\n- GitHub\n- 資料分析\n- ETL\n- Dashboard 開發")
        st.markdown("持續學習：")
        st.markdown("- Power BI")
        st.markdown(
            "希望將原本的流程改善能力，進一步提升為資料分析與決策支援能力。\n\n"
            "未來目標是：不只是協助企業降低成本，更能透過數據整理、分析與視覺化，"
            "協助管理者發現問題、評估機會，並作為決策參考依據。"
        )

    st.caption("本專案以資料分析與系統開發為主要目的，分析結果僅供觀察與研究參考。")


@st.cache_data(ttl=300)
def build_market_radar() -> pd.DataFrame:
    themes = load_theme_definitions()
    mappings = load_theme_mapping_rows()
    observations = load_trend_observations()
    all_prices = load_prices()
    today = pd.Timestamp(taipei_today())
    rows = []
    for theme_name, definition in themes.items():
        keywords = definition["keywords"]
        stock_ids = definition["stock_ids"]
        history = load_theme_history(theme_name, lookback_days=365)
        history["trend_date"] = pd.to_datetime(history.get("trend_date"), errors="coerce")
        recent = history[history["trend_date"] >= today - pd.Timedelta(days=6)] if not history.empty else history
        previous = history[
            history["trend_date"].between(
                today - pd.Timedelta(days=13),
                today - pd.Timedelta(days=7),
            )
        ] if not history.empty else history
        recent_heat = pd.to_numeric(recent.get("trend_score"), errors="coerce").mean() if not recent.empty else float("nan")
        previous_heat = pd.to_numeric(previous.get("trend_score"), errors="coerce").mean() if not previous.empty else float("nan")
        heat_change = recent_heat / previous_heat - 1 if pd.notna(previous_heat) and previous_heat > 0 else pd.NA

        news = prepare_theme_news(observations, keywords)
        latest_theme_date = history["trend_date"].max().normalize() if not history.empty else today
        news = news[news["date"].between(latest_theme_date - pd.Timedelta(days=6), latest_theme_date)]
        sentiment = summarize_sentiment_distribution(news)
        coverage = sentiment["coverage_pct"]

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
            "正面": sentiment["positive_n"],
            "中性": sentiment["neutral_n"],
            "負面": sentiment["negative_n"],
            "未分析": sentiment["unanalyzed"],
            "情緒覆蓋率": coverage / 100 if coverage is not None else pd.NA,
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
    st.caption(f"正面／中性／負面僅計入已分析新聞，未分析新聞另列；情緒覆蓋率低於 {SENTIMENT_COVERAGE_MIN_PCT:.0f}% 時{LOW_SENTIMENT_COVERAGE_MESSAGE}。")
    st.dataframe(
        radar,
        column_config={
            "熱度變化率": st.column_config.NumberColumn(format="percent"),
            "情緒覆蓋率": st.column_config.NumberColumn(format="percent"),
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
                f"{SENTIMENT_LABELS.get(sentiment, sentiment)} {int(count)}"
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
    sentiment_colors = {"Positive": "#228b55", "Neutral": "#e2b52d", "Negative": "#d64b45", UNANALYZED: "#a3aaa5"}
    for sentiment, color in sentiment_colors.items():
        subset = chart_news[chart_news["news_sentiment"].eq(sentiment)]
        if subset.empty:
            continue
        figure.add_trace(go.Scatter(
            x=subset["date"], y=subset["marker_heat"], mode="markers",
            name=f"新聞：{SENTIMENT_LABELS.get(sentiment, sentiment)}",
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
        analyzed = int(sentiment_distribution["analyzed"])
        coverage_cols = st.columns(2)
        with coverage_cols[0]:
            st.metric("已分析新聞", f"{analyzed:,} / {int(sentiment_distribution['total']):,}")
        with coverage_cols[1]:
            coverage = sentiment_distribution["coverage_pct"]
            st.metric("情緒覆蓋率", f"{coverage:.1f}%" if coverage is not None else "N/A")
        if sentiment_distribution["low_coverage"]:
            st.warning(LOW_SENTIMENT_COVERAGE_MESSAGE)
        sentiment_cols = st.columns(4)
        for column, label in zip(sentiment_cols, ANALYZED_SENTIMENTS):
            with column:
                value = sentiment_distribution[f"{label.lower()}_pct"]
                st.metric(f"{label} %", f"{value:.1f}%" if value is not None else "N/A")
        with sentiment_cols[3]:
            st.metric("未分析", f"{int(sentiment_distribution['unanalyzed']):,}")
        st.caption(f"Positive／Neutral／Negative 比例僅依已分析的 {analyzed:,} 則新聞計算，未分析新聞不計入。")
        if analyzed and sentiment_distribution["neutral_pct"] > 80:
            st.warning("已分析新聞中 Neutral 占比超過 80%，請檢查情緒分析流程是否失效、是否僅使用英文模型、是否新聞內容未正確傳入分析模組。")
        sentiment_table = build_sentiment_verification_sample(news)
        if sentiment_table.empty:
            st.info("目前無已分析新聞可供驗證")
        else:
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
    quality_cols = st.columns(2)
    with quality_cols[0]:
        st.metric("有效首次反應天數比例", f"{quality['reaction_days_valid_pct']:.1f}%")
    with quality_cols[1]:
        st.metric(
            "情緒分析覆蓋率",
            format_sentiment_coverage(int(quality["analyzed_news"]), int(quality["total_news"])),
        )
    if quality["low_sentiment_coverage"]:
        st.warning(LOW_SENTIMENT_COVERAGE_MESSAGE)
    if quality["sentiment_warning"]:
        st.warning("情緒分析可能異常：已分析新聞中 Neutral 佔比超過 80%，請檢查中文新聞是否正確送入情緒模型。")
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
        st.caption(f"最後更新：{utc_to_taipei(trend_time):%Y-%m-%d %H:%M}（台北）" if trend_time else "尚無 Trends 資料")
        st.caption(
            f"關鍵字 {int(status['total_keywords']):,}｜新聞 {int(status['total_news']):,}\n\n"
            f"股票行情 {int(status['stock_rows']):,} 筆｜主題 {int(status['theme_count']):,} 個"
        )
        analyzed_news, total_news = int(status["analyzed_news"]), int(status["total_news"])
        st.caption(f"情緒分析覆蓋率 {format_sentiment_coverage(analyzed_news, total_news)}")
        if is_low_sentiment_coverage(sentiment_coverage_pct(analyzed_news, total_news)):
            st.warning(LOW_SENTIMENT_COVERAGE_MESSAGE, icon=":material/warning:")
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