' Runs a batch file with no console window; used by the Task Scheduler actions in register_tasks.ps1.
' Usage: wscript.exe //B //Nologo deploy\run_hidden.vbs C:\proj\trends\run.bat
' Waits for the batch file and returns its exit code, so LastTaskResult stays meaningful.
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run("cmd.exe /c """ & WScript.Arguments(0) & """", 0, True)
