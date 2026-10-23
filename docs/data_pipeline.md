# 資料管線（Data Pipeline）

本文件說明每張資料表從哪裡來、由誰更新、依賴哪個外部服務，以及任一來源失敗時會影響到哪裡。排程設定見 [task_scheduler.md](task_scheduler.md)。

## 1. 資料流向

```text
外部來源                 排程工作 / 步驟                              資料表                         Dashboard
─────────               ─────────────────                            ──────                        ─────────
Google Trends 台灣 RSS ─▶ RSS Collector（每 20 分）─────────────────▶ google_trends ──────────┬──▶ 市場雷達
                                                                     google_trends_news ─────┤    事件研究
                                                                                             │
yfinance ──────────────▶ Stock Collector（交易日 18:30）────────────▶ stocks ─────────────────┤
                                                                                             │
theme_mapping.csv ─────▶ Daily ETL（每天 4 次）                                              │
entity_seed_mapping.csv    0. init_db / sync_theme_mapping ─────────▶ theme_mapping ──────────┤
                           1. Entity Resolution ◀─ Gemini（選用）─▶ entity_master          │
                                                                     keyword_classification ─┤
                           2. 關鍵字分類 ◀──────── Gemini（選用）─▶ keyword_classification ─┤
                                                                     theme_mapping ──────────┤
                           3. 未分類關鍵字自動分類 ◀ Gemini（必要）─▶ keyword_classification   │
Google Trends（pytrends）─▶ 4. 歷史熱度（近 90 天）────────────────▶ google_trends_history ───┤
                           5. 新聞情緒與事件類型 ◀─ Gemini（必要）─▶ google_trends_news ─────┘
                           6. 新聞主題彙整 ────────────────────────▶ news_theme_classification（Dashboard 未使用）
                           7. 主題每日統計 ────────────────────────▶ theme_daily_stats（僅用於側邊欄新鮮度判斷）

事件研究結果：Dashboard 開啟頁面時，由 google_trends_history + google_trends_news + stocks + theme_mapping 即時計算，
不寫入資料庫（event_analysis 表目前未被任何排程寫入）。
```

## 2. 步驟依賴矩陣

Daily ETL（`python -m trends.etl`，[etl.py](../src/trends/etl.py) 的 `run_daily_etl`）依序執行下列步驟。前一步失敗不會中止後續步驟，除非該步驟拋出未處理的例外。

| # | 步驟 | 函式 | Gemini | Google Trends | RSS 資料 | 股價 | 沒有該依賴時的行為 |
|---|---|---|---|---|---|---|---|
| 0 | 資料表初始化、主題映射同步 | `init_db`、`sync_theme_mapping` | — | — | — | — | 讀 `theme_mapping.csv` |
| 1 | Entity Resolution | `run_entity_resolution_etl` | 選用 | — | ✅ 關鍵字 | — | 沒有金鑰時只用 `entity_seed_mapping.csv` 辨識公司 |
| 2 | 關鍵字主題分類 | `classify_pending_keywords` | 選用 | — | ✅ 關鍵字 | — | 沒有金鑰時改用規則分類，標記 `fallback` |
| 3 | 未分類關鍵字自動分類 | `auto_classify_unclassified_keywords` | **必要** | — | ✅ 關鍵字 | — | 印出 `Auto keyword classification skipped` 後略過 |
| 4 | 歷史熱度 | `run_history_collection` | — | **必要** | 間接（關鍵字清單來自 2） | — | 遇到 429 時停止這一步，ETL 結束時 exit code 為 `3` |
| 5 | 新聞情緒與事件類型 | `classify_pending_news` | **必要** | — | ✅ 新聞 | — | 印出 `News AI classification skipped` 後略過，**不會填入假結果** |
| 6 | 新聞主題彙整 | `classify_pending_news_theme` | — | — | ✅ 新聞 | — | 沒有外部依賴 |
| 7 | 主題每日統計 | `refresh_theme_daily_stats` | — | 間接（讀當天的歷史熱度） | ✅ | — | 沒有外部依賴 |

其他收集器：

