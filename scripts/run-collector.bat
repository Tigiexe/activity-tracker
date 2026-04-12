@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" collector\collector.py
if errorlevel 1 pause
