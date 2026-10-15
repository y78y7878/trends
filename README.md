# Trends & Theme 市場事件分析平台

以 Google Trends 熱搜、新聞討論與股票行情建立事件研究流程，觀察熱門主題出現後的市場反應。這是作品集與研究驗證專案，不預測股價、不產生交易訊號，也不構成投資建議。

## 研究問題

當一個熱門話題受到大量搜尋與新聞討論時，相關股票是否仍有投資機會？平台聚焦於可觀察的事件與價格變化，讓使用者檢視不同主題的反應差異，而不將歷史關聯解讀為因果關係。

研究假說：

1. 搜尋熱度明顯增加後，相關股票未來數個交易日出現正報酬的比例可能提高。
2. 正面新聞的平均股價反應可能大於負面新聞。
3. AI、科技、金融、能源、生技、消費及半導體等主題可能有不同反應速度。
4. 搜尋熱度高峰與 MA20、MA60 均線突破之間可能存在時間差。

## 使用者介面

平台只保留三個核心頁面：

- **專案介紹**：研究問題、假說、資料流程、ETL、系統架構、資料表關聯與作品集價值。
- **市場雷達**：近期主題熱度、新聞情緒、相關股票近 10 日表現與首次反應天數。
- **事件研究**：依主題、股票和日期範圍，整合搜尋熱度、股價、MA20、MA60、新聞情緒及事件日；圖表下方列出事件報酬。

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
- **Load**：資料集中保存於專案根目錄的 SQLite `data.db`，不依賴雲端資料庫。

主要資料表：`google_trends`、`google_trends_news`、`keyword_classification`、`theme_mapping`、`google_trends_history`、`stocks`、`event_analysis`。主題與股票關係由 `theme_mapping` 管理；事件報酬以事件日對齊後的交易日行情計算。

## 事件研究方法

符合任一條件即標記研究事件：

- 當日搜尋熱度相較前 7 日平均增加至少 50%。
- 當日新聞至少 3 篇，且篇數達前 7 日平均的 2 倍以上。

非交易日事件會對齊至下一個有行情的交易日，以該日收盤價作為報酬基準。事件後 1、3、5、10 個交易日報酬以收盤價計算；最大漲幅與最大跌幅是事件後 10 個交易日區間內高低價相對基準收盤價的變化。首次反應天數為事件後 1–30 個交易日內，收盤價相對事件日收盤首次變動達 ±1% 的交易日數；30 個交易日內未達門檻則視為資料不足。市場雷達的平均首次反應天數使用每主題最近最多 50 個事件、每個關鍵字一檔映射股票，僅作為描述性觀察指標。

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

`data.db` 隨 repo 提供；程式啟動時會自動建立缺少的資料表。

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

