@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================
echo   Code-agent stop  (ports 8765 / 5175)
echo ============================================
echo.

set COUNT=0

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo   stopping backend  PID %%a
    taskkill /F /T /PID %%a >nul 2>&1
    set /a COUNT+=1
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5175" ^| findstr "LISTENING"') do (
    echo   stopping frontend PID %%a
    taskkill /F /T /PID %%a >nul 2>&1
    set /a COUNT+=1
)

if !COUNT!==0 (
    echo   no Code-agent service is running on 8765 / 5175.
) else (
    echo.
    echo   stopped !COUNT! process^(es^).
)

rem robust delay (works even without a stdin console)
ping -n 4 127.0.0.1 >nul 2>&1
exit /b 0
