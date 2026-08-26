@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Create venv first: python -m venv .venv
  exit /b 1
)
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
endlocal
