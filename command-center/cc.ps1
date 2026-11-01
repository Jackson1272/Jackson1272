# Shortcut for Command Center commands, e.g.:
#   .\cc.ps1 sync            run all syncs now
#   .\cc.ps1 google-auth     connect Google Calendar + Gmail
#   .\cc.ps1 check           show what's configured
#   .\cc.ps1 brief           print today's brief
#   .\cc.ps1 restart         restart the background server (after editing config.toml)
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if ($args[0] -eq "restart") {
    Stop-ScheduledTask -TaskName CommandCenter -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*app serve*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-ScheduledTask -TaskName CommandCenter
    Write-Host "Restarted."
    exit 0
}
& "$Root\.venv\Scripts\python.exe" -m app @args
