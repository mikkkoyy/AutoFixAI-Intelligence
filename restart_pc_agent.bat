@echo off
echo Restarting AIRA PC Agent...
call stop_pc_agent.bat
timeout /t 2 /nobreak >nul
start "" start_pc_agent.bat
