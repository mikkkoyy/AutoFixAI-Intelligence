@echo off
title AIRA - Full Stack
echo ============================================
echo   Starting AIRA + PC Agent
echo ============================================
echo.

echo [1/2] Starting AIRA Brain on http://127.0.0.1:8420 ...
start "AIRA Brain" cmd /c "cd /d "%~dp0" && python main.py"

echo [2/2] Starting PC Agent on http://127.0.0.1:8765 ...
start "AIRA PC Agent" cmd /c "cd /d "%~dp0" && python pc_agent_main.py"

echo.
echo AIRA Stack started:
echo   AIRA:   http://127.0.0.1:8420
echo   PC Agent: http://127.0.0.1:8765
echo.
timeout /t 3 /nobreak >nul
echo Press any key to stop all services...
pause >nul

call stop.bat
call stop_pc_agent.bat
