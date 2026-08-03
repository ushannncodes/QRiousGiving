#!/usr/bin/env pwsh
# install_autostart_task.ps1 — run ONCE on the Beelink, from an
# Administrator PowerShell prompt, to make leap_sender_autostart.ps1
# start automatically every time the Beelink boots — no login, no
# clicking. Registers a Scheduled Task that fires "At startup" (system
# boot, before any user signs in) running as SYSTEM.
#
# Usage (as Administrator):
#   cd "C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main"
#   .\install_autostart_task.ps1
#
# To remove it later:
#   schtasks /Delete /TN "QRiousGiving LeapSender" /F

$taskName  = "QRiousGiving LeapSender"
$scriptDir = "C:\Users\Creative Machine 02\Desktop\leapc-python-bindings-main"
$scriptPath = Join-Path $scriptDir "leap_sender_autostart.ps1"

if (-not (Test-Path $scriptPath)) {
    Write-Error "Can't find $scriptPath — copy leap_sender_autostart.ps1 there first."
    exit 1
}

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force

Write-Host "Installed scheduled task '$taskName' — it will run at every boot, no login required."
Write-Host "Test now without rebooting: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Then check the log: Get-Content '$scriptDir\leap_sender_autostart.log' -Tail 20 -Wait"
