# Windows 工作排程器設定

本專案的資料收集由 Windows 工作排程器（Task Scheduler）觸發三支批次檔，每支執行一次即結束，不需要常駐終端機。所有工作放在排程器的 `\Trends\` 資料夾。

> 不要使用各模組的 `--schedule` 參數。它啟動的是 APScheduler 常駐程序，關閉終端機或重開機就會停止，且與工作排程器重複觸發。

## 工作一覽

| 工作名稱 | 批次檔 | 觸發時間（台北） | 執行時間上限 | 寫入資料表 | Log |
|---|---|---|---|---|---|
| `\Trends\RSS Collector` | `run.bat` | 每 10 分鐘 | 5 分鐘 | `google_trends`、`google_trends_news` | `logs\rss_collector.log` |
| `\Trends\Stock Collector` | `run_stocks.bat` | 週一至週五 18:30 | 60 分鐘 | `stocks` | `logs\stock_collector.log` |
| `\Trends\Health Report` | `run_health.bat` | 每天 07:30 | 10 分鐘 | 不寫入資料庫（唯讀），輸出 `logs\health_report.md` | `logs\health.log` |
| `\Trends\Daily ETL` | `run_etl.bat` | 每天 02:00、08:00、14:00、20:00 | 180 分鐘 | `entity_master`、`keyword_classification`、`theme_mapping`、`google_trends_history`、`google_trends_news`（情緒欄位）、`news_theme_classification`、`theme_daily_stats` | `logs\etl.log` |

共同設定：

| 設定 | 值 | 原因 |
|---|---|---|
| 錯過排程時間後儘快執行（`StartWhenAvailable`） | 開啟 | 個人電腦可能在排程時間關機或睡眠，開機後補跑 |
| 多重執行個體（`MultipleInstances`） | `IgnoreNew` | 前一次尚未結束時不啟動第二份，避免同時寫入 SQLite |
| 使用電池時仍執行 | 開啟 | 筆電未接電源時不跳過 |
| 執行身分 | 目前登入使用者 | `.env`、`.venv` 位於使用者可讀的專案目錄 |
| 起始位置（Start in） | 專案根目錄 | 批次檔另以 `cd /d "%~dp0"` 確保路徑正確 |

## 時間安排的理由

- **RSS 每 10 分鐘**：Google Trends 台灣 RSS 每次只回傳約 10 筆熱搜，間隔過長會漏掉短暫出現的關鍵字。收集器以唯一索引去重，重複抓取不會產生重複資料。
- **股價 18:30（交易日）**：台股 13:30 收盤，yfinance 通常在傍晚提供當日日線。每次會重抓每檔最近 14 天，錯過的日子在下次執行時自動補齊；國定假日執行只會得到無新資料的結果。
- **ETL 每天 4 次（02:00、08:00、14:00、20:00）**：Gemini 503 過載與 Google Trends 429 都和時段有關，多次執行能多次嘗試；續跑記錄和「最久未更新優先」輪替確保每次執行都在推進新的資料，不會重複。每次最多 400 個關鍵字、1,000 篇新聞，約 41 個 Gemini 請求，在每分鐘 5 次下約 8 分鐘。
- **健康報告 07:30**：在前一晚的股價與 ETL 之後、一天開始時產生，只需看這一份報告。

## 註冊方式

在專案根目錄以一般使用者身分開啟 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register_tasks.ps1
```

- 同名工作已存在時會略過；要以本文件的設定覆蓋既有工作，加上 `-Force`。
- 專案不在 `C:\proj\trends` 時不需修改腳本，路徑以腳本所在位置推算；也可用 `-ProjectDir` 指定。

手動建立（GUI）時，依「工作一覽」與「共同設定」兩表設定：動作為「啟動程式」，程式填批次檔完整路徑，「開始位置」填專案根目錄。

## 驗證

立即觸發一次並查看結果：

```powershell
Start-ScheduledTask -TaskPath '\Trends\' -TaskName 'Stock Collector'
Get-ScheduledTask -TaskPath '\Trends\' | Get-ScheduledTaskInfo |
    Format-Table TaskName, LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult` 的意義：

