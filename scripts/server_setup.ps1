# =============================================================================
# server_setup.ps1 — One-time setup for a fresh Windows Server running the bot
# Run as Administrator in PowerShell
# =============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "=== Polymarket Bot — Server Setup ===" -ForegroundColor Cyan

# -----------------------------------------------------------------------------
# 1. Install OpenSSH Server (so GitHub Actions can SSH in for deployments)
# -----------------------------------------------------------------------------
Write-Host "`n[1/7] Installing OpenSSH Server..." -ForegroundColor Yellow

$sshFeature = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
if ($sshFeature.State -ne "Installed") {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Write-Host "  OpenSSH Server installed."
} else {
    Write-Host "  OpenSSH Server already installed, skipping."
}

Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# Allow SSH through the firewall (idempotent — skip if rule already exists)
if (-not (Get-NetFirewallRule -Name "sshd" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule `
        -Name sshd `
        -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True `
        -Direction Inbound `
        -Protocol TCP `
        -Action Allow `
        -LocalPort 22
    Write-Host "  Firewall rule created for port 22."
} else {
    Write-Host "  Firewall rule for sshd already exists, skipping."
}

Write-Host "  SSH service is running and set to start automatically." -ForegroundColor Green

# -----------------------------------------------------------------------------
# 2. Install Python 3.11 via winget (silent)
# -----------------------------------------------------------------------------
Write-Host "`n[2/7] Installing Python 3.11..." -ForegroundColor Yellow

$pythonCheck = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $pythonCheck) {
    winget install `
        --id Python.Python.3.11 `
        -e `
        --silent `
        --accept-source-agreements `
        --accept-package-agreements

    # Refresh PATH so the current session sees the new python
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Host "  Python 3.11 installed." -ForegroundColor Green
} else {
    Write-Host "  Python already found at: $($pythonCheck.Source) — skipping install."
}

# Add Python and Scripts to PATH if not already present
$pythonPaths = @(
    "$env:LOCALAPPDATA\Programs\Python\Python311",
    "$env:LOCALAPPDATA\Programs\Python\Python311\Scripts"
)
$machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
foreach ($p in $pythonPaths) {
    if ($machinePath -notlike "*$p*") {
        [System.Environment]::SetEnvironmentVariable(
            "Path", "$machinePath;$p", "Machine"
        )
        Write-Host "  Added to system PATH: $p"
    }
}

# -----------------------------------------------------------------------------
# 3. Install Git via winget (silent)
# -----------------------------------------------------------------------------
Write-Host "`n[3/7] Installing Git..." -ForegroundColor Yellow

$gitCheck = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $gitCheck) {
    winget install `
        --id Git.Git `
        -e `
        --silent `
        --accept-source-agreements `
        --accept-package-agreements

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Host "  Git installed." -ForegroundColor Green
} else {
    Write-Host "  Git already found at: $($gitCheck.Source) — skipping install."
}

# -----------------------------------------------------------------------------
# 4. Clone the repository into C:\polymarket_bot
# -----------------------------------------------------------------------------
Write-Host "`n[4/7] Cloning repository..." -ForegroundColor Yellow

$botDir = "C:\polymarket_bot"

if (-not (Test-Path "$botDir\.git")) {
    if (-not (Test-Path $botDir)) {
        New-Item -ItemType Directory -Path $botDir | Out-Null
    }
    git clone https://github.com/Disaer38/ouroboros.git $botDir
    Write-Host "  Repository cloned to $botDir." -ForegroundColor Green
} else {
    Write-Host "  Repository already exists at $botDir — pulling latest..."
    Set-Location $botDir
    git pull origin main
    Write-Host "  Repository updated." -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# 5. Install Python dependencies
# -----------------------------------------------------------------------------
Write-Host "`n[5/7] Installing Python dependencies..." -ForegroundColor Yellow

Set-Location $botDir
pip install -r requirements.txt
Write-Host "  Dependencies installed." -ForegroundColor Green

# -----------------------------------------------------------------------------
# 6. Create .env file with placeholder values
# -----------------------------------------------------------------------------
Write-Host "`n[6/7] Creating .env file..." -ForegroundColor Yellow

$envFile = "$botDir\.env"

if (-not (Test-Path $envFile)) {
    @"
POLYMARKET_API_KEY=YOUR_KEY_HERE
POLYMARKET_SECRET=YOUR_SECRET_HERE
POLYMARKET_PASSPHRASE=YOUR_PASSPHRASE_HERE
PROXY_URL=http://user86515:xqysl1@193.243.191.182:4650
"@ | Set-Content -Path $envFile -Encoding UTF8
    Write-Host "  .env created at $envFile" -ForegroundColor Green
    Write-Host "  IMPORTANT: Edit $envFile and fill in your real API credentials!" -ForegroundColor Red
} else {
    Write-Host "  .env already exists at $envFile — not overwriting."
}

# -----------------------------------------------------------------------------
# 7. Create logs directory
# -----------------------------------------------------------------------------
Write-Host "`n[7/7] Creating logs directory..." -ForegroundColor Yellow

$logsDir = "$botDir\logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
    Write-Host "  Created: $logsDir" -ForegroundColor Green
} else {
    Write-Host "  Logs directory already exists: $logsDir"
}

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host " Setup complete!" -ForegroundColor Green
Write-Host " Bot directory : $botDir"
Write-Host " Logs directory: $logsDir"
Write-Host " Next steps:"
Write-Host "   1. Edit $envFile with real API keys"
Write-Host "   2. Run scripts\start_bot.ps1 to launch the bot"
Write-Host "   3. Ensure GitHub Secrets (SSH_HOST, SSH_USER, SSH_PASSWORD) are set"
Write-Host "============================================" -ForegroundColor Cyan
