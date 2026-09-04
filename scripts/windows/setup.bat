@echo off
setlocal
for %%I in ("%~dp0\..\..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
py -3 install.py %*
