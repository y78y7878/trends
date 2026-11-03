@echo off
rem Daily warehouse ETL, one run. Schedule with Windows Task Scheduler daily at 20:00.
rem Steps: entity resolution, keyword/news Gemini classification, Google Trends 90-day history (pytrends),
rem news theme classification, theme_daily_stats. Does NOT update stock prices (see run_stocks.bat).
rem Per-run limits keep Gemini usage bounded; the backlog is drained over successive runs.
rem 400 keywords + 1000 news at 50 items per request is about 41 requests, ~8 minutes at 5 per minute,
rem inside the 30-minute wait budget. Runs 4 times a day; resume state prevents duplicate work.
rem Exit codes: 0 ok, 3 Google Trends rate limited (HTTP 429; history step stopped, other steps completed),
rem other non-zero on unhandled failure (see logs\etl.log).
rem Redirects go first on echo lines: "exit %RC%>> file" would expand to "0>> file" (a stdin redirect).
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
>> logs\etl.log echo [%date:~0,10% %time:~0,8%] etl start
".venv\Scripts\python.exe" -m trends.etl --keyword-limit 400 --news-limit 1000 >> logs\etl.log 2>&1
set RC=%ERRORLEVEL%
>> logs\etl.log echo [%date:~0,10% %time:~0,8%] etl exit %RC%
exit /b %RC%
