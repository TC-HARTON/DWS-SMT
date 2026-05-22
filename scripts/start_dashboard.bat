@echo off
REM SPEC 18.2 - start dashboard 30s after MT5 boots
REM Adjust PYTHON_EXE if you reinstall Python somewhere else.

set "PYTHON_EXE=C:\Users\ohuch\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "PROJECT_ROOT=%~dp0.."

timeout /t 30 /nobreak >nul
cd /d "%PROJECT_ROOT%"
"%PYTHON_EXE%" main.py
