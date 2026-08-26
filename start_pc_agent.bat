@echo off
title AIRA PC Agent
echo ============================================
echo   AIRA PC Agent
echo   Address: http://127.0.0.1:8765
echo   Mode: SAFE
echo ============================================
echo.
python pc_agent_main.py
if errorlevel 1 (
    echo.
    echo AIRA PC Agent encountered an error. Check logs for details.
    pause
)
