@echo off
REM Install/upgrade all Python dependencies listed in requirements.txt.

set "PYTHON_EXE=C:\Users\ohuch\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "PROJECT_ROOT=%~dp0.."

cd /d "%PROJECT_ROOT%"
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install -r requirements.txt
