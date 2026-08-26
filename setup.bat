@echo off
echo Installing AIRA dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installation failed. Ensure Python 3.10+ is installed.
    pause
    exit /b 1
)
echo.
echo Installation complete!
echo.
echo Next steps:
echo   1. Copy .env.example to .env
echo   2. Add your AI API key to .env
echo   3. Run start.bat to launch AIRA
echo.
pause
