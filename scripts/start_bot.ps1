# =============================================================================
# start_bot.ps1 — Launch the Polymarket bot
# Run from any PowerShell session (does NOT require Administrator)
# =============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"   # Graceful handling for missing processes

$botDir  = "C:\polymarket_bot"
$logsDir = "$botDir\logs"
$pidFile = "$logsDir\bot.pid"
$envFile = "$botDir\.env"
$script  = "polymarket_arb\main.py"

# -----------------------------------------------------------------------------
# 1. Kill any existing Python process running main.py
# -----------------------------------------------------------------------------
Write-Host "[start_bot] Stopping any existing bot process..." -ForegroundColor Yellow

$existing = Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*main.py*" }

if ($existing) {
    foreach ($proc in $existing) {
        Write-Host "  Killing PID $($proc.ProcessId) — $($proc.CommandLine)"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
} else {
    Write-Host "  No existing bot process found."
}

# Also clean up stale PID file
if (Test-Path $pidFile) {
    Remove-Item $pidFile -Force
}

# -----------------------------------------------------------------------------
# 2. Set working directory
# -----------------------------------------------------------------------------
if (-not (Test-Path $botDir)) {
    Write-Host "[start_bot] ERROR: Bot directory not found: $botDir" -ForegroundColor Red
    Write-Host "  Run scripts\server_setup.ps1 first."
    exit 1
}

Set-Location $botDir

# Ensure logs directory exists
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

# -----------------------------------------------------------------------------
# 3. Load .env — parse KEY=VALUE lines and set as environment variables
# -----------------------------------------------------------------------------
if (Test-Path $envFile) {
    Write-Host "[start_bot] Loading environment from $envFile..."
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        # Skip blank lines and comments
        if ($line -and $line -notmatch "^\s*#") {
            $idx = $line.IndexOf("=")
            if ($idx -gt 0) {
                $key   = $line.Substring(0, $idx).Trim()
                $value = $line.Substring($idx + 1).Trim()
                [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
                Write-Host "  Set: $key"
            }
        }
    }
} else {
    Write-Host "[start_bot] WARNING: .env not found at $envFile — bot may lack credentials." -ForegroundColor Yellow
}

# -----------------------------------------------------------------------------
# 4. Launch the bot in a new background window, redirect output to log
# -----------------------------------------------------------------------------
Write-Host "[start_bot] Starting bot..." -ForegroundColor Cyan

$logFile = "$logsDir\bot.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Append a start marker to the log
"" | Out-File -FilePath $logFile -Append
"=== Bot started at $timestamp ===" | Out-File -FilePath $logFile -Append

$process = Start-Process `
    -FilePath "python" `
    -ArgumentList "$script", "--ws" `
    -WorkingDirectory $botDir `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError "$logsDir\bot_err.log" `
    -WindowStyle Hidden `
    -PassThru

# -----------------------------------------------------------------------------
# 5. Write PID file
# -----------------------------------------------------------------------------
if ($process -and $process.Id) {
    $process.Id | Out-File -FilePath $pidFile -Encoding ASCII
    Write-Host "[start_bot] Bot started with PID $($process.Id)" -ForegroundColor Green
    Write-Host "  Log file : $logFile"
    Write-Host "  PID file : $pidFile"
    Write-Host "  Stop with: scripts\stop_bot.ps1"
} else {
    Write-Host "[start_bot] ERROR: Failed to start bot process." -ForegroundColor Red
    exit 1
}
