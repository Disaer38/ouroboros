# =============================================================================
# setup_windows.ps1 — Deploy & configure Polymarket bot on Windows Server
#
# Usage (PowerShell, from repo root):
#   .\scripts\setup_windows.ps1
#
# Run as a normal user (not necessarily Administrator).
# Winget install steps will prompt for elevation if needed.
# =============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ──────────────────────────────────────────────────────────────────

function Write-Step([string]$msg) {
    Write-Host "`n$msg" -ForegroundColor Cyan
}

function Write-Ok([string]$msg) {
    Write-Host "  ✓ $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "  ⚠ $msg" -ForegroundColor Yellow
}

function Write-Fail([string]$msg) {
    Write-Host "`n  ✗ $msg" -ForegroundColor Red
    exit 1
}

# Refresh the current session PATH from the registry (needed after winget installs)
function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ── Resolve script / repo root ────────────────────────────────────────────────

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir   # scripts/ is one level below root

Set-Location $RepoRoot
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Polymarket Bot — Windows Deployment Setup"        -ForegroundColor Cyan
Write-Host "  Repo root : $RepoRoot"                            -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# =============================================================================
# 1. Check / install Python 3.11+
# =============================================================================
Write-Step "[1/6] Checking Python version..."

$MIN_MAJOR = 3
$MIN_MINOR = 11

$pythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $pythonCmd = $candidate; break }
}

$pythonOk = $false
if ($pythonCmd) {
    $verLine = & $pythonCmd --version 2>&1   # e.g. "Python 3.12.2"
    if ($verLine -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt $MIN_MAJOR -or ($major -eq $MIN_MAJOR -and $minor -ge $MIN_MINOR)) {
            Write-Ok "Found $verLine — requirement met (>= 3.11)."
            $pythonOk = $true
        } else {
            Write-Warn "Found $verLine — too old (need >= 3.11)."
        }
    }
}

if (-not $pythonOk) {
    Write-Warn "Python 3.11+ not found. Attempting install via winget..."

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Fail @"
winget is not available on this machine.
Please install Python 3.11+ manually:
  https://www.python.org/downloads/
Then re-run this script.
"@
    }

    winget install `
        --id Python.Python.3.11 `
        -e --silent `
        --accept-source-agreements `
        --accept-package-agreements

    Refresh-Path

    # Verify again
    $pythonCmd = "python"
    $verLine = & python --version 2>&1
    if ($verLine -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 11) {
        Write-Ok "Python installed: $verLine"
    } else {
        Write-Fail @"
winget install appeared to succeed, but Python 3.11+ is still not on PATH.
Please open a new terminal and re-run this script, or install manually:
  https://www.python.org/downloads/
"@
    }
}

# =============================================================================
# 2. Check / install Git
# =============================================================================
Write-Step "[2/6] Checking Git..."

$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    $gitVer = & git --version
    Write-Ok "Found $gitVer."
} else {
    Write-Warn "Git not found. Attempting install via winget..."

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Fail @"
winget is not available.
Install Git manually from https://git-scm.com/download/win then re-run.
"@
    }

    winget install `
        --id Git.Git `
        -e --silent `
        --accept-source-agreements `
        --accept-package-agreements

    Refresh-Path

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        Write-Ok "Git installed: $(& git --version)"
    } else {
        Write-Fail "Git install seemed to work but 'git' is still not on PATH.`nOpen a new terminal and re-run, or install manually: https://git-scm.com"
    }
}

# =============================================================================
# 3. Create / reuse virtual environment
# =============================================================================
Write-Step "[3/6] Setting up virtual environment (.venv)..."

$VenvDir = Join-Path $RepoRoot ".venv"

