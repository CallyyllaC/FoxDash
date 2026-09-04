@echo off
setlocal
for %%I in ("%~dp0\..\..") do set "ROOT=%%~fI"
call "%~dp0ensure_env.bat" || exit /b %errorlevel%
cd /d "%ROOT%"
".venv\Scripts\python.exe" -m foxdash_lite convert-log path\to\psa_decoded_core.csv sample_data\converted_ui_display.csv
