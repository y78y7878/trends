"""User pages: quick start, stock events, market radar, findings. Plain language first, with the
tutorial next to the chart it explains."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from trends.database import LINK_DIRECT, LINK_THEME
from trends.web import charts, data, ui

SOURCE_CHOICES = {
    "公司本身（直接事件）": (LINK_DIRECT,),
    "產業詞（主題事件）": (LINK_THEME,),
    "兩種都看": (LINK_DIRECT, LINK_THEME),
}
PERIODS = {"1 個月": 31, "3 個月": 92, "全部": None}
DEFAULT_STOCK = "2330"


def _go(page_key: str, label: str, icon: str) -> None:
    if page_key in ui.PAGES:
        st.page_link(ui.PAGES[page_key], label=label, icon=icon)


# ---------- ① quick start ----------

def page_quickstart() -> None:
    ui.header(
        "看到熱搜才買股票，來得及嗎？",
        "這個網站每 20 分鐘記錄 Google 熱門搜尋，配上相關新聞與股價，讓你用一張圖看懂："
        "<b>一個關鍵字爆紅的前後，相關股票發生了什麼事。</b>",
    )
    st.subheader("三步驟上手")
    ui.steps([
        ("選一檔股票", "到「個股事件圖」，從清單搜尋股票（可以打代號或名稱，例如 2330 或 台積電）。"),
        ("看圖找事件", "圖上的<b>圓圈數字</b>就是事件：關鍵字突然被大量搜尋或報導的那天。滑鼠移上去可以看新聞標題。"),
        ("看事件後的變化", "圓圈的<b>顏色</b>和後面加粗的線段代表之後 10 個交易日的表現：綠色比大盤好、紅色比大盤差。下方卡片有完整新聞與數字。"),
    ])
    st.markdown("")
    _go("stock", "前往個股事件圖", ":material/candlestick_chart:")

    st.divider()
    st.subheader("怎麼看個股事件圖")
    st.markdown("圖分成上中下三層，共用同一條時間軸，所以可以直接往下對照「同一天」發生了什麼：")
    ui.cards([
        ("① 股價", "黑線是每天收盤價，橘色虛線是 20 日均線。<b>圓圈數字</b>標出事件，圓圈顏色和事件後 10 個交易日的加粗線段："
                  "<span style='color:#1f8a55'><b>綠</b></span>＝扣掉大盤後上漲、"
                  "<span style='color:#d14b43'><b>紅</b></span>＝扣掉大盤後下跌、灰＝還沒滿 10 天。"),
        ("② 搜尋熱度", "每條線是一個關鍵字在 Google 的熱門程度（0–100）。線突然衝高，代表大家開始搜尋它，通常就是事件發生的時間點。"),
        ("③ 每日新聞", "每根柱子是當天收錄的相關新聞數，顏色是 AI 判斷的情緒：綠色正面、灰色中性、紅色負面。"),
    ])
    st.info("小技巧：在圖上拖曳可以放大某一段時間，雙擊回到原本範圍；點圖例可以隱藏或顯示某條線。", icon=":material/lightbulb:")

    st.divider()
    st.subheader("其他頁面")
    ui.cards([
        ("市場雷達", "現在哪些產業的搜尋熱度在升溫？相關新聞偏正面還是負面？同類股最近表現如何？"),
        ("研究結論", "把所有事件放在一起統計：熱搜之後買進，平均來說有沒有比大盤好？答案用一張圖說明。"),
    ])
    left, right = st.columns(2)
    with left:
        _go("radar", "前往市場雷達", ":material/radar:")
    with right:
        _go("findings", "前往研究結論", ":material/insights:")

    st.divider()
    ui.glossary(expanded=True)
    st.caption("本網站是歷史資料的關聯研究，不預測股價、不提供買賣建議。")


# ---------- ② stock events ----------

def page_stock_events() -> None:
    ui.header("個股事件圖", "選一檔股票，看看它的相關關鍵字什麼時候爆紅、當時新聞在說什麼，以及<b>之後股價怎麼走</b>。")
    options = data.stock_options()
    if options.empty:
        st.info("目前沒有可以分析的股票，請等排程收集資料。")
        return
    labels = dict(zip(options["label"], options["stock_id"]))
    default = next((i for i, s in enumerate(options["stock_id"]) if s == st.session_state.get("selected_stock", DEFAULT_STOCK)), 0)
    controls = st.columns([2.2, 1.6, 1.4])
    with controls[0]:
        label = st.selectbox("股票（可輸入代號或名稱搜尋）", list(labels), index=default)
    stock_id = labels[label]
    st.session_state["selected_stock"] = stock_id
    row = options[options["stock_id"].eq(stock_id)].iloc[0]
    with controls[1]:
        default_source = 0 if row[LINK_DIRECT] else 1
        source = st.radio("事件來源", list(SOURCE_CHOICES), index=default_source,
                          help="直接事件：關鍵字就是這家公司。主題事件：產業詞上熱搜，這檔股票是同類股之一。")
    with controls[2]:
        period = st.segmented_control("期間", list(PERIODS), default="3 個月")
    st.caption(f"這檔股票連結了 {int(row[LINK_DIRECT])} 個公司關鍵字、{int(row[LINK_THEME])} 個產業關鍵字。")

    stock = data.build_stock_events(stock_id, SOURCE_CHOICES[source])
    if stock is None:
        st.info("這檔股票沒有這種事件來源的關鍵字，請換一個事件來源。")
        return
    end = stock.prices["date"].max()
    days = PERIODS.get(period or "3 個月")
    start = max(data.RESEARCH_START, end - pd.Timedelta(days=days)) if days else data.RESEARCH_START
    events = stock.events[stock.events["event_date"].between(start, end)] if not stock.events.empty else stock.events

    _summary(stock, events)
    with st.expander("看圖小教學", expanded=not st.session_state.get("seen_chart_help"), icon=":material/school:"):
        st.markdown(
            "1. **圓圈數字**＝事件（關鍵字爆紅的那天），滑鼠移上去看關鍵字、原因與新聞標題。\n"
            "2. **圓圈顏色**和後面**加粗的線段**＝之後 10 個交易日扣掉大盤的表現，綠色贏大盤、紅色輸大盤、灰色還沒有足夠天數。\n"
            "3. 往下看第 ② 層的**搜尋熱度**是不是同時衝高，第 ③ 層的**新聞**是正面還是負面。\n"
            "4. 拖曳可以放大，雙擊回復。完整新聞與數字在圖下方的事件卡片。"
        )
        st.session_state["seen_chart_help"] = True
    if len(events) > 40:
        st.warning(f"這段期間有 {len(events)} 個事件，圖會比較擁擠。可以縮短期間，或改看「公司本身」的直接事件。",
                   icon=":material/info:")
    st.plotly_chart(charts.stock_event_chart(stock, start, end), width="stretch",
                    config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]})

    st.subheader("事件時間軸")
    if events.empty:
        st.info("這段期間沒有事件。可以把期間拉長，或切換事件來源。")
        return
    st.caption("最新的在最上面。數字和圖上的圓圈一一對應。")
    shown = st.session_state.setdefault(f"shown_{stock_id}", 8)
    for _, event in events.sort_values("event_date", ascending=False).head(shown).iterrows():
        ui.event_card(event, stock.normal.get(10))
    if len(events) > shown and st.button(f"再顯示 8 個（還有 {len(events) - shown} 個）"):
        st.session_state[f"shown_{stock_id}"] = shown + 8
        st.rerun()


def _summary(stock: data.StockEvents, events: pd.DataFrame) -> None:
    excess = pd.to_numeric(events.get("excess_10d"), errors="coerce").dropna() if not events.empty else pd.Series(dtype=float)
    normal = stock.normal.get(10)
    columns = st.columns(4)
    columns[0].metric("事件數", f"{len(events)}")
    columns[1].metric("事件後 10 日平均（扣大盤）", f"{excess.mean():+.1%}" if not excess.empty else "—",
                      help="從事件當天收盤起算 10 個交易日，再減掉同期 0050 的漲跌。")
    columns[2].metric("平常 10 日（扣大盤）", f"{normal:+.1%}" if normal is not None else "—",
                      help="這檔股票在研究期間任何一天買進、持有 10 天，平均贏大盤多少。")
    if not excess.empty and normal is not None:
        diff = excess.mean() - normal
        columns[3].metric("事件比平常", f"{diff:+.1%}", help="大於 0 代表事件後表現比平常好。事件少於 10 個時參考價值有限。")
    else:
        columns[3].metric("事件比平常", "—")


# ---------- ③ market radar ----------

def page_radar() -> None:
    ui.header("市場雷達", "現在哪些產業正在升溫？把每個主題的<b>搜尋熱度變化</b>、<b>新聞情緒</b>和<b>同類股表現</b>放在一起看。")
    radar = data.build_market_radar()
    if radar.empty:
        st.info("目前沒有主題資料。")
        return
    st.subheader("一張圖看全部主題")
    st.caption("右邊＝最近被搜尋得更多；上面＝同類股最近 10 個交易日上漲；泡泡越大＝最近 7 天新聞越多。")
    st.plotly_chart(charts.radar_chart(radar), width="stretch", config={"displaylogo": False})

    st.subheader("各主題細節")
    ordered = radar.sort_values("heat_change", ascending=False, na_position="last")
    for start in range(0, len(ordered), 2):
        for column, (_, theme) in zip(st.columns(2), ordered.iloc[start:start + 2].iterrows()):
            with column, st.container(border=True):
                change = theme["heat_change"]
                st.markdown(f"#### {theme['theme']}")
                st.markdown(
                    ui.chip(f"熱度 {change:+.0%}" if pd.notna(change) else "熱度 —", "up" if (change or 0) > 0 else "down")
                    + ui.return_chip("同類股 10 日", theme["return_10d"]) + ui.chip(f"近 7 日新聞 {theme['news']} 則"),
                    unsafe_allow_html=True)
                total = max(theme["positive"] + theme["neutral"] + theme["negative"], 1)
                st.markdown(
                    "<div style='display:flex;height:10px;border-radius:5px;overflow:hidden;margin:6px 0'>"
                    f"<div style='width:{theme['positive'] / total:.0%};background:#1f8a55'></div>"
                    f"<div style='width:{theme['neutral'] / total:.0%};background:#9aa39b'></div>"
                    f"<div style='width:{theme['negative'] / total:.0%};background:#d14b43'></div></div>"
                    f"<span class='muted'>新聞情緒：正面 {theme['positive']}／中性 {theme['neutral']}／負面 {theme['negative']}</span>",
                    unsafe_allow_html=True)
                names = "、".join(data.stock_label(s) for s in theme["stocks"][:6])
                st.markdown(f"<span class='muted'>同類股：{names}{'…' if len(theme['stocks']) > 6 else ''}</span>",
                            unsafe_allow_html=True)
                if theme["stocks"] and "stock" in ui.PAGES:
                    first = theme["stocks"][0]
                    if st.button(f"看 {data.stock_label(first)} 的事件圖", key=f"radar_{theme['theme']}"):
                        st.session_state["selected_stock"] = first
                        st.switch_page(ui.PAGES["stock"])
    ui.glossary(["搜尋熱度", "主題事件", "新聞情緒"])
    st.caption("熱度變化＝最近 7 天平均 ÷ 前 7 天平均 − 1；同類股＝theme_mapping.csv 設定的股票籃子等權平均。只描述現況，不代表投資排名。")


# ---------- ④ findings ----------

def page_findings() -> None:
    ui.header("研究結論：看到熱搜再買有用嗎？",
              "把研究期間所有事件放在一起，看熱搜<b>前後</b>股價平均怎麼走。")
    results = data.research_results()
    paths = results["paths"]
    st.subheader("目前的答案：來不及了。")
    st.markdown("股價通常在熱搜<b>之前</b>就開始動，熱搜當天再跳一下，之後反而慢慢回吐。等你看到熱搜才買，平均沒有比大盤好。",
                unsafe_allow_html=True)
    st.plotly_chart(charts.car_chart(paths), width="stretch", config={"displaylogo": False})
    direct = paths[paths["kind"].eq("direct")].set_index("offset")["car_adjusted"] if not paths.empty else pd.Series(dtype=float)

    def at(offset: int) -> str:
        return f"{direct.get(offset):+.1%}" if offset in direct else "—"

    st.subheader("圖怎麼看")
    ui.cards([
        ("熱搜之前：已經先漲", f"第 −5 天的線在 {at(-5)}，代表熱搜前一週股價就已經比大盤多漲了一段。消息常常先在市場上反應。"),
        ("熱搜當天：再跳一下", f"第 0 天跳到 {at(0)}。熱搜和股價大動常常同一天發生，比較像是<b>股價先動，大家才去搜尋</b>。"),
        ("熱搜之後：慢慢回吐", f"第 10 天回到 {at(10)}。灰色區間是看到熱搜後才能買進的時間，這段平均沒有超額報酬。"),
    ])
    st.subheader("產業詞會帶動同類股嗎？")
    st.markdown(
        "藍線是產業詞上熱搜（例如「伺服器」）後，同類股籃子的平均表現。它的形狀和公司本身的事件類似，"
        "<b>扣掉同類股平常的漲幅後，看不出熱搜帶來額外的效果</b>，所以「同類股連動」目前沒有得到支持。",
        unsafe_allow_html=True)
    st.info(
        f"資料期間 2026-08-21～{pd.Timestamp(results['period_end']):%Y-%m-%d}，直接事件 {results['units'].get('direct', 0)} 個、"
        f"主題事件 {results['units'].get('theme', 0)} 個。樣本還不多，結論會隨資料累積每月更新。"
        "淺色帶是 95% 信賴區間：帶子越寬，代表越不確定。", icon=":material/info:")
    ui.glossary(["超額報酬", "平常水準", "直接事件", "主題事件"])
    _go("method", "想知道怎麼算的？看「研究方法」", ":material/functions:")
    st.caption("這是歷史資料的關聯研究，不預測未來股價，也不構成投資建議。")
