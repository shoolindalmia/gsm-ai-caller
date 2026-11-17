@echo off
cd /d "%~dp0"
chcp 65001 >nul
title GSM AI Caller - keep this window open while calling
start "" /min ollama serve >nul 2>&1
timeout /t 2 /nobreak >nul
start "" http://localhost:8080
venv\Scripts\python.exe -m caller run
echo.
echo GSM AI Caller stopped. See logs\caller.log
pause
