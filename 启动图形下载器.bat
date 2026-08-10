@echo off
setlocal
cd /d "%~dp0"
title DingTalk Batch Downloader

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python and add it to PATH.
  pause
  exit /b 1
)

python -c "import customtkinter,cv2,PIL" >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing GUI dependencies...
  python -m pip install -r requirements-gui.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed. Check Python and network.
    pause
    exit /b 1
  )
)

echo [INFO] Starting GUI...
python gui_downloader.py
if errorlevel 1 (
  echo [ERROR] GUI exited with an error.
  pause
  exit /b 1
)

endlocal
