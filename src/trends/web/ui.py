"""Reusable page pieces: headers, cards, chips, the event timeline card and the sidebar."""
from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from trends.event_study import normalize_breakout_day
from trends.web import data
from trends.web.style import SENTIMENT_COLORS, SENTIMENT_NAMES

# Page objects registered by trends.dashboard.main, so pages can link to each other.
PAGES: dict[str, object] = {}

GLOSSARY = {
    "熱搜": "Google Trends 台灣「熱門搜尋」榜上的關鍵字。系統每 20 分鐘記錄一次榜單。",
    "搜尋熱度": "Google 提供的 0–100 分數，代表一個關鍵字每天被搜尋的相對程度（100 = 期間內最熱的一天）。",
    "事件": "關鍵字突然受到關注的那一天：登上熱搜（前 3 天都沒上榜）、搜尋熱度比前 7 天平均高 50% 以上，或新聞量變成平常的 2 倍以上。",
    "直接事件": "關鍵字就是公司本身，例如「台積電」上熱搜，只連到 2330。",
    "主題事件": "關鍵字是產業詞，例如「伺服器」上熱搜，連到同一主題的一籃子股票。",
    "超額報酬": "股票的漲跌減掉同期大盤（0050）的漲跌。大盤一起漲的部分不算熱搜的功勞。",
    "平常水準": "這檔股票在研究期間「任何一天」買進，平均能贏大盤多少。事件後的超額報酬要比這個高，才算熱搜真的有影響。",
    "新聞情緒": "AI（Gemini）讀新聞標題後判斷偏正面、中性或負面；尚未分析的新聞另外標示，不會當成中性。",
    "近期波動": "股票每天的漲跌和大盤（0050）差多少，取最近 20 個交易日的平均；「平常的 X 倍」是和再往前 40 個交易日比較。"
               "研究發現它能預測接下來幾天的波動，熱搜則沒有額外的預測力。只代表幅度，不代表漲或跌。",
}


def format_event_table_value(value: object, column_name: str | None = None) -> str:
    """Display text for one event-table cell: percentages for returns, days for reaction time."""
    if value is None or pd.isna(value):
        return "資料尚不足" if column_name and any(token in column_name for token in ("報酬", "超額", "漲幅", "跌幅", "反應")) else "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if column_name == "首次反應天數":
        days = pd.to_numeric(value, errors="coerce")
        return f"{int(days)} 天" if pd.notna(days) else "資料尚不足"
    if isinstance(value, (float, int)):
        if column_name in {"MA20突破日", "MA60突破日"}:
            return normalize_breakout_day(value)
        if column_name and any(token in column_name for token in ("報酬", "超額", "漲幅", "跌幅", "反應")):
            return f"{float(value):.2%}"
        if column_name == "事件熱度":
            return f"{float(value):.0f}"
        if column_name == "熱搜流量":
            return f"{int(value):,}+"
        return str(value)
    return str(value)


def header(title: str, lead: str) -> None:
    st.title(title)
    st.markdown(f"<p class='lead'>{lead}</p>", unsafe_allow_html=True)


def chip(text: str, kind: str = "") -> str:
    return f"<span class='chip {kind}'>{html.escape(str(text))}</span>"


def return_chip(label: str, value: object) -> str:
    if value is None or pd.isna(value):
        return chip(f"{label} 尚無資料", "flat")
    value = float(value)
    return chip(f"{label} {value:+.1%}", "up" if value > 0 else "down" if value < 0 else "flat")


def volatility_level(ratio: float) -> tuple[str, str]:
    """Plain-language level and chip kind for a recent / usual volatility ratio."""
    if ratio >= 1.5:
        return "明顯比平常動盪", "down"
    if ratio >= 1.2:
        return "比平常動盪", "heat"
    if ratio > 0.8:
        return "和平常差不多", "flat"
    return "比平常平靜", "flat"


def cards(items: list[tuple[str, str]], columns: int | None = None) -> None:
    columns = columns or len(items)
    for start in range(0, len(items), columns):
        for column, (title, body) in zip(st.columns(columns), items[start:start + columns]):
            with column, st.container(border=True, height="stretch"):
                st.markdown(f"**{title}**")
                st.markdown(body, unsafe_allow_html=True)


def steps(items: list[tuple[str, str]]) -> None:
    for column, (index, (title, body)) in zip(st.columns(len(items)), enumerate(items, 1)):
        with column, st.container(border=True, height="stretch"):
            st.markdown(f"<div class='step'>{index}</div>", unsafe_allow_html=True)
            st.markdown(f"**{title}**")
            st.markdown(body, unsafe_allow_html=True)