if (Test-Path (Join-Path $VenvDir "Scripts\python.exe")) {
    Write-Ok ".venv already exists — reusing."
} else {
    & $pythonCmd -m venv $VenvDir
    Write-Ok ".venv created at $VenvDir"
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"

# =============================================================================
# 4. Install requirements
# =============================================================================
Write-Step "[4/6] Installing Python dependencies..."

$RequirementsFile = Join-Path $RepoRoot "requirements.txt"
if (-not (Test-Path $RequirementsFile)) {
    Write-Fail "requirements.txt not found at $RequirementsFile"
}

& $VenvPip install --upgrade pip --quiet
& $VenvPip install -r $RequirementsFile
Write-Ok "All dependencies installed."

# =============================================================================
# 5. Create .env from template (Windows-safe, CRLF)
# =============================================================================
Write-Step "[5/6] Setting up .env file..."

$EnvFile     = Join-Path $RepoRoot ".env"
$EnvTemplate = Join-Path $RepoRoot ".env.example"

if (Test-Path $EnvFile) {
    Write-Ok ".env already exists — not overwriting. Edit manually if needed."
} else {
    if (Test-Path $EnvTemplate) {
        # Copy template, normalize line endings to CRLF (Windows standard)
        $templateContent = Get-Content $EnvTemplate -Raw
        $templateContent = $templateContent -replace "`r?`n", "`r`n"
        [System.IO.File]::WriteAllText($EnvFile, $templateContent, [System.Text.Encoding]::UTF8)
        Write-Ok ".env created from .env.example"
    } else {
        # Generate a sane default template
        $defaultEnv = @"
# ── Polymarket credentials ────────────────────────────────────────────────────
# Get these from https://polymarket.com (API key section)
POLYMARKET_PRIVATE_KEY=YOUR_PRIVATE_KEY_HERE

# ── Wallet ────────────────────────────────────────────────────────────────────
POLYMARKET_WALLET_ADDRESS=YOUR_WALLET_ADDRESS_HERE

# ── HTTP Proxy (optional) ─────────────────────────────────────────────────────
# Format: http://user:pass@host:port  — leave empty to disable
PROXY_URL=

# ── Bot settings ──────────────────────────────────────────────────────────────
DRY_RUN=true
LOG_LEVEL=INFO
"@
        # Normalize to CRLF
        $defaultEnv = $defaultEnv -replace "`r?`n", "`r`n"
        [System.IO.File]::WriteAllText($EnvFile, $defaultEnv, [System.Text.Encoding]::UTF8)
        Write-Ok ".env created with defaults."
    }

    Write-Warn "ACTION REQUIRED: Open .env and fill in your real credentials:`n    notepad $EnvFile"
}

# =============================================================================
# 6. Generate start_bot.bat and stop_bot.bat
# =============================================================================
Write-Step "[6/6] Generating start_bot.bat / stop_bot.bat..."

# ── start_bot.bat ─────────────────────────────────────────────────────────────
$StartBat = Join-Path $RepoRoot "start_bot.bat"

$startContent = @"
@echo off
:: start_bot.bat — Launch the Polymarket arbitrage bot
:: Double-click or run from Command Prompt.

setlocal

cd /d "%~dp0"

:: Activate venv
call .venv\Scripts\activate.bat

:: Export .env vars into the process environment
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "line=%%A"
    if not "!line:~0,1!"=="#" (
        if not "%%A"=="" set "%%A=%%B"
    )
)

:: Log directory
if not exist logs mkdir logs

:: Run the bot; tee stdout+stderr to a timestamped log
set LOG_FILE=logs\bot_%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%.log
set LOG_FILE=%LOG_FILE: =0%

echo [%date% %time%] Starting bot... >> %LOG_FILE%
python scripts\mm_bot.py >> %LOG_FILE% 2>&1

echo.
echo Bot exited. Log: %LOG_FILE%
pause
"@

# Use CRLF
$startContent = $startContent -replace "`r?`n", "`r`n"
[System.IO.File]::WriteAllText($StartBat, $startContent, [System.Text.Encoding]::ASCII)
Write-Ok "start_bot.bat written."

# ── stop_bot.bat ──────────────────────────────────────────────────────────────
$StopBat = Join-Path $RepoRoot "stop_bot.bat"

$stopContent = @"
@echo off
:: stop_bot.bat — Kill all running mm_bot.py processes

echo Stopping mm_bot.py processes...
taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *mm_bot*" 2>nul
wmic process where "name='python.exe' and commandline like '%%mm_bot%%'" delete 2>nul
echo Done.
pause
"@

$stopContent = $stopContent -replace "`r?`n", "`r`n"
[System.IO.File]::WriteAllText($StopBat, $stopContent, [System.Text.Encoding]::ASCII)
Write-Ok "stop_bot.bat written."

# ── Optional: register a Windows Scheduled Task for auto-start on boot ────────
Write-Host ""
$registerTask = Read-Host "  Register Windows Scheduled Task (auto-start on reboot)? [y/N]"
if ($registerTask -imatch "^y") {
    $taskName   = "PolymarketBot"
    $taskAction = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c `"$StartBat`"" `
        -WorkingDirectory $RepoRoot

    $taskTrigger  = New-ScheduledTaskTrigger -AtStartup
    $taskSettings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0) `   # no time limit
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Warn "Existing task '$taskName' removed."
    }

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action   $taskAction `
        -Trigger  $taskTrigger `
        -Settings $taskSettings `
        -RunLevel Highest `
        -Force | Out-Null

    Write-Ok "Scheduled Task '$taskName' registered (runs at startup, restarts on failure)."
    Write-Warn "To start now without rebooting:`n    Start-ScheduledTask -TaskName '$taskName'"
} else {
    Write-Ok "Skipped Scheduled Task registration. Run start_bot.bat manually to launch."
}

# =============================================================================
# Summary
# =============================================================================
Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Repo root  : $RepoRoot"
Write-Host "  Venv       : $VenvDir"
Write-Host "  .env       : $EnvFile"
Write-Host "  start_bot  : $StartBat"
Write-Host "  stop_bot   : $StopBat"
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Yellow
Write-Host "   1. Edit .env — fill in POLYMARKET_PRIVATE_KEY + WALLET_ADDRESS"
Write-Host "   2. Set DRY_RUN=false when ready to trade"
Write-Host "   3. Double-click start_bot.bat  (or run it from PowerShell)"
Write-Host "==================================================" -ForegroundColor Cyan
