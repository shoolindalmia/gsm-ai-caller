@echo off
cd /d "%~dp0"
chcp 65001 >nul
set /p PHONE=Mobile number to call (10 digits): 
set /p NAME=Name of the person: 
start "" /min ollama serve >nul 2>&1
venv\Scripts\python.exe -m caller test-call %PHONE% --name "%NAME%" --script-file scripts\sample_loan_script.md
pause
