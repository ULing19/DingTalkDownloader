@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build DingTalk GUI EXE 1.1.0

set "PYTHON_EXE=python"
if defined DTD_PYTHON set "PYTHON_EXE=%DTD_PYTHON%"
if defined DTD_PYTHON (
  if not exist "%PYTHON_EXE%" (
    echo [ERROR] DTD_PYTHON does not exist: %PYTHON_EXE%
    pause
    exit /b 1
  )
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found
    pause
    exit /b 1
  )
)

"%PYTHON_EXE%" -m pip install -r requirements-gui.txt "pyinstaller>=6.22,<7"
if errorlevel 1 (
  echo [ERROR] pip install failed
  pause
  exit /b 1
)

if exist build\DingTalkDownloader rmdir /s /q build\DingTalkDownloader
if exist build\portable_payload rmdir /s /q build\portable_payload
if exist dist\DingTalkDownloader rmdir /s /q dist\DingTalkDownloader
if exist dist\DingTalkDownloader.exe del /f /q dist\DingTalkDownloader.exe

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean "DingTalkDownloader.spec"

if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "tools\assemble_release.ps1" -RootDir "%CD%" -Version "1.1.0"
if errorlevel 1 (
  echo [ERROR] Release assembly failed
  pause
  exit /b 1
)
echo [OK] Release folder: dist\DingTalkDownloader_1.1.0
endlocal
