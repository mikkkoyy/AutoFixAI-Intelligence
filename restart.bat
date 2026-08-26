@echo off
echo Restarting AIRA...
call stop.bat
timeout /t 2 /nobreak >nul
start "" run.bat
