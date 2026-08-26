@echo off
title AIRA - Autonomous Intelligent Reasoning Assistant
echo ============================================
echo   AIRA - Autonomous Intelligent Reasoning Assistant
echo   Starting on http://127.0.0.1:8420
echo ============================================
echo.
python main.py
if errorlevel 1 (
    echo.
    echo AIRA encountered an error. Check logs for details.
    pause
)
