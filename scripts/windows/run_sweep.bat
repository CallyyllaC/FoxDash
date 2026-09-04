@echo off
chcp 65001 >nul
setlocal
for %%I in ("%~dp0\..\..") do set "ROOT=%%~fI"
call "%~dp0ensure_env.bat" || exit /b %errorlevel%
cd /d "%ROOT%"
".venv\Scripts\python.exe" -m foxdash_lite run --source sweep --refresh-hz 10 %*
