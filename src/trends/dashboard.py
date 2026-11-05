"""Streamlit entry point (app.py calls main). Pages live in trends.web: user pages explain and
let anyone explore events; developer pages present how the project works.

The sentiment and news helpers are re-exported here because tests and older code import them
from trends.dashboard.
"""
from __future__ import annotations

import streamlit as st

from trends.web import dev_pages, ui, user_pages
from trends.web.data import (  # noqa: F401  (re-exported)
    ANALYZED_SENTIMENTS,
    LOW_SENTIMENT_COVERAGE_MESSAGE,
    RESEARCH_START,
    SENTIMENT_COVERAGE_MIN_PCT,
    UNANALYZED,
    build_data_quality_check,
    build_sentiment_verification_sample,
    database_engine,
    format_sentiment_coverage,
    is_low_sentiment_coverage,
    load_trend_observations,
    normalize_sentiment,
    prepare_theme_news,
    prepare_theme_rss,
    sentiment_coverage_pct,
    summarize_sentiment_distribution,
)
from trends.web.style import configure_app
from trends.web.ui import format_event_table_value  # noqa: F401  (re-exported)


def main() -> None:
    configure_app()
    pages = {
        "quickstart": st.Page(user_pages.page_quickstart, title="快速上手", icon=":material/school:", default=True),
        "stock": st.Page(user_pages.page_stock_events, title="個股事件圖", icon=":material/candlestick_chart:", url_path="stock"),
        "radar": st.Page(user_pages.page_radar, title="市場雷達", icon=":material/radar:", url_path="radar"),
        "findings": st.Page(user_pages.page_findings, title="研究結論", icon=":material/insights:", url_path="findings"),
        "overview": st.Page(dev_pages.page_overview, title="專案總覽", icon=":material/info:", url_path="overview"),
        "architecture": st.Page(dev_pages.page_architecture, title="系統架構", icon=":material/account_tree:", url_path="architecture"),
        "logic": st.Page(dev_pages.page_logic, title="資料處理邏輯", icon=":material/rule:", url_path="logic"),
        "method": st.Page(dev_pages.page_method, title="研究方法", icon=":material/functions:", url_path="method"),
        "quality": st.Page(dev_pages.page_quality, title="品質與維運", icon=":material/monitor_heart:", url_path="quality"),
    }
    ui.PAGES.update(pages)
    navigation = st.navigation({
        "使用者": [pages[key] for key in ("quickstart", "stock", "radar", "findings")],
        "開發者／作品集": [pages[key] for key in ("overview", "architecture", "logic", "method", "quality")],
    }, position="sidebar")
    ui.sidebar_status()
    navigation.run()