| 工作 | 模組 | 外部依賴 | 寫入 |
|---|---|---|---|
| RSS Collector | `trends.rss_collector --once` | Google Trends RSS（`trends.google.com/trending/rss?geo=TW`） | `google_trends`、`google_trends_news` |
| Stock Collector | `trends.stock_collector` | yfinance（Yahoo Finance） | `stocks` |

Dashboard 各頁使用的資料：

| 頁面 | 讀取的資料表 |
|---|---|
| 側邊欄狀態 | `google_trends`、`google_trends_news`、`keyword_classification`、`stocks`、`theme_mapping`、`theme_daily_stats`（僅最新日期） |
| 市場雷達 | `theme_mapping`、`google_trends_history`、`google_trends` + `google_trends_news` + `keyword_classification`、`stocks` |
| 事件研究 | 同市場雷達 |
| 專案介紹 | 不依賴資料更新 |

## 3. 資料來源與更新頻率

| 來源 | 存取方式 | 頻率 | 每次處理範圍 | 費用與限制 |
|---|---|---|---|---|
| Google Trends RSS | HTTP GET，公開 RSS | 每 20 分鐘 | 當下約 10 筆熱搜和附帶的新聞 | 免費，至今未觀察到限流 |
| yfinance | 非官方 Yahoo Finance API | 交易日 18:30 | 每檔重抓最近 14 天（新股票回補 5 年） | 免費，非官方，介面可能變動 |
| Google Trends（pytrends） | 非官方，套件已停止維護 | 每天 4 次 | 啟用中的主題關鍵字，每個抓近 90 天；已更新到昨天的跳過；最久未更新的優先，同樣久時有對應股票的優先 | 免費，**容易被 429 限流**，Google 不公開配額 |
| Gemini API | `google-genai`，金鑰放在 `.env` | 每天 4 次 | 每次最多 400 個關鍵字、1,000 篇新聞，每個請求 150 筆 | 免費方案每個模型每天約 20 個請求；主模型用完改用備用模型，全部用完才停止並留待下次 |
| `theme_mapping.csv`、`entity_seed_mapping.csv` | 本機 CSV，人工維護 | 每次 ETL 或 Dashboard 啟動時同步 | 全部 | — |

## 4. 失敗影響範圍

| 失敗來源 | 直接停止更新 | 間接影響 | 自動恢復 | 偵測方式 |
|---|---|---|---|---|
| **RSS** | `google_trends`、`google_trends_news` | 新關鍵字無法分類；新聞型事件與新聞情緒停止；側邊欄顯示「超過 24 小時未更新」 | ❌ RSS 只提供當下熱搜，**中斷期間的資料永久遺失** | `rss_collector.log` 出現 `failed>0` 或 exit 1/2；工作結果非 0 |
| **yfinance** | `stocks` | 事件後報酬、MA20/MA60、市場雷達近 10 日表現顯示「資料不足」 | ✅ 缺口在 14 天內，下次執行自動補齊 | `stock_collector.log`；側邊欄在股價超過 3 天未更新時顯示「部分資料待更新」 |
| **Google Trends（pytrends）** | `google_trends_history` | 約 90% 的事件來自「搜尋熱度上升」，這類事件停止產生；市場雷達熱度變化停止 | ✅ 缺口在 90 天內，限流解除後自動回補 | `etl.log` 出現 `rate limited; stopping run`；`Daily ETL` 工作結果為 `3` |
| **Gemini** | `keyword_classification`（AI 部分）、新聞情緒 | 新關鍵字改用 fallback 規則或歸入「其他」；情緒覆蓋率停滯，Dashboard 顯示「情緒樣本不足」 | ✅ 從中斷處繼續，已完成的資料不會重送（見下方「Gemini 限流與續跑」） | `etl.log` 的 `Gemini Stats`，以及 `[GEMINI] stopped for this run: …` |
| **Daily ETL 沒有執行** | 上述 Gemini、Google Trends 的所有項目，以及 `theme_daily_stats` | 同上 | ✅ 同上 | `Daily ETL` 的 `LastRunTime` 沒有更新 |
| **電腦關機或睡眠** | 全部 | 同上 | 工作設定了錯過後補跑；RSS 的中斷期間無法補 | 工作排程器的 `LastRunTime` |

## Gemini 限流與續跑

