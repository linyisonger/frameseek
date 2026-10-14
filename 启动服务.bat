@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python
rem 台词来源：同名 .srt -> 内嵌字幕 -> OCR 识别画面硬字幕（RapidOCR，纯 CPU）
rem 语音转写已停用，不再需要 CUDA 运行时
start "" http://127.0.0.1:8771/
"%PY%" app.py
pause
