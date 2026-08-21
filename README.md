Markdown# Social Media Trends Scraper (社群熱門文章爬蟲)

這是一個用來自動抓取台灣 Google Trends 熱搜關鍵字及社群平台（目前支援 PTT）熱門文章的 Python 爬蟲專案。透過自動化瀏覽器側錄 API 封包，獲取熱門文章的標題、連結、作者及互動熱度，並將資料持久化儲存至 MariaDB 資料庫中，以利後續的數據分析或趨勢追蹤。

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
pip install playwright pymysql python-dateutil
2. 初始化 Playwright 瀏覽器安裝 Playwright 運行所需的 Chromium 核心：Bashplaywright install chromium
3. 建置資料庫請進入您的 MariaDB 控制台，建立資料庫（例如 trends）並執行以下 SQL 語法建立資料表：SQLCREATE DATABASE IF NOT EXISTS trends CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE trends;

CREATE TABLE social_trends (
    id INT AUTO_INCREMENT PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,
    category VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content_url VARCHAR(255) NOT NULL,
    author VARCHAR(100),
    engagement_score INT DEFAULT 0,
    published_at DATETIME,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
4. 設定資料庫密碼在 dcard_trends.py 檔案中，找到資料庫連線區塊，將密碼修改為您本地端的設定：Pythonconn = pymysql.connect(
    host="localhost",
    user="root",
    password="your_password", # 請替換為您的密碼
    database="trends",
    ...
)
5. 執行爬蟲Bashpython dcard_trends.py
⚠️ 已知問題與待辦事項 (Known Issues & TODOs)[ ] Dcard 爬取穩定性問題：目前 Dcard 的 Cloudflare 防護極為嚴格，偶爾會觸發 HTTP 403 (Forbidden) 阻擋或 HTTP 429 (Too Many Requests) 限流。目前的解法是攔截前端原生封包，但若同 IP 請求過於頻繁，仍可能暫時抓不到資料（需等待 3~5 分鐘冷卻）。後續考慮引入代理池（Proxy Pool）或拉長排程間距。[ ] 支援更多社群平台（如 PTT、Threads 等）。[ ] 新增資料重複性檢查（使用 content_url 判斷是否已存在於資料庫）。📜 免責聲明 (Disclaimer)本專案僅供程式語言學習與技術研究之用。請遵守各平台的服務條款（Terms of Service），切勿將此工具用於惡意攻擊、高頻率壓測或商業營利用途。資料庫 Schema 設計 (Database Schema)專案採用 MySQL 資料庫（utf8mb4_unicode_ci 編碼），主要包含三個資料表，用於儲存 Google 趨勢關鍵字、相關新聞以及社群平台熱門文章。1. google_trends（Google 熱門搜尋關鍵字）紀錄 Google Trends 的熱門搜尋關鍵字與預估流量。欄位名稱型態允許空值說明idINT(11)NO (PK)自動遞增主鍵keywordVARCHAR(255)NO搜尋關鍵字（建立索引 idx_keyword）approx_trafficVARCHAR(50)YES預估搜尋量（例：50,000+）published_atDATETIMEYES趨勢發布時間（建立索引 idx_published）fetched_atDATETIMEYES資料爬取時間（預設當前時間）2. google_trends_news（Google 趨勢相關新聞）紀錄與熱門關鍵字相關的新聞報導，與 google_trends 形成 1 對多關聯。欄位名稱型態允許空值說明idINT(11)NO (PK)自動遞增主鍵trend_idINT(11)NO (FK)外鍵，關聯 google_trends.id（ON DELETE CASCADE）news_titleVARCHAR(500)YES相關新聞標題news_urlVARCHAR(1000)YES新聞連結 URLnews_sourceVARCHAR(255)YES新聞來源媒體名稱fetched_atDATETIMEYES資料爬取時間（預設當前時間）關聯說明： 當 google_trends 中的關鍵字被刪除時，對應的 google_trends_news 資料會自動級聯刪除（Cascade）。3. social_trends（社群平台熱門文章）紀錄社群平台（如 PTT 等）的熱門文章與互動數據。欄位名稱型態允許空值說明idINT(11)NO (PK)自動遞增主鍵platformVARCHAR(50)NO來源平台名稱（例：ptt）categoryVARCHAR(100)YES看板/分類名稱（例：Gossiping）titleVARCHAR(500)NO文章標題（建立索引 idx_title）content_urlVARCHAR(1000)YES文章完整連結authorVARCHAR(255)YES文章作者 IDengagement_scoreINT(11)YES互動分數/推文數（預設 0）published_atDATETIMEYES文章發布時間fetched_atDATETIMEYES資料爬取時間（建立複合索引 idx_platform_fetched）SQLCREATE TABLE `google_trends` (
	`id` INT(11) NOT NULL AUTO_INCREMENT,
	`keyword` VARCHAR(255) NOT NULL COLLATE 'utf8mb4_unicode_ci',
	`approx_traffic` VARCHAR(50) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`published_at` DATETIME NULL DEFAULT NULL,
	`fetched_at` DATETIME NULL DEFAULT current_timestamp(),
	PRIMARY KEY (`id`) USING BTREE,
	INDEX `idx_keyword` (`keyword`) USING BTREE,
	INDEX `idx_published` (`published_at`) USING BTREE
)
COLLATE='utf8mb4_unicode_ci'
ENGINE=InnoDB;

CREATE TABLE `google_trends_news` (
	`id` INT(11) NOT NULL AUTO_INCREMENT,
	`trend_id` INT(11) NOT NULL,
	`news_title` VARCHAR(500) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`news_url` VARCHAR(1000) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`news_source` VARCHAR(255) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`fetched_at` DATETIME NULL DEFAULT current_timestamp(),
	PRIMARY KEY (`id`) USING BTREE,
	INDEX `trend_id` (`trend_id`) USING BTREE,
	CONSTRAINT `fk_google_trends_news` FOREIGN KEY (`trend_id`) REFERENCES `google_trends` (`id`) ON UPDATE RESTRICT ON DELETE CASCADE
)
COLLATE='utf8mb4_unicode_ci'
ENGINE=InnoDB;

CREATE TABLE `social_trends` (
	`id` INT(11) NOT NULL AUTO_INCREMENT,
	`platform` VARCHAR(50) NOT NULL COLLATE 'utf8mb4_unicode_ci',
	`category` VARCHAR(100) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`title` VARCHAR(500) NOT NULL COLLATE 'utf8mb4_unicode_ci',
	`content_url` VARCHAR(1000) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`author` VARCHAR(255) NULL DEFAULT NULL COLLATE 'utf8mb4_unicode_ci',
	`engagement_score` INT(11) NULL DEFAULT '0',
	`published_at` DATETIME NULL DEFAULT NULL,
	`fetched_at` DATETIME NULL DEFAULT current_timestamp(),
	PRIMARY KEY (`id`) USING BTREE,
	INDEX `idx_platform_fetched` (`platform`, `fetched_at`) USING BTREE,
	INDEX `idx_title` (`title`) USING BTREE
)
COLLATE='utf8mb4_unicode_ci'
ENGINE=InnoDB;
