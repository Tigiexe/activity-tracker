$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No .venv found. Run .\scripts\setup-local.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Starting API (ACTIVITY_HOST / ACTIVITY_PORT from .env)..." -ForegroundColor Cyan
& $py serve.py
