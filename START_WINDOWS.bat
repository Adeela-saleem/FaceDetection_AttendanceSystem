@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.10 or 3.11 and enable Add Python to PATH.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv 2>nul
  if errorlevel 1 py -3.10 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency installation failed. See README.md section: Windows face-engine setup.
  pause
  exit /b 1
)
python check_environment.py
if errorlevel 1 (
  echo.
  echo Face engine is not ready. Resolve the missing dependency before scanning.
  pause
  exit /b 1
)
echo.
echo Opening Face Attendance at http://localhost:8000
start "" http://localhost:8000
python -m uvicorn api:app --host 127.0.0.1 --port 8000
pause
