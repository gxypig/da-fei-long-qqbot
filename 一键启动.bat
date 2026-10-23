@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" manage.py start
start "" http://127.0.0.1:6185
pause
