# Trends & Theme 市場事件分析平台

以 Google Trends 熱搜、新聞討論與股票行情建立事件研究流程，觀察熱門主題出現後的市場反應。這是作品集與研究驗證專案，不預測股價、不產生交易訊號，也不構成投資建議。

## 研究問題

當一個熱門話題受到大量搜尋與新聞討論時，相關股票是否仍有投資機會？平台聚焦於可觀察的事件與價格變化，讓使用者檢視不同主題的反應差異，而不將歷史關聯解讀為因果關係。

研究假說：

1. 搜尋熱度明顯增加後，相關股票未來數個交易日出現正報酬的比例可能提高。
2. 正面新聞的平均股價反應可能大於負面新聞。
3. AI、科技、金融、能源、生技、消費及半導體等主題可能有不同反應速度。
4. 搜尋熱度高峰與 MA20、MA60 均線突破之間可能存在時間差。
5. 產業詞上熱搜時（例如「保險」「伺服器」），同類股票可能一起反應。

## 研究發現（2026-11-05，資料期間 2026-08-21～11-05）

- **注意力和股價大動同時發生**：熱搜前 5 天股價已比大盤多漲約 2%，熱搜當天波動約是平常的 2 倍，比較像是股價先動，大家才去搜尋。PTT 股票板的討論爆量也一樣，沒有比熱搜早。
- **熱搜之後沒有可利用的超額報酬**：從熱搜當天收盤起算，扣除 0050 與個股平常水準後，10 日反而略為回吐（−1.7%，t = −3.1）。
- **同類股連動（假說 5）目前未獲支持**：主題籃子事件後看似上漲，但扣除籃子平常的漲幅後接近 0。
- **事件後波動較大，但看股價本身就能預測**：控制最近 20 天的波動後，熱搜、PTT、搜尋熱度都沒有額外的預測力，所以網站在個股事件圖顯示「近 20 日波動」。

樣本約 2.5 個月，結論會隨資料累積每月重跑更新。方法、完整數字與限制見 [docs/hypothesis_study.md](docs/hypothesis_study.md)。

## 使用者介面

導覽列分成兩組（程式在 `src/trends/web/`）：

**使用者**（附教學，容易上手）
- **快速上手**：三步驟教學、怎麼看個股事件圖、名詞解釋。
- **個股事件圖**：選一檔股票，三層圖共用時間軸：股價（圓圈數字＝事件，顏色＝事件後 10 日扣大盤的表現）、各關鍵字搜尋熱度、每日新聞情緒；上方顯示近 20 日波動是平常的幾倍；下方事件卡片列出觸發原因、新聞概要與標題、事件後 1／5／10 日報酬與超額報酬，以及事件前後的波動。
- **市場雷達**：各主題的搜尋熱度變化、新聞情緒與同類股近 10 日表現，一鍵跳到代表股的事件圖。
- **研究結論**：熱搜前後 −5～+10 日的平均累積超額報酬圖，用白話回答「看到熱搜再買有用嗎」；另附注意力與波動的研究結果。

**開發者／作品集**
- **專案總覽**：動機、即時資料量、技術與成果。
- **系統架構**：架構圖、一天的排程、資料表關聯圖、程式結構。
- **資料處理邏輯**：事件判定、關鍵字連到股票的決策流程、Gemini 管理、熱度輪替、資料可信度。
- **研究方法**：三個假訊號陷阱與解法、實例、顯著性。
- **品質與維運**：自動更新狀態、健康報告、測試數、遇過的問題與解法、復原方式。

資料由排程自動更新，網站只讀資料庫、每 5 分鐘重新讀取，不提供手動更新按鈕。

## 資料流程

```text
Google Trends 熱搜
    ↓
熱門關鍵字與熱度
    ↓
相關新聞蒐集
    ↓
AI 關鍵字分類與新聞情緒分析
    ↓
主題與股票關聯建立
    ↓
股價資料下載
    ↓
事件資料集建立與事件報酬計算
    ↓
視覺化分析
```

### ETL

- **Extract**：Google Trends 關鍵字、熱度與時間；新聞標題、來源與時間；股票開高低收及成交量。
- **Transform**：關鍵字正規化、AI 主題分類、Positive／Neutral／Negative 新聞情緒分析、主題股票映射與事件報酬計算。
- **Load**：資料集中保存於專案根目錄的 SQLite `data.db`，不依賴雲端資料庫（每天備份到雲端硬碟）。

