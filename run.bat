@echo off
rem Google Trends RSS collector, one run. Schedule with Windows Task Scheduler every 10 minutes.
rem Exit codes: 0 ok, 1 fetch/store failure, 2 database not migrated (see P0 runbook).
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" -m trends.rss_collector --once >> logs\rss_collector.log 2>&1
exit /b %ERRORLEVEL%
