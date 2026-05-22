@echo off
REM Register start_dashboard.bat to run at user logon via Windows Task Scheduler.
REM Includes SPEC §18.4 recovery: retry 3 times at 1-minute intervals when the
REM dashboard process exits with a non-zero code.
REM
REM The XML lives in mt5_dashboard_task.xml so editors syntax-highlight it
REM and the .bat only handles substitution. Run this script ONCE as the user
REM (no elevation required for ONLOGON triggers).

setlocal
set "TASK_NAME=MT5_Dashboard"
set "SCRIPT_DIR=%~dp0"
set "START_SCRIPT=%SCRIPT_DIR%start_dashboard.bat"
set "TEMPLATE=%SCRIPT_DIR%mt5_dashboard_task.xml"
set "RENDERED=%TEMP%\mt5_dashboard_task.rendered.xml"
set "USER_ID=%USERDOMAIN%\%USERNAME%"

if not exist "%TEMPLATE%" (
    echo Missing template: %TEMPLATE%
    exit /b 2
)

REM Substitute placeholders via PowerShell so we don't fight cmd's escaping.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "(Get-Content -LiteralPath '%TEMPLATE%' -Raw) -replace '__USER_ID__', '%USER_ID%' -replace '__START_SCRIPT__', '%START_SCRIPT%' | Set-Content -LiteralPath '%RENDERED%' -Encoding Unicode"
if errorlevel 1 (
    echo Failed to render task XML.
    exit /b 3
)

schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
schtasks /Create /TN "%TASK_NAME%" /XML "%RENDERED%"

if %ERRORLEVEL%==0 (
    echo Scheduled task "%TASK_NAME%" registered successfully.
    echo   Trigger:    at user logon
    echo   On failure: restart 3 times at 1-minute intervals
    echo   Command:    %START_SCRIPT%
) else (
    echo Failed to register scheduled task.
    del "%RENDERED%" >nul 2>&1
    exit /b 1
)
del "%RENDERED%" >nul 2>&1
endlocal
