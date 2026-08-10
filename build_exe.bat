@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build DingTalk GUI EXE

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found
  pause
  exit /b 1
)

python -m pip install -r requirements-gui.txt pyinstaller
if errorlevel 1 (
  echo [ERROR] pip install failed
  pause
  exit /b 1
)

if exist build rmdir /s /q build
if exist dist\DingTalkDownloader rmdir /s /q dist\DingTalkDownloader
if exist dist\DingTalkDownloader.exe del /f /q dist\DingTalkDownloader.exe

python -m PyInstaller --noconfirm --clean ^
  --name DingTalkDownloader ^
  --windowed ^
  --onefile ^
  --icon "assets\download.ico" ^
  --add-data "assets\download.ico;assets" ^
  --collect-all customtkinter ^
  --collect-all darkdetect ^
  --hidden-import PIL ^
  --hidden-import PIL._tkinter_finder ^
  --hidden-import cv2 ^
  gui_downloader.py

if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  pause
  exit /b 1
)

set OUT=dist\DingTalkDownloader_Release
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%"

copy /y "dist\DingTalkDownloader.exe" "%OUT%\"
copy /y "GoDingtalk_v2.5.2_windows_amd64.exe" "%OUT%\"
if exist ffmpeg.exe copy /y "ffmpeg.exe" "%OUT%\"
if exist "ffmpeg_tmp\ffmpeg-9.0-essentials_build\bin\ffmpeg.exe" (
  if not exist "%OUT%\ffmpeg.exe" copy /y "ffmpeg_tmp\ffmpeg-9.0-essentials_build\bin\ffmpeg.exe" "%OUT%\"
)
if not exist "%OUT%\assets" mkdir "%OUT%\assets"
if exist "assets\download.ico" copy /y "assets\download.ico" "%OUT%\assets\" >nul

mkdir "%OUT%\video"
mkdir "%OUT%\.goDingtalkConfig"

if exist "使用说明.txt" (
  copy /y "使用说明.txt" "%OUT%\使用说明.txt" >nul
)
if exist "LICENSE" copy /y "LICENSE" "%OUT%\LICENSE" >nul
if exist "THIRD_PARTY_NOTICES.md" copy /y "THIRD_PARTY_NOTICES.md" "%OUT%\THIRD_PARTY_NOTICES.md" >nul

> "%OUT%\README.txt" (
  echo 钉钉回放批量下载器 1.0.1
  echo 本项目: https://github.com/NAXG/DingTalkDownloader
  echo 上游引擎: https://github.com/NAXG/GoDingtalk
  echo.
  echo 1. 双击 DingTalkDownloader.exe
  echo 2. 首次运行点击“重新登录”并完成钉钉授权
  echo 3. 粘贴链接，或导入 TXT / 二维码图片
  echo 4. 选择保存目录并点击“开始下载”
  echo.
  echo 绿色版必须保持以下文件在同一目录:
  echo   - DingTalkDownloader.exe
  echo   - GoDingtalk_v2.5.2_windows_amd64.exe
  echo   - ffmpeg.exe
  echo.
  echo 登录 Cookies 保存在 .goDingtalkConfig\，默认视频目录为 video\
  echo 中文完整说明: 使用说明.txt
)

echo.
echo [OK] Release folder: %OUT%
echo 可直接分享发布目录，或运行 build_installer.bat 生成单文件安装程序。

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\make_release_zip.ps1" -SourceDir "%~dp0dist\DingTalkDownloader_Release" -Destination "%~dp0dist\DingTalkDownloader_Release.zip"
if errorlevel 1 (
  echo [WARN] 压缩包生成失败，可直接使用发布目录
) else (
  echo [OK] 压缩包: dist\DingTalkDownloader_Release.zip
)
explorer "%OUT%"
endlocal