| 值 | 意義 |
|---|---|
| `0` | 成功 |
| `3` | 僅 `Daily ETL`：Google Trends 限流（HTTP 429），歷史熱度步驟提前停止，其餘步驟已完成 |
| `1` 等其他非 0 值 | 未預期錯誤，查看對應 log 的 Traceback |
| `2` | 僅 `RSS Collector`：資料庫尚未完成遷移 |
| `1`（Health Report） | 至少一個元件為 FAIL，打開 `logs\health_report.md` 查看 |
| `267009` | 仍在執行中 |

各工作正常時的 log：

| Log | 正常訊息 |
|---|---|
| `logs\rss_collector.log` | `RSS collected: items=10 ... failed=0` |
| `logs\stock_collector.log` | `已寫入 N 筆行情`，結尾 `stock_collector exit 0` |
| `logs\etl.log` | 歷史熱度統計 `Completed / Success / Failed / Skipped`，接著 `Theme ETL completed: {...}`，結尾 `etl exit 0` |

隔天確認資料有往前推進（唯讀查詢）：

```powershell
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:data.db?mode=ro', uri=True); print(c.execute('select (select max(fetched_at) from google_trends), (select max(date) from stocks), (select max(trend_date) from google_trends_history), (select max(created_at) from keyword_classification), (select max(stat_date) from theme_daily_stats)').fetchone())"
```

## 已知狀況與排除

| 狀況 | 現象 | 處理 |
|---|---|---|
| Google Trends 限流（pytrends 429） | `etl.log` 出現 `[ERROR] keyword=... rate limited; stopping run` 與 `Stopped: Google Trends rate limited`；工作結果為 `3` | 收集器在第一個 429 就停止，不重試也不再查下一個關鍵字，避免延長封鎖。每次抓近 90 天，已更新到昨天的關鍵字會跳過，限流解除後缺口自動回補。連續多日為 `3` 時，見 [data_pipeline.md](data_pipeline.md) 的 Google Trends 段落 |
| Gemini 每日配額用完 | `etl.log` 出現 quota exceeded，該層提前停止 | 不需處理，剩餘關鍵字與新聞留待下次執行 |
| 未設定 `GEMINI_API_KEY` | `News AI classification skipped`；關鍵字改用標記為 `fallback` 的規則分類 | 在專案根目錄 `.env` 設定 `GEMINI_API_KEY` |
| `run.bat` 回傳 2 | 資料庫尚未完成 P0 遷移（log 會寫明原因） | 對 `trends.migrations` 下尚未套用的遷移（`p0_0_fix_event_analysis`、`p0_1_dedupe_rss`、`p0_2_timezone`）先 `--report` 檢視，再 `--rehearse` 演練，最後 `--apply`，例如 `python -m trends.migrations.p0_1_dedupe_rss --report` |
| `database is locked` | 兩個工作同時寫入 SQLite 時偶發 | 下次排程會重試；持續發生時錯開 ETL 時間 |
| 工作顯示「準備就緒」但從未執行 | 電腦在排程時間關機，且未開啟「錯過排程時間後儘快執行」 | 以 `register_tasks.ps1 -Force` 重新套用設定 |

## Log 管理

批次檔以附加（`>>`）方式寫入 `logs\`，該目錄已列於 `.gitignore`。RSS log 約每月 4,000 行；需要時可手動封存，例如把 `logs\rss_collector.log` 改名為 `logs\rss_collector_202610.log`，收集器下次執行會建立新檔。

## 移除排程

```powershell
Unregister-ScheduledTask -TaskPath '\Trends\' -TaskName 'Stock Collector' -Confirm:$false
Unregister-ScheduledTask -TaskPath '\Trends\' -TaskName 'Daily ETL' -Confirm:$false
Unregister-ScheduledTask -TaskPath '\Trends\' -TaskName 'RSS Collector' -Confirm:$false
```