主要資料表：`google_trends`、`google_trends_news`、`google_trends_observations`、`keyword_classification`、`theme_mapping`、`google_trends_history`、`stocks`、`event_analysis`。主題與股票關係由 `theme_mapping` 管理；事件報酬以事件日對齊後的交易日行情計算。

## 事件研究方法

符合任一條件即標記研究事件：

- 關鍵字登上 Google Trends 熱搜，且前 3 天都沒有上榜（「登上熱搜」，事件大小以熱搜流量級距表示）。
- 當日搜尋熱度相較前 7 日平均增加至少 50%。
- 當日新聞至少 3 篇，且篇數達前 7 日平均的 2 倍以上。

事件分成兩種來源：**直接**（關鍵字就是該公司）與**主題**（產業詞，連到該主題的同類股籃子），事件研究頁可以分開檢視。

非交易日事件會對齊至下一個有行情的交易日，以該日收盤價作為報酬基準。判斷熱搜是否有效時看**超額報酬**：扣除同期 0050，再扣除這檔股票在期間內任一天起算的平常水準，並以 t 值判斷是否顯著；只看原始報酬會把大盤上漲誤當成熱搜效果。事件後 1、3、5、10 個交易日報酬以收盤價計算；最大漲幅與最大跌幅是事件後 10 個交易日區間內高低價相對基準收盤價的變化。首次反應天數為事件後 1–30 個交易日內，收盤價相對事件日收盤首次變動達 ±1% 的交易日數；30 個交易日內未達門檻則視為資料不足。市場雷達的平均首次反應天數使用每主題最近最多 50 個事件、每個關鍵字一檔映射股票，僅作為描述性觀察指標。

事件門檻是透明的研究規則，不代表統計顯著性。結果受資料期間、主題映射、新聞覆蓋與樣本數影響，不能推論因果或未來績效。

## 部署

### 環境需求

- Windows 10 以上（排程使用 Windows 工作排程器）
- Python 3.14 以上，虛擬環境位於專案根目錄 `.venv`（批次檔固定使用 `.venv\Scripts\python.exe`）
- Gemini API 金鑰（關鍵字與新聞 AI 分類用；沒有金鑰時，系統不會偽造新聞情緒分析結果）

### 安裝

```powershell
git clone <repo-url> C:\proj\trends
cd C:\proj\trends
uv sync                          # 或：python -m venv .venv; .venv\Scripts\pip install -e .
```

在專案根目錄建立 `.env`（已列於 `.gitignore`）：

```text
GEMINI_API_KEY=<your-key>
```

`data.db` 不在 repo 中（2026-11-03 起；約 80 MB，接近 GitHub 單檔 100 MB 上限）。每天 03:30 由 `run_backup.bat` 壓縮備份到 Google 雲端硬碟 `我的雲端硬碟\trends_backup\data_YYYYMMDD.zip`（保留 14 份；位置可用環境變數 `TRENDS_BACKUP_DIR` 改）。新電腦或還原時，在沒有排程執行的時候把 zip 中的 `data.db` 與 `data.gemini_progress.json` 解壓到專案根目錄；沒有 `data.db` 時程式會建立空的資料庫與資料表。

### 首次執行

先各手動跑一次，確認環境無誤並補齊資料缺口：

```powershell
.\run.bat                        # RSS 熱搜與新聞
.\run_stocks.bat                 # 股價（自動補齊缺少的交易日）
.\run_etl.bat                    # 分類、歷史熱度、主題統計
```

結果寫在 `logs\`。分類積壓較多時，可以一次處理全部待分類資料（受 Gemini 每日配額限制，用完會自動停止，剩餘留待下次）：

```powershell
.venv\Scripts\python.exe -m trends.etl --keyword-limit 0 --news-limit 0
```

### 註冊排程

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register_tasks.ps1
```

