$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Activity Tracker — local setup (project root: $Root)" -ForegroundColor Cyan

if (Get-Command py -ErrorAction SilentlyContinue) {
    $PyExe = "py"
    $PyArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PyExe = "python"
    $PyArgs = @()
} else {
    Write-Host "Python 3 not found. Install from https://www.python.org/downloads/ (enable 'Add to PATH')." -ForegroundColor Red
    exit 1
}

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment..."
    & $PyExe @PyArgs -m venv .venv
}

$pip = Join-Path $Root ".venv\Scripts\pip.exe"
& $pip install --upgrade pip
Write-Host "Installing dependencies (Windows: server + collector)..."
& $pip install -r requirements-windows.txt

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
    Write-Host "Created .env from .env.example — set ACTIVITY_API_KEY." -ForegroundColor Yellow
}

$cfg = Join-Path $Root "collector\config.json"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $Root "collector\config.example.json") $cfg
    Write-Host "Created collector\config.json — match api_key to .env for the collector." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Green
Write-Host "  1. Edit .env (API key; use ACTIVITY_HOST=0.0.0.0 only for LAN/VPS)."
Write-Host "  2. Run:  .\scripts\run-server.ps1"
Write-Host "  3. Optional:  .\scripts\run-collector.ps1"
Write-Host "  4. Open: http://127.0.0.1:8000"