所有 Gemini 呼叫（Entity Resolution、關鍵字分類、自動分類、新聞情緒）都經過 [gemini_control.py](../src/trends/gemini_control.py) 的同一個限流器：

| 狀況 | 行為 |
|---|---|
| 模型順序 | 依序使用 `gemini-3.6-flash`、`gemini-3.8-flash`、`gemini-3.5-flash-lite`、`gemini-3.1-flash-lite`；免費方案每個模型每天 20 個請求，分開計算（`gemini-3.5-flash` 與 3.6-flash 共用額度，所以不列入）。可在 `.env` 用 `GEMINI_MODELS=模型A,模型B` 覆寫 |
| 一般請求 | 每個模型任意 60 秒內最多 5 次（免費方案每分鐘上限），超過就等待 |
| HTTP 429（每分鐘配額） | 依回應的 `retryDelay` 等待，沒有提供時等 60 秒，最多重試 3 次，仍失敗就改用下一個模型 |
| HTTP 429（每日配額，quotaId 含 `PerDay`） | 不等待，同一批直接改用下一個模型 |
| HTTP 5xx（例如 503 過載） | 等 10 秒重試 1 次，仍失敗就改用下一個模型。失敗的請求也會消耗每日配額，所以不在過載的模型上反覆重試 |
| HTTP 404（模型名稱不存在） | 改用下一個模型 |
| 某個模型放棄後 | 本次執行不再使用該模型 |
| 所有模型都放棄，或本次累計等待超過 30 分鐘 | 本次執行**停止所有 Gemini 呼叫**，之後的 Gemini 步驟直接跳過；歷史熱度、新聞主題彙整和主題統計照常完成 |

**續跑**：每批完成後立即寫入資料庫，所以中斷後下次會從未完成的資料繼續。有些步驟會把 Gemini 已回答過的關鍵字留在待處理清單（例如判定為「其他」的關鍵字），因此另外在資料庫旁的 `data.gemini_progress.json` 記錄各步驟已送出並收到回應的項目，同一步驟不再重送。刪除這個檔案只會讓這些項目再被送出一次，不影響資料正確性。

ETL 結束時輸出：

```text
Gemini Stats
Requests: 35
Models: gemini-3.6-flash 20, gemini-3.8-flash 15
Processed: 640 (entity 120, keyword 200, news 200, auto_quality 120)
Skipped: 4378 (keywords 611, news 3767)
429 Count: 1
503 Count: 0
Wait Time: 412 seconds
Stopped: no
```

`Models` 是各模型送出的請求數；`Processed` 是 Gemini 成功回應的項目數（依步驟細分）；`Skipped` 是執行結束時仍待處理的關鍵字與新聞數，會留到下次執行。

## 5. 如何檢查是否正常

### 每日健康報告（建議只看這一份）

工作排程器每天 07:30 執行 `run_health.bat`，產生 `logs\health_report.md`；隨時也可以手動執行：

```powershell
.venv\Scripts\python.exe -m trends.health_report
```

報告以唯讀方式讀取 `data.db` 和 `logs\`，內容包含：

| 區塊 | 內容 |
|---|---|
| 系統健康度 | RSS、Stocks、Google Trends、Gemini、ETL 五個元件各 20 分（OK 20、WARN 10、FAIL 0、UNKNOWN 10）；90 以上 🟢，60–89 🟡，未滿 60 🔴 |
| 元件狀態 | 每個元件的 OK／WARN／FAIL、最後成功時間與摘要 |
| 今日新增資料量 | 台北日當天的熱搜、新聞、股價、歷史熱度、關鍵字分類、實體筆數 |
| Gemini 今日處理量 | 當天所有 ETL 的 Requests、Processed、429、503、等待秒數合計 |
| 覆蓋率 | 分類覆蓋率、情緒覆蓋率（定義與 Dashboard 側邊欄相同）、熱度覆蓋率（近 3 天有熱度的啟用主題關鍵字） |
| 最近錯誤 | RSS 近 24 小時的錯誤與警告，以及股價、ETL 最後一次執行的錯誤訊息 |

判定規則：

| 元件 | OK | WARN | FAIL |
|---|---|---|---|
| RSS | 最新資料在 30 分鐘內，且近 24 小時沒有錯誤 | 30 分鐘到 2 小時，或有錯誤 | 超過 2 小時 |
| Stocks | 最新交易日不早於預期交易日（平日 18:30 後為當天） | 落後一個平日（國定假日也會出現） | 落後更多，或最後一次執行失敗 |
| Google Trends | 最新熱度日 ≥ 昨天，熱度覆蓋率 ≥ 80%，最後一次沒有被限流 | 介於兩者之間 | 最新熱度日早於 3 天前，或熱度覆蓋率 < 20% |
| Gemini | 最後一次 ETL 沒有停止，且情緒覆蓋率 ≥ 50% | 有處理但中途停止，或情緒覆蓋率 < 50% | 停止且一筆都沒處理 |
| ETL | 26 小時內有完成的執行（exit 0 或 3），主題統計 ≥ 昨天 | 執行中或沒有結束紀錄，或主題統計落後 | 超過 26 小時沒有執行，或 exit 為其他非 0 值 |

工作結果為 `1` 代表至少一個元件為 FAIL。

### 手動檢查

### 每日快速檢查（約 1 分鐘）

```powershell
# 1) 三個工作的最近執行結果（0 = 成功，3 = Google Trends 限流）
Get-ScheduledTask -TaskPath '\Trends\' | Get-ScheduledTaskInfo |
    Format-Table TaskName, LastRunTime, LastTaskResult, NextRunTime

