@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build DingTalk Replay Link Collector

set "PYTHON_EXE=%CD%\build\pyinstaller-venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Build environment not found: %PYTHON_EXE%
  exit /b 1
)

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean "DingTalkReplayLinkCollector.spec"
if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  exit /b 1
)

echo [OK] dist\DingTalkReplayLinkCollector.exe
endlocal
