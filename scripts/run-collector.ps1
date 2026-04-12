$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No .venv found. Run .\scripts\setup-local.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $Root "collector\config.json"))) {
    Write-Host "Missing collector\config.json. Copy collector\config.example.json and edit server_url + api_key." -ForegroundColor Red
    exit 1
}

Write-Host "Starting Windows collector..." -ForegroundColor Cyan
& $py (Join-Path $Root "collector\collector.py")
