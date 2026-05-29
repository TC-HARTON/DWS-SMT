@echo off
REM ============================================================
REM  MT5 Dashboard - one-click launcher
REM  Double-click this file: it starts the dashboard server and
REM  opens it in your web browser automatically.
REM  Keep the "MT5 Dashboard" window open while you use it;
REM  close that window (or press Ctrl+C in it) to stop.
REM ============================================================
title MT5 Dashboard Launcher

set "PYEXE=C:\Users\ohuch\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "URL=http://127.0.0.1:8050"

REM This .bat lives in the project root - run from here.
cd /d "%~dp0"

REM --- If port 8050 is held by a HEALTHY dashboard, just open the browser.
REM     Otherwise (zombie / stale / half-dead process in ANY socket state),
REM     kill EVERY holder of the port and wait until it is actually free,
REM     then start fresh. Killing only LISTENING holders used to miss a
REM     process stuck mid-shutdown (socket in TIME_WAIT/CLOSE_WAIT), which
REM     then blocked the new bind and the dashboard "would not start". ---
set "HOLDER="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /C:"127.0.0.1:8050"') do set "HOLDER=%%P"
if defined HOLDER (
    REM Probe the response. powershell is always available on modern Windows.
    powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%URL%' -UseBasicParsing -TimeoutSec 3; if ($r.StatusCode -eq 200 -and $r.Content -match 'MT5 Dashboard') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 (
        echo The dashboard is already running. Opening browser...
        start "" "%URL%"
        ping -n 3 127.0.0.1 >nul
        exit /b 0
    )
    echo Port 8050 is held by a stale process. Cleaning up...
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr /C:"127.0.0.1:8050"') do (
        taskkill /F /PID %%P >nul 2>&1
    )
    REM Force-killed sockets are released by the OS almost immediately; a short
    REM fixed wait avoids the delayed-expansion / label-in-block batch pitfalls.
    ping -n 4 127.0.0.1 >nul
)

REM --- Start the server in its own window. ---
echo Starting the MT5 dashboard server...
start "MT5 Dashboard" cmd /k %PYEXE% main.py

REM --- Wait until the server is listening, then open the browser. ---
set /a tries=0
:waitloop
ping -n 3 127.0.0.1 >nul
set /a tries+=1
netstat -ano | findstr /C:"127.0.0.1:8050" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto ready
if %tries% geq 30 goto failed
goto waitloop

:ready
echo Dashboard is up. Opening browser...
start "" "%URL%"
exit /b 0

:failed
echo.
echo ERROR: the dashboard did not start within 60 seconds.
echo Look at the "MT5 Dashboard" window for the error message.
echo Common cause: the MetaTrader 5 terminal is not running.
echo.
pause
exit /b 1
