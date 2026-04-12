# Maintainer: build a zip you can attach to GitHub Releases for friends (no Python required).
# Run from repo root OR: powershell -File packaging\friends\build-windows.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$OutName = "ActivityTracker-Windows-Friends"
$Stage = Join-Path $Root "dist\$OutName"

Set-Location $Root

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$venvPip = Join-Path $Root ".venv\Scripts\pip.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Create .venv first (e.g. run scripts\setup-local.ps1)." -ForegroundColor Red
    exit 1
}

& $venvPip install -q "pyinstaller>=6.0"

$pyi = Join-Path $Root ".venv\Scripts\pyinstaller.exe"
$dist = Join-Path $Root "dist"
$work = Join-Path $Root "build\pyinstaller"
$specDir = Join-Path $PSScriptRoot ".pyi-spec"
New-Item -ItemType Directory -Force -Path $work, $specDir | Out-Null

$serverEntry = Join-Path $Root "serve.py"
$collEntry = Join-Path $Root "collector\collector.py"
& $pyi --noconfirm --clean --onefile --name "ActivityTrackerServer" `
    --distpath $dist --workpath $work --specpath $specDir $serverEntry
& $pyi --noconfirm --clean --onefile --name "ActivityTrackerCollector" `
    --distpath $dist --workpath $work --specpath $specDir $collEntry

New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item (Join-Path $Root "dist\ActivityTrackerServer.exe") (Join-Path $Stage "ActivityTrackerServer.exe") -Force
Copy-Item (Join-Path $Root "dist\ActivityTrackerCollector.exe") (Join-Path $Stage "ActivityTrackerCollector.exe") -Force
Copy-Item (Join-Path $PSScriptRoot "config.friend.json") (Join-Path $Stage "config.json") -Force
Copy-Item (Join-Path $PSScriptRoot "START-HERE.txt") (Join-Path $Stage "START-HERE.txt") -Force

$zip = Join-Path $Root "dist\$OutName.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $Stage -DestinationPath $zip -Force

Write-Host ""
Write-Host "Built: $zip" -ForegroundColor Green
Write-Host "Upload that zip to a GitHub Release for friends."
