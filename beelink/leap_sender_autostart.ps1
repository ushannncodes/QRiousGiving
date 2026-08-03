#!/usr/bin/env pwsh
# leap_sender_autostart.ps1 — runs on the Beelink mini PC (Windows).
#
# Wraps leap_sender.py so it survives being launched unattended at boot:
# waits for the Pi to be reachable on the network, then runs leap_sender.py
# in a loop, restarting it if it ever exits (crash, Pi reboot, USB hiccup).
# Installed as a Scheduled Task by install_autostart_task.ps1 — see that
# file, and the "Autostart on the Beelink" section in LEAP_HANDOFF.md.
#
# Not meant to be run by hand except for testing: `pwsh -File .\leap_sender_autostart.ps1`
# (Ctrl+C to stop). Logs to leap_sender_autostart.log next to this script.

$ErrorActionPreference = "Continue"

$root    = "C:\Users\Creative Machine 02"
$workDir = "$root\Desktop\leapc-python-bindings-main"
$logFile = Join-Path $PSScriptRoot "leap_sender_autostart.log"

$env:LEAPSDK_INSTALL_LOCATION = "C:\Program Files (x86)\Steam\steamapps\common\Ultraleap Gemini\LeapSDK"
$env:RPI_HOST      = "192.168.1.102"  # static now — see "Network setup" in LEAP_HANDOFF.md
$env:PROJECT_AXES  = "x,z"          # update if the Leap mount changes — see LEAP_HANDOFF.md

function Log($msg) {
    "$(Get-Date -Format o)  $msg" | Out-File -FilePath $logFile -Append -Encoding utf8
}

Log "=== autostart script launched ==="

& "$root\leapenv\Scripts\Activate.ps1"
Set-Location $workDir

while ($true) {
    Log "waiting for network to $($env:RPI_HOST)..."
    while (-not (Test-Connection -ComputerName $env:RPI_HOST -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
        Start-Sleep -Seconds 5
    }

    Log "starting leap_sender.py"
    python leap_sender.py *>> $logFile
    Log "leap_sender.py exited (code $LASTEXITCODE), restarting in 5s"
    Start-Sleep -Seconds 5
}
