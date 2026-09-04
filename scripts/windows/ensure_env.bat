@echo off
setlocal
for %%I in ("%~dp0\..\..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

py -3 install.py --check-venv >nul 2>&1
if not errorlevel 1 goto :ready

echo [FoxDash] repairing local Windows virtual environment...
py -3 install.py
if errorlevel 1 exit /b %errorlevel%

:ready
if not exist ".venv\Scripts\python.exe" (
  echo [FoxDash] installer completed but Windows venv interpreter is missing.
  exit /b 1
)
exit /b 0