會在工作排程器的 `\Trends\` 資料夾建立五個工作（RSS、股價、ETL、健康報告、資料庫備份）；設定細節、驗證與排除方式見 [docs/task_scheduler.md](docs/task_scheduler.md)。

### 啟動 Dashboard

```powershell
.venv\Scripts\streamlit.exe run app.py
```

Dashboard 只讀取資料庫，不負責定期更新；資料由排程工作寫入。

## 排程架構

```text
Windows 工作排程器 \Trends\
│
├─ RSS Collector    每 20 分鐘        run.bat
│    └─ trends.rss_collector --once
│         → google_trends、google_trends_news
│
├─ Stock Collector  週一至週五 18:30  run_stocks.bat
│    └─ trends.stock_collector
│         → stocks（每檔重抓近 14 天）
│
├─ Daily ETL        每天 4 次         run_etl.bat（02:00、08:00、14:00、20:00）
│    └─ trends.etl --keyword-limit 400 --news-limit 1000（每個 Gemini 請求 150 筆）
│         1. Entity Resolution         → entity_master、keyword_classification
│         2. 關鍵字 AI 分類（Gemini）   → keyword_classification、theme_mapping
│         3. 未分類關鍵字自動分類
│         4. 歷史熱度（pytrends，近 90 天）→ google_trends_history
│         5. 新聞情緒與事件類型（Gemini）→ google_trends_news
│         6. 新聞主題彙整              → news_theme_classification
│         7. 主題每日統計              → theme_daily_stats
│
├─ Health Report    每天 07:30        run_health.bat
│    └─ trends.health_report（唯讀）
│         → logs\health_report.md：五個元件的狀態、健康度分數、今日新增量、覆蓋率、最近錯誤
│
└─ DB Backup        每天 03:30        run_backup.bat
     └─ trends.db_backup（唯讀快照）→ Google 雲端硬碟 trends_backup\data_YYYYMMDD.zip，保留 14 份

