# Social Media Trends Scraper (社群與搜尋趨勢爬蟲)

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
src/trends/sentiment.py        Transformers 多語新聞情緒分類
src/trends/event_study.py      多股票事件對齊、資料持久化與事件統計
src/trends/alpha_signal.py     Alpha Signal 分數與狀態標籤
```

### 安裝與執行

```powershell
pip install -e .
streamlit run app.py
```

首次行情更新會回補最多五年資料，後續只更新最近區間並重算有修訂的日期。平台分析與圖表只使用 2026-08-21 起的研究期間。也可在 Windows 工作排程器中執行每日收集器：

```powershell
python -m trends.stock_collector --schedule
```

不帶 `--schedule` 則立即抓取一次。預設股票池由 `keyword_theme_mapping.csv` 去重產生，涵蓋約 48 檔台股 ETF、產業代表股及美股；台股代號會轉為 yfinance 的 `.TW` 格式。映射欄位為 `keyword,type,stock_id`，其中 `type` 為 `company`、`industry` 或 `theme`，同一 keyword 可有多筆股票。

新聞情緒模型採用多語 Transformers 模型，第一次分析需要下載模型。安裝可選依賴後，在「新聞事件分析」頁按下分類按鈕：

```powershell
pip install transformers torch
```

若需重新建立／檢查 schema，使用 `schema.sql`；程式的 `init_db()` 會自動建立資料表並遷移舊新聞表。資料欄位及索引定義以 [schema.sql](schema.sql) 為準。

### 事件研究定義

RSS 的 `published_at` 作為事件日期；若當天不是交易日，會對齊至下一個有行情的交易日。前 1/3/5/10 日報酬用事件日收盤價相對過去收盤價計算；事件後報酬從對齊後的事件日收盤價起算。研究摘要回報各持有期間的平均報酬、勝率、最大漲幅與最大跌幅。Alpha 分數是熱度、成交量變化、新聞情緒與「只使用較早事件」計算的歷史勝率之加權排序，不是投資建議或預測保證。
