@echo off
rem Daily off-site backup of data.db to Google Drive (see src\trends\db_backup.py); keeps 14 days.
rem Schedule with Windows Task Scheduler daily at 03:30, after the 02:00 ETL.
rem Exit codes: 0 ok, 1 backup failed (e.g. Google Drive not running; see logs\db_backup.log).
rem Redirects go first on echo lines: "exit %RC%>> file" would expand to "0>> file" (a stdin redirect).
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
>> logs\db_backup.log echo [%date:~0,10% %time:~0,8%] db_backup start
".venv\Scripts\python.exe" -m trends.db_backup >> logs\db_backup.log 2>&1
set RC=%ERRORLEVEL%
>> logs\db_backup.log echo [%date:~0,10% %time:~0,8%] db_backup exit %RC%
exit /b %RC%
