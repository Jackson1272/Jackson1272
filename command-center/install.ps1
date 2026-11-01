# Command Center — Windows installer.
# Run from this folder in PowerShell:   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Re-running is safe (it updates dependencies and re-registers the startup task).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "== Command Center installer ==" -ForegroundColor Magenta

# 1. Python venv
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv } else { & python -m venv .venv }
    if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Could not create venv - is Python 3.11+ installed?" }
}
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$venvPyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -r requirements.txt

# 2. Config
if (-not (Test-Path "config.toml")) {
    Copy-Item config.example.toml config.toml
    $code = -join ((48..57) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
    (Get-Content config.toml) -replace 'passcode = "change-me"', "passcode = `"$code`"" | Set-Content config.toml
    $user = $env:USERPROFILE -replace '\\', '/'
    (Get-Content config.toml) -replace 'C:/Users/Jackson', $user | Set-Content config.toml
    Write-Host "Created config.toml — your dashboard passcode is: $code" -ForegroundColor Yellow
}

# 3. Start at logon (hidden window), restart if it crashes
$taskName = "CommandCenter"
$action = New-ScheduledTaskAction -Execute $venvPyw -Argument "-m app serve" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -StartWhenAvailable
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Command Center dashboard (http://localhost:8765)" | Out-Null
Write-Host "Registered startup task '$taskName'."

# 4. Firewall: allow the port only from Tailscale addresses (needs admin; skipped otherwise)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Remove-NetFirewallRule -DisplayName "Command Center (Tailscale)" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "Command Center (Tailscale)" -Direction Inbound -Protocol TCP `
        -LocalPort 8765 -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
    Write-Host "Firewall: port 8765 open to Tailscale devices only."
} else {
    Write-Host "Tip: re-run as Administrator to add a Tailscale-only firewall rule for port 8765." -ForegroundColor DarkYellow
}

Start-ScheduledTask -TaskName $taskName
Start-Sleep 3
& $venvPy -m app check
$ts = (& tailscale ip -4 2>$null)
Write-Host ""
Write-Host "Done. Open http://localhost:8765 here" -ForegroundColor Green
if ($ts) { Write-Host "On your phone (Tailscale): http://$($ts.Trim()):8765" -ForegroundColor Green }
Write-Host "Next: edit config.toml (Canvas token, note folders), then run  .\cc.ps1 google-auth"