def glossary(terms: list[str] | None = None, expanded: bool = False) -> None:
    with st.expander("名詞解釋", expanded=expanded, icon=":material/menu_book:"):
        for term in terms or GLOSSARY:
            st.markdown(f"**{term}**：{GLOSSARY[term]}")


def takeaway(text: str) -> None:
    st.markdown(f"> **{text}**")


def event_card(event: pd.Series, normal_10d: float | None) -> None:
    """One event: what happened (keywords, why it counts), what the news said, what the price did."""
    with st.container(border=True):
        kinds = "".join(chip(data.LINK_LABELS.get(k, k) + "事件", "heat") for k in event["link_types"])
        st.markdown(
            f"<span class='badge'>{event['number']}</span>"
            f"<b>{event['event_date']:%Y-%m-%d}</b>　{html.escape('、'.join(event['keywords'][:5]))}　{kinds}",
            unsafe_allow_html=True,
        )
        left, right = st.columns([3, 2])
        with left:
            reasons = "".join(chip(t) for t in event["triggers"])
            traffic = chip(f"熱搜流量 {int(event['traffic']):,}+", "heat") if pd.notna(event["traffic"]) else ""
            st.markdown(f"<span class='muted'>為什麼算事件：</span>{reasons}{traffic}", unsafe_allow_html=True)
            if event["news_count"]:
                tags = "".join(chip(t) for t in event["event_types"])
                st.markdown(
                    f"<span class='muted'>新聞概要：{event['news_count']} 則，正面 {event['positive']}、負面 {event['negative']}"
                    f"</span>　{tags}", unsafe_allow_html=True)
                for item in event["headlines"][:3]:
                    color = SENTIMENT_COLORS.get(item["news_sentiment"], "#cfd5cc")
                    title = html.escape(str(item["news_title"]))
                    link = f"<a href='{html.escape(str(item['news_url']))}' target='_blank'>{title}</a>" if item["news_url"] else title
                    st.markdown(
                        f"<span style='color:{color}'>●</span> {link} <span class='muted'>— {html.escape(str(item['news_source'] or ''))}"
                        f"（{SENTIMENT_NAMES.get(item['news_sentiment'], '')}）</span>", unsafe_allow_html=True)
            else:
                st.markdown("<span class='muted'>這天沒有收錄到相關新聞（事件由搜尋熱度判定）。</span>", unsafe_allow_html=True)
        with right:
            st.markdown("<span class='muted'>之後的股價（從事件當天收盤起算）</span>", unsafe_allow_html=True)
            st.markdown(
                return_chip("1 日", event.get("return_1d")) + return_chip("5 日", event.get("return_5d"))
                + return_chip("10 日", event.get("return_10d")), unsafe_allow_html=True)
            st.markdown("<span class='muted'>扣掉同期大盤（0050）後</span>", unsafe_allow_html=True)
            st.markdown(
                return_chip("5 日", event.get("excess_5d")) + return_chip("10 日", event.get("excess_10d")),
                unsafe_allow_html=True)
            excess = event.get("excess_10d")
            if normal_10d is not None and excess is not None and pd.notna(excess):
                verdict = "比平常好" if excess - normal_10d > 0.01 else "比平常差" if excess - normal_10d < -0.01 else "和平常差不多"
                st.markdown(f"<span class='muted'>這檔股票平常 10 日約 {normal_10d:+.1%}，這次 <b>{verdict}</b></span>",
                            unsafe_allow_html=True)
            before, after = event.get("vol_before"), event.get("vol_after5")
            if before is not None and pd.notna(before):
                after_text = f"，之後 5 日 <b>{after:.1f} 倍</b>" if after is not None and pd.notna(after) else "，之後 5 日還沒滿"
                st.markdown(f"<span class='muted'>波動：事件前 20 日是平常的 <b>{before:.1f} 倍</b>{after_text}</span>",
                            unsafe_allow_html=True)


def sidebar_status() -> None:
    status = data.system_status()
    with st.sidebar:
        st.markdown("### TRENDS / THEME")
        st.caption("熱搜 × 新聞 × 股價")
        st.markdown(f"**{status['label']}**")
        if status["latest_rss"]:
            st.caption(f"最新熱搜：{status['latest_rss']:%m-%d %H:%M}")
        if status["last_etl"]:
            st.caption(f"上次 AI 分類：{status['last_etl']:%m-%d %H:%M}")
        st.caption(f"下次 AI 分類：{status['next_etl']:%m-%d %H:%M}")
        if status["stock_date"]:
            st.caption(f"股價最新交易日：{status['stock_date']:%Y-%m-%d}")
        st.caption(f"等待 AI 分析的新聞：{status['pending_news']:,} 則（下次分類時處理）")
        st.divider()
        st.caption("資料由排程自動更新，網頁每 5 分鐘重新讀取，不需要手動更新。這是歷史關聯研究，不是投資建議。")
