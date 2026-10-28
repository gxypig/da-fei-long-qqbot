@echo off
chcp 65001 >nul
setlocal
if not exist "%~dp0scripts\open-personality-prompts.ps1" (
  echo 找不到项目脚本。请在项目目录运行，桌面请使用快捷方式，不要单独复制BAT文件。
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open-personality-prompts.ps1"
if errorlevel 1 (
  echo 打开过程中有错误，请查看上面的具体原因。
  pause
  exit /b 1
)
exit /b 0
