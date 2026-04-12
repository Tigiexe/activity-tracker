# Maintainer: build a Windows portable zip (no Python) for GitHub Releases.
# Run: powershell -ExecutionPolicy Bypass -File packaging\windows-portable\build-windows.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$OutName = "ActivityTracker-Windows"
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
$serverHidden = @(
    "--hidden-import=backend.app.main",
    "--hidden-import=backend.jobs.daily_rollup",
    "--hidden-import=backend.jobs.discord_summary",
    "--hidden-import=backend.jobs.maintenance",
    "--hidden-import=backend.jobs.automation"
)
& $pyi --noconfirm --clean --onefile --name "ActivityTrackerServer" `
    --distpath $dist --workpath $work --specpath $specDir @serverHidden $serverEntry
& $pyi --noconfirm --clean --onefile --name "ActivityTrackerCollector" `
    --distpath $dist --workpath $work --specpath $specDir $collEntry

New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item (Join-Path $Root "dist\ActivityTrackerServer.exe") (Join-Path $Stage "ActivityTrackerServer.exe") -Force
Copy-Item (Join-Path $Root "dist\ActivityTrackerCollector.exe") (Join-Path $Stage "ActivityTrackerCollector.exe") -Force
Copy-Item (Join-Path $PSScriptRoot "config.portable.json") (Join-Path $Stage "config.json") -Force
Copy-Item (Join-Path $PSScriptRoot "START-HERE.txt") (Join-Path $Stage "START-HERE.txt") -Force

$zip = Join-Path $Root "dist\$OutName.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $Stage -DestinationPath $zip -Force

Write-Host ""
Write-Host "Built: $zip" -ForegroundColor Green
Write-Host "Attach to a GitHub Release if you publish binaries."
