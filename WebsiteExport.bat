@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0WebsiteExport.ps1" %*
exit /b %errorlevel%
