@echo off
REM No console window: pythonw + tray icon (notification area / ^).
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
  echo Create .venv first: scripts\setup-local.ps1 or python -m venv .venv
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" collector\collector.py --tray
exit /b 0
