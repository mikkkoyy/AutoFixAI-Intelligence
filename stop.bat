@echo off
echo Stopping AIRA...
taskkill /FI "WINDOWTITLE eq AIRA*" /F 2>nul
taskkill /FI "IMAGENAME eq python.exe" /F /FI "MODULES eq uvicorn*" 2>nul
echo AIRA stopped.
