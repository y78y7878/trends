"""Developer / portfolio pages: what the project is, how it is built, how data flows and is
cleaned, how the research avoids false signals, and how it is kept running. Diagrams first."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import streamlit as st

from trends.database import DB_PATH
from trends.web import charts, data, ui

ROOT = DB_PATH.parent
NODE = 'node [shape=box, style="rounded,filled", fontname="Noto Sans TC", fontsize=11, color="#53745b", fillcolor="#eef2e8"]; edge [color="#53745b", fontname="Noto Sans TC", fontsize=10];'


def _dot(body: str, direction: str = "LR") -> str:
    return f"digraph G {{ rankdir={direction}; bgcolor=transparent; {NODE} {body} }}"


# ---------- ① overview ----------

def page_overview() -> None:
    ui.header("專案總覽", "一套自動收集 Google 熱搜、新聞與股價，用 AI 整理後做事件研究的資料系統。"
              "從資料收集、清洗、AI 分析、統計驗證到網站呈現，全部自己設計、自動執行。")
    counts = data.table_counts()
    columns = st.columns(4)
    columns[0].metric("熱搜紀錄", f"{counts['google_trends']:,}")
    columns[1].metric("新聞", f"{counts['google_trends_news']:,}")
    columns[2].metric("股價（日資料）", f"{counts['stocks']:,}")
    columns[3].metric("歷史熱度", f"{counts['google_trends_history']:,}")
    st.caption("數字即時讀自資料庫。")

    st.subheader("一句話看懂整個系統")
    st.graphviz_chart(_dot(
        'rss [label="Google 熱搜\\n每 20 分鐘", fillcolor="#dceaf5"]; ai [label="AI 整理\\n分類・辨識公司・新聞情緒", fillcolor="#e9e1f2"];'
        'price [label="股價\\n216 檔", fillcolor="#dcefe3"]; study [label="事件研究\\n扣大盤・對照組", fillcolor="#f7e7d3"];'
        'web [label="網站\\n個股事件圖・研究結論", fillcolor="#2f6b4f", fontcolor="white"];'
        'rss -> ai -> study; price -> study -> web;'), width="stretch")

    st.subheader("為什麼做這個專案")
    st.markdown(
        "> 看到新聞才知道一家公司的消息，是不是已經太晚了？市場是不是早就反應了？\n\n"
        "這是我接觸股市以來一直有的疑問。在職訓期間學了 Python、資料庫、視覺化與 AI 應用之後，"
        "我把它變成一個可以用資料驗證的問題：**當一個話題突然爆紅，相關股票之後會不會有超乎大盤的表現？**"
    )
    ui.takeaway("答案（截至 2026-11）：熱搜當天股價波動是平常的 2 倍，但熱搜之後沒有可利用的超額報酬，也看不到同類股連動。")

    st.subheader("用到的技術")
    ui.cards([
        ("資料收集", "RSS 解析、yfinance 股價、pytrends 歷史熱度、證交所／櫃買中心公開資料"),
        ("資料處理", "Python・pandas・SQLAlchemy・SQLite；冪等寫入、時區統一、一次性資料遷移"),
        ("AI 應用", "Gemini 批次分類與情緒判讀；多模型備援、限流、續跑、結果驗證"),
        ("研究與呈現", "事件研究、超額報酬、t 檢定；Streamlit・Plotly・Graphviz"),
        ("維運", f"Windows 工作排程器、健康報告、雲端備份、{_test_count()} 個自動化測試、Git"),
    ], columns=3)

    st.subheader("我的能力成長路線")
    st.graphviz_chart(_dot(
        'warehouse [label="倉管／出貨管理"]; erp [label="ERP 維護"]; vba [label="Excel VBA 自動化", fillcolor="#dfe9d3"];'
        'python [label="Python 資料處理", fillcolor="#d3e4c9"]; etl [label="ETL 流程設計", fillcolor="#c6dcbd"];'
        'dashboard [label="Dashboard 開發", fillcolor="#b6d2ad"]; ai [label="AI 資料分析", fillcolor="#2f6b4f", fontcolor="white"];'
        'warehouse -> erp -> vba -> python -> etl -> dashboard -> ai;'), width="stretch")
    st.markdown(
        "過去負責訂單管理、出貨流程與 ERP 維護，用 Excel VBA 把每日人工處理時間從約 3～4 小時縮短到 5～10 分鐘。"
        "之後學習 Python，把自動化能力延伸到資料工程、網站與 AI 應用。這套架構同樣可以用在訂單、客訴、庫存等營運資料分析。"
    )
    st.caption("左側導覽列「開發者」分頁依序說明：系統架構 → 資料處理邏輯 → 研究方法 → 品質與維運。")


# ---------- ② architecture ----------

def page_architecture() -> None:
    ui.header("系統架構", "資料從哪裡來、經過哪些程式、存在哪裡、什麼時候更新。")
    st.subheader("整體架構")
    st.graphviz_chart(_dot(
        'subgraph cluster_src { label="外部資料來源"; style="rounded,dashed"; color="#9aa39b"; fontname="Noto Sans TC";'
        '  rss [label="Google Trends\\n熱搜 RSS", fillcolor="#dceaf5"]; pyt [label="Google Trends\\n歷史熱度（pytrends）", fillcolor="#dceaf5"];'
        '  yf [label="Yahoo Finance\\n股價（yfinance）", fillcolor="#dcefe3"]; gem [label="Gemini API", fillcolor="#e9e1f2"];'
        '  twse [label="證交所／櫃買中心\\n上市櫃名稱", fillcolor="#f7e7d3"]; }'
        'subgraph cluster_job { label="排程程式（Windows 工作排程器）"; style="rounded"; color="#53745b"; fontname="Noto Sans TC";'
        '  c_rss [label="rss_collector\\n每 20 分鐘"]; c_etl [label="etl\\n每天 4 次"]; c_stock [label="stock_collector\\n交易日 18:30"];'
        '  c_health [label="health_report\\n每天 07:30"]; c_backup [label="db_backup\\n每天 03:30"]; }'
        'db [label="SQLite\\ndata.db", shape=cylinder, fillcolor="#fff7e0"];'
        'web [label="Streamlit 網站\\n（唯讀）", fillcolor="#2f6b4f", fontcolor="white"];'
        'drive [label="Google 雲端硬碟\\n每日備份 14 份", shape=folder, fillcolor="#eef2e8"];'
        'report [label="logs/health_report.md", shape=note];'
        'rss -> c_rss; pyt -> c_etl; gem -> c_etl; twse -> c_etl; yf -> c_stock;'
        'c_rss -> db; c_etl -> db; c_stock -> db; db -> c_health -> report; db -> c_backup -> drive; db -> web;'
    ), width="stretch")
    st.caption("所有寫入都在排程程式裡完成，網站只讀資料庫，所以網頁開著或關著都不影響資料收集。")

    st.subheader("一天的排程")
    st.plotly_chart(charts.schedule_chart(), width="stretch", config={"displaylogo": False})
    st.markdown(
        "- **RSS 每 20 分鐘**：熱搜榜只保留最近約 1 小時的內容，錯過就拿不回來，所以收得最勤。\n"
        "- **ETL 每天 4 次**：Gemini 和 Google Trends 都有限流，分散在不同時段多試幾次，總處理量比一次跑完更多。\n"
        "- **備份排在 ETL 之後、健康報告排在備份之後**：早上看報告時，前一晚的資料都已經處理並備份好。"
    )

    st.subheader("資料表關聯")
    counts = data.table_counts()
    c = lambda name: f"{counts.get(name, 0):,} 筆"
    st.graphviz_chart(_dot(
        f'gt [label="google_trends\\n熱搜關鍵字\\n{c("google_trends")}", fillcolor="#dceaf5"];'
        f'obs [label="google_trends_observations\\n每次看到的流量\\n{c("google_trends_observations")}", fillcolor="#dceaf5"];'
        f'news [label="google_trends_news\\n新聞＋AI 情緒\\n{c("google_trends_news")}", fillcolor="#dceaf5"];'
        f'kc [label="keyword_classification\\n關鍵字分類\\n{c("keyword_classification")}", fillcolor="#e9e1f2"];'
        f'em [label="entity_master\\n公司實體\\n{c("entity_master")}", fillcolor="#e9e1f2"];'
        f'tm [label="theme_mapping\\n關鍵字 → 股票（直接／主題）\\n{c("theme_mapping")}", fillcolor="#f7e7d3"];'
        f'hist [label="google_trends_history\\n每日熱度 0–100\\n{c("google_trends_history")}", fillcolor="#dceaf5"];'
        f'stocks [label="stocks\\n日股價\\n{c("stocks")}", fillcolor="#dcefe3"];'
        f'tds [label="theme_daily_stats\\n主題每日統計\\n{c("theme_daily_stats")}"];'
        'gt -> obs [label="trend_id"]; gt -> news [label="trend_id"]; gt -> kc [label="keyword"]; kc -> em [label="entity_name"];'
        'kc -> tm [label="canonical keyword"]; tm -> hist [label="keyword"]; tm -> stocks [label="stock_id"]; tm -> tds [label="theme"];'
    ), width="stretch")

    st.subheader("程式結構")
    st.code(
        "src/trends/\n"
        "├─ rss_collector.py         熱搜 RSS（冪等寫入）\n"
        "├─ etl.py                   每日 ETL 流程\n"
        "│  ├─ entity_resolution_etl.py   公司辨識（直接連結）\n"
        "│  ├─ keyword_classification.py  AI 分類（公司／產業／不連）＋新聞情緒\n"
        "│  ├─ listed_companies.py        官方代號名稱比對\n"
        "│  ├─ gemini_control.py          限流、備援模型、續跑\n"
        "│  └─ trend_history_collector.py 歷史熱度輪替\n"
        "├─ stock_collector.py       股價（.TW／.TWO）\n"
        "├─ event_study.py / research.py  事件偵測與超額報酬\n"
        "├─ health_report.py / db_backup.py  維運\n"
        "├─ migrations/              一次性資料遷移（演練 → 套用 → 可還原）\n"
        "└─ web/                     這個網站（使用者頁、開發者頁、圖表）",
        language="text",
    )


# ---------- ③ processing logic ----------

def page_logic() -> None:
    ui.header("資料處理邏輯", "原始資料很亂：同一家公司有很多寫法、AI 會判斷錯、Google 會限流。這頁說明每一步怎麼處理。")
    tabs = st.tabs(["事件怎麼判定", "關鍵字怎麼連到股票", "AI 怎麼管理", "熱度資料怎麼補", "資料可信度"])

    with tabs[0]:
        st.markdown("一個關鍵字「突然受到關注」的那天就是事件。符合任一條件即可：")
        st.graphviz_chart(_dot(
            'a [label="登上熱搜\\n且前 3 天都沒上榜", fillcolor="#dceaf5"]; b [label="搜尋熱度\\n比前 7 天平均高 50%", fillcolor="#dceaf5"];'
            'c [label="新聞 ≥ 3 則\\n且是前 7 天平均的 2 倍", fillcolor="#dceaf5"];'
            'e [label="事件日", shape=ellipse, fillcolor="#2f6b4f", fontcolor="white"];'
            'd [label="對齊到下一個交易日\\n從當天收盤起算報酬", fillcolor="#f7e7d3"];'
            'a -> e; b -> e; c -> e; e -> d;'), width=640)
        st.markdown(
            "- **「前 3 天沒上榜」**：熱門話題常連續上榜好幾天，只有第一天算事件，避免同一件事被算好幾次。\n"
            "- **事件大小**：用 RSS 的流量級距（例如 2,000+）。系統每次收集都會記錄流量，看得到熱度怎麼升溫。\n"
            "- **時間**：RSS 給的是國際標準時間，統一換算成台北日期；週末的事件對齊到下一個交易日。"
        )

    with tabs[1]:
        st.markdown("關鍵字要先知道「是哪家公司」才能連到股價。依序嘗試，越前面越可靠：")
        st.graphviz_chart(_dot(
            'k [label="熱搜關鍵字", shape=ellipse, fillcolor="#dceaf5"];'
            's1 [label="① 人工種子表\\nentity_seed_mapping.csv"]; s2 [label="② 公司別名表\\nkeyword_theme_mapping.csv"];'
            's3 [label="③ 關鍵字本身是上市櫃代號？\\n查官方清單（排除像年份的 2025）"]; s4 [label="④ 問 Gemini", fillcolor="#e9e1f2"];'
            'v [label="代號比對官方名稱\\n名稱不符就不連", fillcolor="#f6dedc"];'
            'direct [label="直接連結\\n只連到自己的股票", fillcolor="#dcefe3"];'
            'k -> s1; s1 -> s2 [label="找不到"]; s2 -> s3 [label="找不到"]; s3 -> s4 [label="找不到"];'
            's1 -> direct [label="找到"]; s2 -> direct [label="找到"]; s3 -> direct [label="找到"];'
            's4 -> v [label="給出代號"]; v -> direct [label="通過"];',
            direction="TB"), width=460)
        st.markdown("不是公司的關鍵字，再交給 Gemini 判斷是哪一種：")
        st.graphviz_chart(_dot(
            'k [label="非公司關鍵字", shape=ellipse, fillcolor="#dceaf5"]; g [label="Gemini 判斷", fillcolor="#e9e1f2"];'
            'ind [label="產業詞\\n（保險、伺服器、CPI）", fillcolor="#f7e7d3"]; none [label="其他\\n（人名、外國公司、生活用語）"];'
            'theme [label="主題連結\\n連到人工設定的同類股籃子", fillcolor="#dcefe3"]; nolink [label="不連股票\\n只用在主題統計"];'
            'k -> g; g -> ind -> theme; g -> none -> nolink;'), width=680)
        ui.cards([
            ("修正前", "AI 只要覺得「跟股票有關」就把關鍵字連到整個主題的所有股票：「Uber」被連到 78 檔科技股、「謝金河」連到 25 檔金融股。"),
            ("修正後", "區分「公司本身」與「產業詞」兩種連結，產業詞只連到人工設定的籃子（3～12 檔），代號一律比對官方名稱。停用約 1 萬 2 千筆錯誤連結。"),
        ])

    with tabs[2]:
        st.markdown("免費的 Gemini 每個模型每天只有約 20 次請求，而且常常忙線（HTTP 503）。所以每個請求都經過同一個管理器：")
        st.graphviz_chart(_dot(
            'q [label="一批 150 筆\\n關鍵字或新聞", fillcolor="#dceaf5"]; l [label="共用限流器\\n每模型每分鐘 5 次"];'
            'm1 [label="gemini-3.6-flash", fillcolor="#e9e1f2"]; m2 [label="gemini-3.8-flash", fillcolor="#e9e1f2"];'
            'm3 [label="gemini-3.5-flash-lite", fillcolor="#e9e1f2"]; ok [label="寫入資料庫\\n記錄已完成（可續跑）", fillcolor="#dcefe3"];'
            'stop [label="全部額度用完\\n停止，下次排程接著做", fillcolor="#f6dedc"];'
            'q -> l -> m1 -> ok; m1 -> m2 [label="額度用完／忙線"]; m2 -> m3 [label="額度用完／忙線"]; m2 -> ok; m3 -> ok; m3 -> stop [label="也不行"];'
        ), width=820)
        st.markdown(
            "- **每批 150 筆**：一次請求處理更多筆，每天可用的 20 次請求就能處理更多資料。\n"
            "- **續跑紀錄**：已經回答過的項目記在 `data.gemini_progress.json`，中斷後不會重送、不浪費額度。\n"
            "- **不造假**：AI 沒處理到的新聞標示為「未分析」，不會預設成中性。"
        )

    with tabs[3]:
        st.markdown("Google Trends 歷史熱度（pytrends）是非官方介面，常常第一次請求就被擋（HTTP 429）。策略是「被擋就停、下次接著做、重要的先做」：")
        st.graphviz_chart(_dot(
            'p1 [label="① 直接連結的關鍵字", fillcolor="#dcefe3"]; p2 [label="② 主題連結的關鍵字", fillcolor="#f7e7d3"];'
            'p3 [label="③ 其他關鍵字"]; note [label="同一組內：最久沒更新的先抓\\n遇到 429 立刻停止", shape=note];'
            'p1 -> p2 -> p3; p3 -> note [style=dashed, arrowhead=none];'), width=560)
        st.markdown("結果：事件研究用到的關鍵字（有對應股票的），近 3 天有熱度資料的比例從約 2% 提升到 98%。")

    with tabs[4]:
        ui.cards([
            ("去除重複", "熱搜以（關鍵字, 發布時間）、新聞以（熱搜, 網址）當唯一鍵，重複執行不會多出資料，也不會覆蓋已分析的情緒。"),
            ("時間統一", "資料庫一律存國際標準時間，顯示與分組時才換成台北日期；全專案只有一個時間工具模組。"),
            ("資料遷移", "大規模修改資料前先「預覽 → 在副本演練 → 比對計畫雜湊 → 自動備份後套用」，出錯可以一鍵還原。"),
            ("未分析 ≠ 中性", "情緒比例只用已分析的新聞計算，覆蓋率不足時畫面會警告。"),
        ], columns=2)


# ---------- ④ method ----------

def page_method() -> None:
    ui.header("研究方法", "「事件後股價上漲」不等於「熱搜有效」。這頁說明三個常見的假訊號，以及怎麼排除。")
    ui.cards([
        ("陷阱 1：因果顛倒", "股價先大漲，大家才去搜尋。如果從事件前一天算報酬，就把「造成熱搜的漲幅」算成熱搜的功勞。<br><b>解法：</b>從事件當天收盤起算，也就是看到熱搜後最早能買的價格。"),
        ("陷阱 2：大盤一起漲", "行情好的時候，大部分事件的報酬都是正的。<br><b>解法：</b>減掉同期 0050 的漲跌，得到超額報酬。"),
        ("陷阱 3：本來就比較強", "有些股票（或籃子）這段期間本來就一直贏大盤。<br><b>解法：</b>再減掉它「任何一天買進」的平均超額報酬（平常水準），剩下的才是事件的影響。"),
    ])
    st.subheader("實例：三種算法差多少")
    stock = data.build_stock_events("2881", ("direct", "theme"))
    if stock is not None and not stock.events.empty:
        raw = pd.to_numeric(stock.events["return_10d"], errors="coerce").mean()
        excess = pd.to_numeric(stock.events["excess_10d"], errors="coerce").mean()
        normal = stock.normal.get(10, 0.0)
        st.plotly_chart(charts.return_breakdown_chart(raw, excess, excess - normal, "富邦金（2881）"),
                        width="stretch", config={"displaylogo": False})
        st.markdown(
            f"只看原始報酬是 {raw:+.1%}，扣掉大盤後還有 {excess:+.1%}，看起來熱搜很有效；"
            f"但富邦金在這段期間<b>任何一天</b>買進，10 日平均就贏大盤 {normal:+.1%}，扣掉後只剩 {excess - normal:+.1%}。",
            unsafe_allow_html=True)
    st.subheader("顯著性")
    st.markdown(
        "平均值會被運氣影響，所以再算 **t 值**：平均值除以它的不確定程度。|t| ≥ 2 大約代表「純靠運氣出現這種結果的機率小於 5%」。"
        "事件研究頁的假說判定與研究結論都用這個標準。同時看很多組數字時，偶爾出現一兩個顯著是正常的，所以單一結果先當線索、等資料累積再確認。"
    )
    st.subheader("研究設計")
    st.graphviz_chart(_dot(
        'ev [label="事件\\n（公司或產業詞）", fillcolor="#dceaf5"]; d0 [label="第 0 天收盤", fillcolor="#f7e7d3"];'
        'ar [label="之後 1/3/5/10 日報酬"]; ex [label="− 同期 0050\\n= 超額報酬", fillcolor="#dceaf5"];'
        'ab [label="− 平常水準\\n= 事件效果", fillcolor="#dcefe3"]; t [label="t 檢定\\n|t| ≥ 2 才算顯著", fillcolor="#2f6b4f", fontcolor="white"];'
        'ev -> d0 -> ar -> ex -> ab -> t;'), width="stretch")
    results = data.research_results()
    paths = results["paths"]
    end = paths[paths["offset"].eq(10)].set_index("kind") if not paths.empty else pd.DataFrame()
    if not end.empty:
        rows = [{"事件種類": {"direct": "直接（公司本身）", "theme": "主題（同類股籃子）"}[k],
                 "事件數": int(r["events"]), "熱搜前後累積（未扣平常）": f"{r['car']:+.2%}",
                 "扣平常水準後": f"{r['car_adjusted']:+.2%}", "t 值": f"{r['car_adjusted'] / r['se']:+.2f}" if r["se"] else "—"}
                for k, r in end.iterrows()]
        st.markdown("目前結果（第 −1 天收盤到第 10 天）：")
        st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("完整方法與每次重跑的紀錄：tools/hypothesis_study.py、docs/hypothesis_study.md。")


# ---------- ⑤ quality and operations ----------

def _test_count() -> int:
    return sum(len(re.findall(r"^\s+def test_", path.read_text(encoding="utf-8"), re.M)) for path in (ROOT / "tests").glob("test_*.py"))


def _health_summary() -> tuple[str | None, str | None]:
    path = ROOT / "logs" / "health_report.md"
    if not path.is_file():
        return None, None
    text = path.read_text(encoding="utf-8")
    title = re.search(r"Project Health Report — (.+)", text)
    score = re.search(r"系統健康度：(\d+ / 100)", text)
    return (title.group(1) if title else None), (score.group(1) if score else None)


def page_quality() -> None:
    ui.header("品質與維運", "系統每天自己跑，所以要能自己發現問題、出事時能復原。")
    status = data.system_status()
    when, score = _health_summary()
    columns = st.columns(4)
    columns[0].metric("自動更新", status["label"].split(" ", 1)[1])
    columns[1].metric("健康報告分數", score or "—", help=f"logs/health_report.md（{when}）" if when else None)
    columns[2].metric("自動化測試", f"{_test_count()} 個")
    columns[3].metric("雲端備份", "每天 03:30", help="壓縮後存到 Google 雲端硬碟，保留最近 14 份。")

    st.subheader("每天的自我檢查")
    st.graphviz_chart(_dot(
        'h [label="健康報告 07:30", fillcolor="#f7e7d3"]; r [label="RSS：近 24 小時跑了幾次？"]; s [label="股價：最新交易日對嗎？"];'
        'g [label="熱度：直接連結的覆蓋率"]; a [label="Gemini：處理量、錯誤數"]; e [label="ETL：上次結束狀態"];'
        'out [label="0–100 分\\n✅ ⚠️ ❌ 與最近錯誤", shape=note];'
        'h -> r; h -> s; h -> g; h -> a; h -> e; r -> out; s -> out; g -> out; a -> out; e -> out;'), width=760)

    st.subheader("遇過的問題與解法")
    cases = [
        ("重複資料", "唯一鍵與冪等寫入", "重複執行結果不變"),
        ("日期偏移", "全專案統一時間工具，存 UTC、顯示台北時間", "事件歸屬正確"),
        ("未分析被當中性", "區分「尚未分析」與「中性」", "情緒比例可信"),
        ("Gemini 額度不夠、常忙線", "每批 150 筆、多模型備援、續跑紀錄", "待處理新聞從 5,000 筆降到個位數"),
        ("Google Trends 限流", "被擋就停、重要關鍵字優先輪替", "有股票的關鍵字熱度覆蓋率約 2% → 98%"),
        ("AI 把關鍵字連錯股票", "直接／主題兩種連結、代號比對官方名稱", "停用約 1.2 萬筆錯誤連結"),
        ("大盤上漲造成假訊號", "扣 0050、扣平常水準、t 檢定", "正確判定假說未獲支持"),
        ("股價清單漏掉 140 檔", "股票池改讀資料表、支援上櫃與帶字母 ETF", "75 檔 → 216 檔"),
        ("資料庫接近 GitHub 上限", "改為每日壓縮備份到雲端硬碟", "repo 從 1.2 GB 縮到 47 MB"),
    ]
    st.dataframe(pd.DataFrame(cases, columns=["問題", "解法", "成果"]), hide_index=True, width="stretch")

    st.subheader("出事時怎麼復原")
    ui.cards([
        ("電腦關機一段時間", "排程設定「錯過就補跑」，開機後自動補上；RSS 熱搜補不回來的時段會記錄為資料空窗，分析時排除。"),
        ("資料庫損毀或誤刪", "從 Google 雲端硬碟最新的 zip 解壓 data.db 即可；大規模修改前另有本機備份。"),
        ("資料修改出錯", "資料遷移工具會先自動備份，並提供還原指令與檢查碼。"),
    ])
