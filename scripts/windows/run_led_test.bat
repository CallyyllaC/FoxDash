@echo off
setlocal
for %%I in ("%~dp0\..\..") do set "ROOT=%%~fI"
call "%~dp0ensure_env.bat" || exit /b %errorlevel%
cd /d "%ROOT%"
".venv\Scripts\python.exe" "tools\hardware\led_strip_test.py" %*
pause
