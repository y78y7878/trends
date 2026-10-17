# Registers the Trends collection tasks in Windows Task Scheduler under \Trends\.
# See docs/task_scheduler.md for the schedule design and verification steps.
#
# Usage (from the project root, normal user PowerShell):
#   powershell -ExecutionPolicy Bypass -File deploy\register_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\register_tasks.ps1 -Force    # overwrite existing tasks
#
# Existing tasks with the same name are skipped unless -Force is given.

param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$TaskPath = '\Trends\'

foreach ($script in 'run.bat', 'run_stocks.bat', 'run_etl.bat', 'run_health.bat') {
    if (-not (Test-Path (Join-Path $ProjectDir $script))) {
        throw "Missing $script in $ProjectDir"
    }
}
if (-not (Test-Path (Join-Path $ProjectDir '.venv\Scripts\python.exe'))) {
    throw "Missing .venv in $ProjectDir; run 'uv sync' (or create the venv and 'pip install -e .') first"
}

function New-TrendsSettings([int]$LimitMinutes) {
    # StartWhenAvailable: run as soon as possible after a missed start (PC off or asleep).
    # IgnoreNew: never start a second copy while the previous run is still going.
    New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $LimitMinutes)
}

$today = (Get-Date).Date
$tasks = @(
    @{
        Name        = 'RSS Collector'
        Script      = 'run.bat'
        Description = 'Google Trends TW RSS -> google_trends / google_trends_news, every 10 minutes'
        Trigger     = New-ScheduledTaskTrigger -Once -At $today -RepetitionInterval (New-TimeSpan -Minutes 10)
        Settings    = New-TrendsSettings 5
    },
    @{
        Name        = 'Stock Collector'
        Script      = 'run_stocks.bat'
        Description = 'yfinance daily prices -> stocks, trading days 18:30 Asia/Taipei'
        Trigger     = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '18:30'
        Settings    = New-TrendsSettings 60
    },
    @{
        Name        = 'Daily ETL'
        Script      = 'run_etl.bat'
        Description = 'Entity resolution, Gemini classification, pytrends history, theme_daily_stats, daily 02:00/08:00/14:00/20:00'
        # Four runs a day: Gemini 503 overload and Google Trends 429 blocks are time-dependent, and
        # resume state plus stalest-first rotation turn every extra run into extra coverage.
        Trigger     = @('02:00', '08:00', '14:00', '20:00' | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
        Settings    = New-TrendsSettings 180
    },
    @{
        Name        = 'Health Report'
        Script      = 'run_health.bat'
        Description = 'Read-only daily health report -> logs\health_report.md, daily 07:30'
        Trigger     = New-ScheduledTaskTrigger -Daily -At '07:30'
        Settings    = New-TrendsSettings 10
    }
)

foreach ($task in $tasks) {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $task.Name -ErrorAction SilentlyContinue
    if ($existing -and -not $Force) {
        Write-Host "skip     $TaskPath$($task.Name) (already registered; use -Force to overwrite)"
        continue
    }
    # Launch through wscript + run_hidden.vbs so no console window pops up on every run.
    $launcher = Join-Path $PSScriptRoot 'run_hidden.vbs'
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' `
        -Argument "//B //Nologo `"$launcher`" `"$(Join-Path $ProjectDir $task.Script)`"" -WorkingDirectory $ProjectDir
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $task.Name -Description $task.Description `
        -Action $action -Trigger $task.Trigger -Settings $task.Settings -Force | Out-Null
    Write-Host "register $TaskPath$($task.Name) -> $($task.Script)"
}

Get-ScheduledTask -TaskPath $TaskPath | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    [pscustomobject]@{ Task = $_.TaskName; State = $_.State; LastRun = $info.LastRunTime; LastResult = $info.LastTaskResult; NextRun = $info.NextRunTime }
} | Format-Table -AutoSize
