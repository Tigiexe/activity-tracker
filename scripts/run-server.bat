@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" serve.py
if errorlevel 1 pause
