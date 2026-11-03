@echo off
rem Daily project health report (read-only): writes logs\health_report.md and appends to logs\health.log.
rem Schedule with Windows Task Scheduler daily at 07:30, after the previous evening's stock and ETL runs.
rem Exit codes: 0 no component failed, 1 at least one component is FAIL (open logs\health_report.md).
rem Redirects go first on echo lines: "exit %RC%>> file" would expand to "0>> file" (a stdin redirect).
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
>> logs\health.log echo [%date:~0,10% %time:~0,8%] health start
".venv\Scripts\python.exe" -m trends.health_report >> logs\health.log 2>&1
set RC=%ERRORLEVEL%
>> logs\health.log echo [%date:~0,10% %time:~0,8%] health exit %RC%
exit /b %RC%