# 2) 各 log 的最後幾行
Get-Content logs\rss_collector.log -Tail 3
Get-Content logs\stock_collector.log -Tail 3
Get-Content logs\etl.log -Tail 12
```

`etl.log` 中歷史熱度的統計長這樣：

```text
Completed
Success: 183
Failed: 0
Skipped: 42
Not attempted: 0
Rows written: 16470
```

被限流時第一行是 `Stopped: Google Trends rate limited (HTTP 429)`，`Not attempted` 是這次沒有查詢的關鍵字數。

### 資料新鮮度（唯讀查詢）

```powershell
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:data.db?mode=ro', uri=True); [print(n.ljust(14), *c.execute(q).fetchone()) for n, q in [('RSS_UTC', 'select max(fetched_at) from google_trends'), ('stocks', 'select max(date) from stocks'), ('history', 'select max(trend_date) from google_trends_history'), ('classify_UTC', 'select max(created_at) from keyword_classification'), ('theme_stats', 'select max(stat_date) from theme_daily_stats'), ('sentiment', 'select sum(news_sentiment is not null), count(*) from google_trends_news')]]"
```

輸出依序為：RSS 最新（UTC）、股價最新交易日、歷史熱度最新日、關鍵字分類最新（UTC）、主題統計最新日、情緒已分析篇數與全部篇數。預期值（今天為 D，台北時間）：

| 項目 | 正常 | 需要處理 |
|---|---|---|
| RSS 最新 | 30 分鐘內 | 超過 1 小時 |
| 股價最新交易日 | 最近一個交易日（D 的 18:30 後應為 D） | 落後 3 個交易日以上 |
| 歷史熱度最新日 | D−1 | 落後 3 天以上（多半是 429） |
| 關鍵字分類最新 | 最近一次 ETL 的時間 | 超過 2 天（ETL 沒跑或 Gemini 失敗） |
| 主題統計最新日 | D（02:00 後）或 D−1 | 落後 2 天以上 |

### Dashboard

側邊欄的「資料狀態」會綜合 RSS、股價、主題統計的新鮮度和待處理數量：🟢 正常、🟡 部分資料待更新、🔴 超過 24 小時未更新。它**不檢查歷史熱度**，所以 Google Trends 停止更新時仍可能顯示 🟡 而不是 🔴，要以上面的查詢為準。

## 附註

- 歷史熱度只寫入**完整的日資料**：Google 標記為 `isPartial` 的當天數據會被捨棄，所以正常時最新日期是昨天。`theme_daily_stats` 統計的是當天（D），因此它的 `keyword_count`、`avg_trend_score`、`max_trend_score` 通常是 0 或空值；Dashboard 沒有讀這三個欄位。
- `event_analysis` 只有 `trends.event_study.run_event_study()` 會寫入，目前沒有任何排程或頁面呼叫它，所以是空表。Dashboard 的事件研究是即時計算的，不依賴這張表。