會在工作排程器的 `\Trends\` 資料夾建立四個工作；設定細節、驗證與排除方式見 [docs/task_scheduler.md](docs/task_scheduler.md)。

### 啟動 Dashboard

```powershell
.venv\Scripts\streamlit.exe run app.py
```

Dashboard 只讀取資料庫，不負責定期更新；資料由排程工作寫入。

## 排程架構

```text
Windows 工作排程器 \Trends\
│
├─ RSS Collector    每 10 分鐘        run.bat
│    └─ trends.rss_collector --once
│         → google_trends、google_trends_news
│
├─ Stock Collector  週一至週五 18:30  run_stocks.bat
│    └─ trends.stock_collector
│         → stocks（每檔重抓近 14 天）
│
├─ Daily ETL        每天 4 次         run_etl.bat（02:00、08:00、14:00、20:00）
│    └─ trends.etl --keyword-limit 400 --news-limit 1000（每個 Gemini 請求 50 筆）
│         1. Entity Resolution         → entity_master、keyword_classification
│         2. 關鍵字 AI 分類（Gemini）   → keyword_classification、theme_mapping
│         3. 未分類關鍵字自動分類
│         4. 歷史熱度（pytrends，近 90 天）→ google_trends_history
│         5. 新聞情緒與事件類型（Gemini）→ google_trends_news
│         6. 新聞主題彙整              → news_theme_classification
│         7. 主題每日統計              → theme_daily_stats
│
└─ Health Report    每天 07:30        run_health.bat
     └─ trends.health_report（唯讀）
          → logs\health_report.md：五個元件的狀態、健康度分數、今日新增量、覆蓋率、最近錯誤

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
| `google_trends` | Google Trends 台灣 RSS | 每 10 分鐘 | `run.bat` | RSS 當下約 10 筆熱搜 | 無法回補；RSS 只提供當下熱搜 |
| `google_trends_news` | RSS 內附新聞 | 每 10 分鐘 | `run.bat` | 隨熱搜一併收集 | 同上 |
| `stocks` | yfinance 日線 | 交易日 18:30 | `run_stocks.bat` | 每檔最近 14 天（新股票回補 5 年） | 自動；缺口不超過 14 天 |
| `google_trends_history` | pytrends 每日熱度 | 每天 4 次 | `run_etl.bat` | 啟用主題關鍵字的近 90 天，最久未更新的優先（同樣久時有對應股票的優先），到第一個 429 為止 | 自動；缺口不超過 90 天 |
| `keyword_classification`、`entity_master`、`theme_mapping` | Gemini + 規則 | 每天 4 次 | `run_etl.bat` | 種子與產業規則套用到全部待處理關鍵字；Gemini 每次最多 400 個 | 積壓會在之後幾次執行中消化 |
| `google_trends_news`（情緒、事件類型） | Gemini | 每天 4 次 | `run_etl.bat` | 每次最多 1,000 篇待分析新聞 | 同上 |
| `news_theme_classification` | 彙整關鍵字主題與新聞情緒 | 每天 4 次 | `run_etl.bat` | 每次最多 1,000 篇尚未彙整的新聞 | 同上 |
| `theme_daily_stats` | 由上列資料表彙總 | 每天 4 次 | `run_etl.bat` | 重算主題每日統計 | 自動 |
| 事件研究結果 | 由上列資料表即時計算 | 開啟頁面時 | Dashboard | 依頁面選擇的主題、股票與日期 | 不需要 |

資料新鮮度的預期：熱搜與新聞延遲約 10 分鐘；股價為前一個交易日收盤（當天 18:30 後為當日收盤）；分類、情緒與歷史熱度最多落後約 6 小時（最近一次 ETL）。Google Trends 限流（HTTP 429）時，歷史熱度會暫停更新，限流解除後的第一次執行會自動補齊。

主題、關鍵字與股票對應維護於 `theme_mapping.csv`；股票與公司別名維護於 `keyword_theme_mapping.csv` 及 `entity_seed_mapping.csv`。

## 專案能力展示

- **資料工程**：多來源 ETL、自動化資料收集、SQLite 資料模型。
- **資料分析**：事件研究、搜尋熱度、新聞情緒與股票報酬。
- **商業分析**：市場主題辨識、事件影響評估與市場反應驗證。
- **資料視覺化**：多來源單圖整合、事件績效表與聚焦式儀表板。# Social Media Trends Scraper (社群與搜尋趨勢爬蟲)

這是一個用來自動抓取台灣 Google Trends 熱搜關鍵字及社群平台（目前支援PTT）熱門文章的 Python 爬蟲專案。透過自動化瀏覽器側錄 API 封包，獲取熱門文章的標題、連結、作者及互動熱度，並將資料持久化儲存至 MariaDB 資料庫中，以利後續的數據分析或趨勢落差比對。

## ✨ 特色 (Features)
* **無頭瀏覽器側錄**：使用 Playwright 模擬真實瀏覽器行為，透過監聽網路請求（Network Interception）側錄原生 API 回應，減少直接打 API 被阻擋的風險。
* **Cloudflare 防護繞過**：針對 Dcard 的 JavaScript 挑戰（JS Challenge）與 403/429 限制，採用頁面自然載入與等待機制處理。
* **資料庫整合**：自動解析時間格式並計算「互動分數」（讚數 + 留言數），統一寫入 MariaDB。

## 🛠️ 環境需求 (Prerequisites)
* Python 3.8+
* MariaDB / MySQL
* Chromium (透過 Playwright 安裝)

## 🚀 快速開始 (Quick Start)

### 1. 安裝套件
請先在您的虛擬環境中安裝所需的 Python 套件：

```bash
pip install playwright pymysql python-dateutil requests
```

### 2. 初始化 Playwright 瀏覽器
安裝 Playwright 運行所需的 Chromium 核心：

```bash
playwright install chromium
```

### 3. 建置資料庫
請進入您的 MariaDB 控制台，建立資料庫並執行以下 SQL 語法建立相關資料表：

