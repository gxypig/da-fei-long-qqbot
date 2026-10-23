@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" manage.py stop
pause
