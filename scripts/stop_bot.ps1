# =============================================================================
# stop_bot.ps1 — Gracefully stop the Polymarket bot
# Run from any PowerShell session
# =============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$botDir  = "C:\polymarket_bot"
$logsDir = "$botDir\logs"
$pidFile = "$logsDir\bot.pid"

$stopped = $false

# -----------------------------------------------------------------------------
# 1. Try to stop the specific process recorded in the PID file
# -----------------------------------------------------------------------------
if (Test-Path $pidFile) {
    $pidRaw = Get-Content $pidFile -Raw
    $pid    = $pidRaw.Trim()

    if ($pid -match "^\d+$") {
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue

        if ($proc) {
            Write-Host "[stop_bot] Stopping bot (PID $pid)..." -ForegroundColor Yellow
            Stop-Process -Id $pid -Force
            Start-Sleep -Seconds 2

            # Confirm it's gone
            $stillRunning = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if (-not $stillRunning) {
                Write-Host "[stop_bot] Process $pid stopped." -ForegroundColor Green
                $stopped = $true
            } else {
                Write-Host "[stop_bot] WARNING: Process $pid did not exit cleanly." -ForegroundColor Red
            }
        } else {
            Write-Host "[stop_bot] PID $pid not found — process may have already exited."
        }
    } else {
        Write-Host "[stop_bot] WARNING: PID file contains invalid value: '$pid'" -ForegroundColor Yellow
    }

    # Clean up PID file
    Remove-Item $pidFile -Force
    Write-Host "[stop_bot] PID file removed."
} else {
    Write-Host "[stop_bot] No PID file found at $pidFile."
}

# -----------------------------------------------------------------------------
# 2. Fallback: kill any stray python.exe processes running main.py
# -----------------------------------------------------------------------------
Write-Host "[stop_bot] Checking for stray python processes running main.py..."

$stray = Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*main.py*" }

if ($stray) {
    foreach ($proc in $stray) {
        Write-Host "  Killing stray PID $($proc.ProcessId) — $($proc.CommandLine)" -ForegroundColor Yellow
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }
    Start-Sleep -Seconds 1
    Write-Host "[stop_bot] Stray processes killed." -ForegroundColor Green
} else {
    Write-Host "  No stray processes found."
}

# -----------------------------------------------------------------------------
# 3. Final status
# -----------------------------------------------------------------------------
if ($stopped) {
    Write-Host "`n[stop_bot] Bot stopped successfully." -ForegroundColor Green
} else {
    Write-Host "`n[stop_bot] No running bot process was found." -ForegroundColor Cyan
}
