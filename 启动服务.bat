@echo off
setlocal
cd /d "%~dp0"

rem FrameSeek launcher.
rem app.py opens the browser itself once the server is ready, so do NOT open it here.
rem Closing this window stops the server.

set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem already running? just open the page instead of fighting for port 8771
"%PY%" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',8771))==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo FrameSeek is already running. Opening the page...
  start "" http://127.0.0.1:8771/
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)

echo Starting FrameSeek (first run warms up the OCR engine, ~10s)...
echo The browser will open automatically when the server is ready.
echo To stop the server, just close this window.
echo.
"%PY%" app.py

echo.
echo FrameSeek has stopped.
pause
