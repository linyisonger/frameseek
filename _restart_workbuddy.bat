@echo off
REM ============================================================
REM WorkBuddy 延迟重启脚本
REM   1) 等 12 秒（确保 AI 的回复先送达用户）
REM   2) 结束所有 WorkBuddy.exe（含 8 个 Electron 子进程）
REM   3) 等 4 秒让端口/文件锁释放
REM   4) 重新拉起 WorkBuddy.exe
REM 日志写入 _out\workbuddy_restart.log
REM ============================================================

set LOG=D:\project\AIProjects\frameseek\_out\workbuddy_restart.log
set EXE=D:\Program Files\WorkBuddy\WorkBuddy.exe

echo [%date% %time%] 等待 12 秒后开始重启 >> "%LOG%"

powershell -NoProfile -Command "Start-Sleep -Seconds 12" >> "%LOG%" 2>&1

echo [%date% %time%] 结束 WorkBuddy 进程 >> "%LOG%"
taskkill /F /IM WorkBuddy.exe /T >> "%LOG%" 2>&1

powershell -NoProfile -Command "Start-Sleep -Seconds 4" >> "%LOG%" 2>&1

echo [%date% %time%] 重新拉起 %EXE% >> "%LOG%"
start "" "%EXE%" >> "%LOG%" 2>&1

echo [%date% %time%] 重启指令已发出 >> "%LOG%"
exit /b 0
