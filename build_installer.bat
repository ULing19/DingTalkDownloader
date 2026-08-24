@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\build_installer.ps1" -RootDir "%CD%" -Version "1.3.5"
if errorlevel 1 (
  echo [ERROR] Installer build failed.
  pause
  exit /b 1
)
echo [OK] Installer and portable package are ready in dist\DingTalkDownloader_1.3.5
endlocal