```sql
CREATE DATABASE IF NOT EXISTS trends CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE trends;

-- 1. 建立社群趨勢資料表
CREATE TABLE IF NOT EXISTS social_trends (
    id INT AUTO_INCREMENT PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,
    category VARCHAR(100),
    title VARCHAR(500) NOT NULL,
    content_url VARCHAR(1000),
    author VARCHAR(255),
    engagement_score INT DEFAULT 0,
    published_at DATETIME,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_platform_fetched (platform, fetched_at),
    INDEX idx_title (title)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. 建立 Google Trends 主資料表
CREATE TABLE IF NOT EXISTS google_trends (
    id INT AUTO_INCREMENT PRIMARY KEY,
    keyword VARCHAR(255) NOT NULL,
    approx_traffic VARCHAR(50),
    published_at DATETIME,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_keyword (keyword),
    INDEX idx_published (published_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. 建立 Google Trends 新聞子資料表
CREATE TABLE IF NOT EXISTS google_trends_news (
    id INT AUTO_INCREMENT PRIMARY KEY,
    trend_id INT NOT NULL,
    news_title VARCHAR(500),
    news_url VARCHAR(1000),
    news_source VARCHAR(255),
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trend_id) REFERENCES google_trends(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 4. 設定資料庫密碼
在爬蟲的 Python 檔案中，找到資料庫連線區塊，將密碼修改為您本地端的設定：

```python
conn = pymysql.connect(
    host="localhost",
    user="root",
    password="your_password", # 請替換為您的密碼
    database="trends",
    charset="utf8mb4",
    autocommit=True
)
```

### 5. 執行爬蟲
```bash
python dcard_trends.py
```

## ⚠️ 已知問題與待辦事項 (Known Issues & TODOs)
- [ ] **Dcard 爬取穩定性問題**：目前 Dcard 的 Cloudflare 防護極為嚴格，偶爾會觸發 `HTTP 403 (Forbidden)` 阻擋或 `HTTP 429 (Too Many Requests)` 限流。目前的解法是攔截前端原生封包，但若同 IP 請求過於頻繁，仍可能暫時抓不到資料（需等待 3~5 分鐘冷卻）。後續考慮引入代理池（Proxy Pool）或拉長排程間距。
- [ ] 支援更多社群平台（如 PTT、Threads 等）。
- [ ] 新增資料重複性檢查（使用 `content_url` 判斷是否已存在於資料庫）。

## 📜 免責聲明 (Disclaimer)
本專案僅供程式語言學習與技術研究之用。請遵守各平台的服務條款（Terms of Service），切勿將此工具用於惡意攻擊、高頻率壓測或商業營利用途。

## Google Trends 與台股事件驅動分析平台

此平台沿用根目錄的 `data.db` 與既有 `google_trends`、`google_trends_news` RSS 資料表。啟動時會建立行情/事件表，並為舊版新聞表補上 `news_sentiment` 欄位。

### 專案結構

```text
app.py                         Streamlit 研究 Dashboard
schema.sql                     SQLite 完整 CREATE TABLE 範例
keyword_theme_mapping.csv      company/industry/theme 多股票對應表
src/trends/database.py         SQLAlchemy models、SQLite 初始化與相容遷移
src/trends/stock_collector.py  yfinance 歷史回補、增量更新與每日排程
src/trends/features.py         前/後 1、3、5、10 日報酬及成交量變化
src/trends/keyword_mapping.py  RapidFuzz 事件分類與多股票配對
src/trends/event_study.py      多股票事件對齊、資料持久化與事件統計
src/trends/alpha_signal.py     Alpha Signal 分數與狀態標籤
src/trends/keyword_classification.py Gemini 關鍵字正規化、主題分類及新聞事件/情緒標註
src/trends/entity_resolution_etl.py Entity Resolution、公司產業對應與實體覆蓋率
src/trends/etl.py              統一更新流程、系統健康度及 Analytics ETL
src/trends/trend_history_collector.py pytrends 近 90 日歷史熱度收集器
entity_seed_mapping.csv        人工維護的公司實體、股票代號、產業及別名
```

### 安裝與執行

```powershell
pip install -e .
streamlit run app.py
```

首次行情更新會回補最多五年資料，後續只更新最近區間並重算有修訂的日期。平台分析與圖表只使用 2026-08-21 起的研究期間。正式運營由工作排程器每個交易日 18:30 執行 `run_stocks.bat`（見[排程架構](#排程架構)）；手動抓取一次：

```powershell
python -m trends.stock_collector
```

預設股票池由 `keyword_theme_mapping.csv` 去重產生，涵蓋約 48 檔台股 ETF、產業代表股及美股；台股代號會轉為 yfinance 的 `.TW` 格式。映射欄位為 `keyword,type,stock_id`，其中 `type` 為 `company`、`industry` 或 `theme`，同一 keyword 可有多筆股票。

若需重新建立／檢查 schema，使用 `schema.sql`；程式的 `init_db()` 會自動建立資料表並遷移舊新聞表。資料欄位及索引定義以 [schema.sql](schema.sql) 為準。

### 主題研究與歷史熱度

第二頁「主題研究」以 `theme_mapping.csv` 定義主題、關鍵字、股票與分類。第一次啟動時會依內建分類自動產生 CSV，並同步到 `theme_mapping`；若要調整映射，編輯 CSV 後重啟 app。Event Cluster 和 RSS 表仍保留為即時事件偵測及新聞時間軸資料來源。

`google_trends_history` 儲存 pytrends 收集的每日 0–100 熱度分數，供 Pearson heatmap 與 -10 至 +10 交易日 Cross Correlation 使用。首次回補及手動單次更新：

```powershell
python -m trends.trend_history_collector
```

正式運營時，歷史熱度由每天 4 次的 `run_etl.bat` 一併更新（見[排程架構](#排程架構)），不需另外排程。

### Warehouse ETL

原始 `google_trends`、`google_trends_news`、`stocks`、`event_analysis` 資料不會被分類流程更新或刪除。`keyword_classification` 以原始 keyword 為主鍵，保存 AI canonical keyword、主題/次分類、股票關聯與信心分數；AI 結果會寫入 `theme_mapping`，歷史分數、新聞 enrichment 與 `theme_daily_stats` 以可重跑方式 upsert。

關鍵字分類前會先執行 Entity Resolution：優先使用 `entity_seed_mapping.csv` 與股票映射辨識公司，再以 Gemini 補充其他實體；上市公司主題依產業規則決定。可單獨執行或回補 Entity Layer：

```powershell
python -m trends.entity_resolution_etl --limit 500
```

`entity_master` 保存 canonical entity 與類型；`keyword_classification` 另保存 `entity_name`、`entity_type`。市場總覽的 Entity Coverage 顯示辨識率及未辨識關鍵字 TOP 100。

需在環境變數或 `.env` 設定 `GEMINI_API_KEY`（亦支援 `GOOGLE_API_KEY`）。單次執行完整 ETL：

```powershell
python -m trends.etl
```

預設每次最多分類 200 個關鍵字與 200 篇新聞（各 10 次 Gemini 批次請求）；可使用 `--news-limit 0` 處理全部待分類新聞。若 API 回報每日配額耗盡，該層會停止並將剩餘列留待下次排程。正式運營由工作排程器每天台北時間 02:00、08:00、14:00、20:00 執行 `run_etl.bat`（每次最多 400 個關鍵字、1,000 篇新聞，每個 Gemini 請求 50 筆）（見[排程架構](#排程架構)）。

Dashboard 的「分類 RSS 關鍵字與新聞」按鈕則以每次 100 筆增量處理。所有語言與新聞來源都會納入；主題透過所屬 Trends keyword 的 classification 關聯，不依新聞語言或來源篩選。無 API key 時，keyword 可使用明確標記為 `fallback` 的規則分類；新聞 AI 分類會保留待處理，不會填入假造的 AI 結果。

每日關鍵字數較多時，回補可能需要數分鐘；Google Trends 可能限流，Collector 會記錄失敗關鍵字並繼續處理其餘項目。正 Lag 表示關鍵字熱度先於股票日報酬，負 Lag 表示股價報酬先行。新聞後 5 日報酬以新聞日期之後的下一個交易日收盤作為觀察起點。

### 事件研究定義

RSS 的 `published_at` 作為事件日期；若當天不是交易日，會對齊至下一個有行情的交易日。前 1/3/5/10 日報酬用事件日收盤價相對過去收盤價計算；事件後報酬從對齊後的事件日收盤價起算。研究摘要回報各持有期間的平均報酬、勝率、最大漲幅與最大跌幅。Alpha 分數是熱度、成交量變化、新聞情緒與「只使用較早事件」計算的歷史勝率之加權排序，不是投資建議或預測保證。