Streamlit Dashboard（app.py）── 唯讀查詢 data.db（快取 5 分鐘）
```

設計原則：

- **一次執行即結束**：每個工作都是跑完就退出的程式，由工作排程器負責觸發。不使用各模組的 `--schedule` 常駐模式，避免關閉終端機或重開機後中斷。
- **冪等、可重跑**：RSS 以唯一索引去重；股價與歷史熱度用 upsert，每次重抓一段回溯期間，錯過的執行會在下一次自動補齊。
- **錯過即補跑**：工作設定「錯過排程時間後儘快執行」，電腦在排程時間關機時，開機後會補跑。
- **不重疊**：同一工作前一次尚未結束時不啟動新的一次；股價（18:30）與 ETL（02:00、08:00、14:00、20:00）錯開，降低 SQLite 同時寫入的機會。
- **Log 分流**：`logs\rss_collector.log`、`logs\stock_collector.log`、`logs\etl.log`、`logs\health.log`。
- **每天看一份報告**：`logs\health_report.md`，判定規則見 [docs/data_pipeline.md](docs/data_pipeline.md#每日健康報告建議只看這一份)。

`python -m trends.etl` 不更新股價；股價只由 `run_stocks.bat`（`trends.stock_collector`）或 Dashboard 側邊欄的「更新研究資料」按鈕更新。

## 資料更新頻率

| 資料表 | 來源 | 更新頻率 | 更新程式 | 每次處理範圍 | 缺口回補 |
|---|---|---|---|---|---|
| `google_trends` | Google Trends 台灣 RSS | 每 20 分鐘 | `run.bat` | RSS 當下約 10 筆熱搜 | 無法回補；RSS 只提供當下熱搜 |
| `google_trends_news` | RSS 內附新聞 | 每 20 分鐘 | `run.bat` | 隨熱搜一併收集 | 同上 |
| `stocks` | yfinance 日線 | 交易日 18:30 | `run_stocks.bat` | 每檔最近 14 天（新股票回補 5 年） | 自動；缺口不超過 14 天 |
| `google_trends_history` | pytrends 每日熱度 | 每天 4 次 | `run_etl.bat` | 啟用主題關鍵字的近 90 天，最久未更新的優先（同樣久時有對應股票的優先），到第一個 429 為止 | 自動；缺口不超過 90 天 |
| `keyword_classification`、`entity_master`、`theme_mapping` | Gemini + 規則 | 每天 4 次 | `run_etl.bat` | 種子與產業規則套用到全部待處理關鍵字；Gemini 每次最多 400 個 | 積壓會在之後幾次執行中消化 |
| `google_trends_news`（情緒、事件類型） | Gemini | 每天 4 次 | `run_etl.bat` | 每次最多 1,000 篇待分析新聞 | 同上 |
| `news_theme_classification` | 彙整關鍵字主題與新聞情緒 | 每天 4 次 | `run_etl.bat` | 每次最多 1,000 篇尚未彙整的新聞 | 同上 |
| `theme_daily_stats` | 由上列資料表彙總 | 每天 4 次 | `run_etl.bat` | 重算主題每日統計 | 自動 |
| 事件研究結果 | 由上列資料表即時計算 | 開啟頁面時 | Dashboard | 依頁面選擇的主題、股票與日期 | 不需要 |

資料新鮮度的預期：熱搜與新聞延遲約 20 分鐘；股價為前一個交易日收盤（當天 18:30 後為當日收盤）；分類、情緒與歷史熱度最多落後約 6 小時（最近一次 ETL）。Google Trends 限流（HTTP 429）時，歷史熱度會暫停更新，限流解除後的第一次執行會自動補齊。

主題、關鍵字與股票對應維護於 `theme_mapping.csv`；股票與公司別名維護於 `keyword_theme_mapping.csv` 及 `entity_seed_mapping.csv`。

`theme_mapping.link_type` 區分關鍵字與股票的兩種連結（2026-10-30 起，`trends.migrations.p1_link_type`）：

- `direct`：關鍵字就是該公司或 ETF（實體辨識或公司別名），只連到它自己的股票。
- `theme`：產業詞（例如「保險」「伺服器」），連到 `theme_mapping.csv` 中該主題人工設定的股票籃子，用來研究「熱門詞是否帶動同類股」。
- 人名、未在台灣上市的外國公司、事件等不連股票。一般用語剛好是公司簡稱的（`AMBIGUOUS_COMPANY_WORDS`，例如「創意」「全新」）也不連。

事件研究頁的「事件來源」可只看直接或主題事件；歷史熱度收集依序優先 `direct`、`theme`。

## 專案能力展示

- **資料工程**：多來源 ETL、自動化資料收集、SQLite 資料模型。
- **資料分析**：事件研究、搜尋熱度、新聞情緒與股票報酬。
- **商業分析**：市場主題辨識、事件影響評估與市場反應驗證。
- **資料視覺化**：多來源單圖整合、事件績效表與聚焦式儀表板。

## 技術參考

### 專案結構

```text
app.py                                Streamlit Dashboard 入口
run.bat / run_stocks.bat / run_etl.bat / run_health.bat / run_backup.bat   工作排程器呼叫的批次檔
deploy/register_tasks.ps1             註冊 \Trends\ 排程（deploy/run_hidden.vbs 隱藏視窗執行）
schema.sql                            SQLite 完整 CREATE TABLE 範例
theme_mapping.csv                     主題、關鍵字與人工設定的同類股籃子
keyword_theme_mapping.csv             公司別名與股票代號（company / industry / theme）
entity_seed_mapping.csv               人工維護的公司實體、股票代號、產業及別名
src/trends/database.py                SQLAlchemy models、SQLite 初始化與相容遷移
src/trends/rss_collector.py           Google Trends RSS 收集（含每次看到的流量紀錄）
src/trends/stock_collector.py         yfinance 行情回補與增量更新（.TW / .TWO）
src/trends/etl.py                     每日 ETL 流程與執行統計
src/trends/entity_resolution_etl.py   實體辨識：公司 → 自己的股票（direct 連結）
src/trends/keyword_classification.py  Gemini 關鍵字分類（company / industry / none）與新聞情緒
src/trends/listed_companies.py        證交所／櫃買中心官方代號與名稱，用來檢查 Gemini 給的代號
src/trends/gemini_control.py          Gemini 共用限流、備援模型與續跑紀錄
src/trends/trend_history_collector.py pytrends 近 90 日歷史熱度（direct → theme → 其他 輪替）
src/trends/theme_study.py             主題定義、theme_mapping.csv 同步與主題籃子
src/trends/event_study.py             事件偵測、事件報酬與超額報酬、假說判定
src/trends/health_report.py           每日健康報告
src/trends/db_backup.py               data.db 每日雲端備份
src/trends/migrations/                一次性資料遷移（--report / --rehearse / --apply）
tools/hypothesis_study.py             熱搜與股價連動分析（每月重跑，結果記在 docs/hypothesis_study.md）
```

### 手動執行

日常由工作排程器自動執行（見[排程架構](#排程架構)）；需要手動補資料時：

```powershell
.venv\Scripts\python.exe -m trends.stock_collector          # 行情：首次回補 5 年，之後重抓最近 14 天
.venv\Scripts\python.exe -m trends.etl                      # 完整 ETL（實體辨識、分類、熱度、情緒、主題統計）
.venv\Scripts\python.exe -m trends.entity_resolution_etl --limit 500
.venv\Scripts\python.exe -m trends.trend_history_collector  # 只更新歷史熱度
.venv\Scripts\python.exe tools\hypothesis_study.py           # 研究分析
```

股票池為 `keyword_theme_mapping.csv`、`theme_mapping.csv` 與 `theme_mapping` 資料表中所有有效連結的股票（2026-11-01 起，約 215 檔），所以實體辨識新找到的公司也會有股價。台股代號（含 `00631L` 這類字尾帶字母的 ETF）先用 yfinance 的 `.TW`（上市），沒有資料時改用 `.TWO`（上櫃）；log 中 `.TW` 的「possibly delisted」訊息是這個嘗試留下的，不代表抓取失敗。平台分析與圖表只使用 2026-08-21 起的研究期間。

若需重新建立／檢查 schema，使用 `schema.sql`；程式的 `init_db()` 會自動建立資料表並遷移舊表。資料欄位及索引定義以 [schema.sql](schema.sql) 為準。

### 主題與歷史熱度

`theme_mapping.csv` 定義主題、關鍵字與每個主題人工設定的同類股籃子，每次 ETL 或 Dashboard 啟動時同步到 `theme_mapping` 資料表；調整籃子時編輯 CSV 即可。

`google_trends_history` 儲存 pytrends 收集的每日 0–100 熱度分數，作為事件前的熱度基準線。Google Trends 會限流（HTTP 429），收集器在第一次 429 就停止、留待下次執行，並依「direct 連結 → theme 連結 → 其他，同組內最久未更新優先」輪替，所以事件研究用到的關鍵字會先更新。

### ETL 與 AI 分類

原始 `google_trends`、`google_trends_news`、`stocks` 資料不會被分類流程更新或刪除。`keyword_classification` 以原始 keyword 為主鍵，保存 canonical keyword、主題、股票關聯與信心分數；連結寫入 `theme_mapping`，歷史熱度、新聞情緒與 `theme_daily_stats` 以可重跑方式 upsert。

關鍵字分類前先做實體辨識：依序使用 `entity_seed_mapping.csv`、公司別名、官方上市櫃代號（關鍵字本身就是代號時），最後才問 Gemini。Gemini 給的股票代號會比對官方名稱，屬於別家公司就不建立連結。

需在 `.env` 設定 `GEMINI_API_KEY`（亦支援 `GOOGLE_API_KEY`）。單次執行 `trends.etl` 預設最多分類 200 個關鍵字與 200 篇新聞；可使用 `--news-limit 0` 處理全部待分類新聞。正式運營時每次最多 400 個關鍵字、1,000 篇新聞，每個 Gemini 請求 150 筆。某個模型的每日額度用完時會換備援模型，全部用完才停止，剩餘資料留待下次排程。沒有 API key 時，關鍵字使用明確標記為 `fallback` 的規則分類，新聞情緒保留待處理，不會填入假造的 AI 結果。

### 事件研究定義

RSS 的 `published_at`（換算為台北日期）作為事件日期；若當天不是交易日，對齊至下一個有行情的交易日。事件後報酬從對齊後的事件日收盤價起算，也就是看到熱搜後最早能成交的價格。超額報酬 = 事件後報酬 − 同期 0050 報酬；判斷熱搜是否有效時，再扣除這檔股票在期間內任一天起算的平均超額報酬（平常水準），並以 t 值判斷是否顯著。研究摘要另列勝率、最大漲幅與最大跌幅，僅作描述性參考。
