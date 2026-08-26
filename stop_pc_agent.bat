@echo off
echo Stopping AIRA PC Agent...
taskkill /FI "WINDOWTITLE eq AIRA PC Agent*" /F 2>nul
echo AIRA PC Agent stopped.
