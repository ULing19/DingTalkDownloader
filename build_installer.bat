@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title 打包钉钉回放下载器安装程序

echo ========================================
echo   钉钉回放下载器 - 安装包构建
echo ========================================
echo.

:: ---------- 1. 检查 / 构建发布目录 ----------
set "RELEASE=dist\DingTalkDownloader_Release"
set "NEED_BUILD=0"

if not exist "%RELEASE%\DingTalkDownloader.exe" set NEED_BUILD=1
if not exist "%RELEASE%\GoDingtalk_v2.5.2_windows_amd64.exe" set NEED_BUILD=1
if not exist "%RELEASE%\ffmpeg.exe" set NEED_BUILD=1
if not exist "dist\DingTalkDownloader_Release.zip" set NEED_BUILD=1

if "%NEED_BUILD%"=="1" (
  echo [1/4] 发布目录不完整，先执行 build_exe.bat ...
  call "%~dp0build_exe.bat"
  if errorlevel 1 (
    echo [ERROR] 构建 EXE 失败
    pause
    exit /b 1
  )
) else (
  echo [1/4] 已找到发布目录: %RELEASE%
)

:: 同步最新说明到发布目录
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\copy_release_docs.ps1" -SourceDir "." -DestinationDir "%RELEASE%"
if errorlevel 1 echo [WARN] 发布说明复制失败

:: 同步说明后重新生成绿色版压缩包
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\make_release_zip.ps1" -SourceDir "%RELEASE%" -Destination "dist\DingTalkDownloader_Release.zip"
if errorlevel 1 echo [WARN] 绿色版压缩包生成失败

:: ---------- 2. 查找 Inno Setup 编译器 ----------
echo [2/4] 查找 Inno Setup (ISCC.exe) ...
set "ISCC="

if defined INNOSETUP_PATH if exist "%INNOSETUP_PATH%" set "ISCC=%INNOSETUP_PATH%"

if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 7\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"

where iscc >nul 2>nul
if not defined ISCC if not errorlevel 1 for /f "delims=" %%I in ('where iscc') do set "ISCC=%%I"

if not defined ISCC (
  echo       未找到 Inno Setup，尝试用 winget 安装...
  where winget >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] 未找到 ISCC.exe，且系统没有 winget。
    echo         请安装 Inno Setup 6: https://jrsoftware.org/isdl.php
    echo         安装后重新运行本脚本。
    pause
    exit /b 1
  )
  winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo [ERROR] winget 安装 Inno Setup 失败
    pause
    exit /b 1
  )
  if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
  if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)

if not defined ISCC (
  echo [ERROR] 仍未找到 ISCC.exe，请手动安装 Inno Setup 后重试
  pause
  exit /b 1
)
echo       ISCC = %ISCC%

:: ---------- 3. 编译安装程序 ----------
echo [3/4] 编译安装程序 ...
if not exist "installer\setup.iss" (
  echo [ERROR] 缺少 installer\setup.iss
  pause
  exit /b 1
)

"%ISCC%" "installer\setup.iss"
if errorlevel 1 (
  echo [ERROR] Inno Setup 编译失败
  pause
  exit /b 1
)

:: ---------- 4. 完成 ----------
echo [4/4] 完成
echo.
set "SETUP=dist\钉钉回放下载器_安装程序.exe"
if exist "%SETUP%" (
  echo [OK] 安装程序已生成:
  echo      %CD%\%SETUP%
  for %%A in ("%SETUP%") do echo      大小: %%~zA 字节
) else (
  echo [WARN] 未在预期路径找到安装包，请检查 dist 目录
)
echo.
echo 分发时请提供:
echo   - dist\钉钉回放下载器_安装程序.exe
echo   （安装包内已包含 使用说明.txt）
echo.
explorer "dist"
endlocal
