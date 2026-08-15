@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build DingTalk GUI EXE 1.2.0

set "BASE_PYTHON=python"
if defined DTD_PYTHON set "BASE_PYTHON=%DTD_PYTHON%"
if defined DTD_PYTHON (
  if not exist "%BASE_PYTHON%" (
    echo [ERROR] DTD_PYTHON does not exist: %BASE_PYTHON%
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

set "BUILD_VENV=%CD%\build\pyinstaller-venv"
if defined DTD_BUILD_VENV set "BUILD_VENV=%DTD_BUILD_VENV%"
set "PYTHON_EXE=%BUILD_VENV%\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [INFO] Creating isolated build environment...
  "%BASE_PYTHON%" -m venv "%BUILD_VENV%"
  if errorlevel 1 (
    echo [ERROR] Failed to create the isolated build environment
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

powershell -NoProfile -ExecutionPolicy Bypass -File "tools\assemble_release.ps1" -RootDir "%CD%" -Version "1.2.0"
if errorlevel 1 (
  echo [ERROR] Release assembly failed
  pause
  exit /b 1
)
echo [OK] Release folder: dist\DingTalkDownloader_1.2.0
endlocal
