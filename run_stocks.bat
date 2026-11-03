@echo off
rem Daily stock prices via yfinance, one run. Schedule with Windows Task Scheduler on trading days at 18:30.
rem Incremental: refetches the last 14 days per ticker, so missed days are backfilled on the next run.
rem Exit codes: 0 ok, non-zero on unhandled failure (see logs\stock_collector.log).
rem Redirects go first on echo lines: "exit %RC%>> file" would expand to "0>> file" (a stdin redirect).
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
>> logs\stock_collector.log echo [%date:~0,10% %time:~0,8%] stock_collector start
".venv\Scripts\python.exe" -m trends.stock_collector >> logs\stock_collector.log 2>&1
set RC=%ERRORLEVEL%
>> logs\stock_collector.log echo [%date:~0,10% %time:~0,8%] stock_collector exit %RC%
exit /b %RC%
