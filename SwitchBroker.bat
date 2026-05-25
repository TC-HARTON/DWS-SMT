@echo off
REM ============================================================
REM  SwitchBroker.bat - Flip MT5_TERMINAL_PATH in .env between
REM  Exness and IC Markets. Other .env values are preserved.
REM  Run this, then restart Dashboard.bat.
REM ============================================================
title MT5 Broker Switcher

set "EXNESS_PATH=C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
set "IC_PATH=C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe"

cd /d "%~dp0"

if not exist ".env" (
    echo .env not found at %CD%
    echo Copy .env.example to .env first.
    pause
    exit /b 1
)

echo ============================================================
echo  Current MT5_TERMINAL_PATH in .env:
echo ------------------------------------------------------------
findstr /B "MT5_TERMINAL_PATH=" .env
echo ============================================================
echo.
echo  1) Exness      %EXNESS_PATH%
echo  2) IC Markets  %IC_PATH%
echo.
choice /c 12q /n /m "Pick a broker (1=Exness, 2=IC Markets, q=quit): "
if errorlevel 3 (
    echo Cancelled.
    exit /b 0
)
if errorlevel 2 (
    set "NEW_PATH=%IC_PATH%"
    set "LABEL=IC Markets"
    goto :do_switch
)
set "NEW_PATH=%EXNESS_PATH%"
set "LABEL=Exness"

:do_switch
REM Hand the new path to PowerShell through an env var so we don't have to
REM escape spaces / backslashes through the .bat to PowerShell quoting chain.
REM Get-Content returns a scalar for a single-line file, so wrap with @() to
REM force an array and use Where-Object for existence detection.
set "NEW_PATH_FOR_PS=%NEW_PATH%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$line = 'MT5_TERMINAL_PATH=' + $env:NEW_PATH_FOR_PS; $c = @(Get-Content .env -Encoding utf8); $has = ($c | Where-Object { $_ -match '^MT5_TERMINAL_PATH=' }).Count -gt 0; if ($has) { $c = $c | ForEach-Object { if ($_ -match '^MT5_TERMINAL_PATH=') { $line } else { $_ } } } else { $c = $c + $line }; $c | Set-Content -Path .env -Encoding utf8"
if errorlevel 1 (
    echo .env update FAILED.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Switched to %LABEL%.
echo ------------------------------------------------------------
findstr /B "MT5_TERMINAL_PATH=" .env
echo ============================================================
echo.
echo Next step:
echo   1. Close the existing "MT5 Dashboard" cmd window (if running).
echo   2. Double-click Dashboard.bat to relaunch against %LABEL%.
echo.
pause
exit /b 0
