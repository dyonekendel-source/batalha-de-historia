@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -m pip install -r requirements.txt
  py -m uvicorn server:app --host 0.0.0.0 --port 3000
) else (
  python -m pip install -r requirements.txt
  python -m uvicorn server:app --host 0.0.0.0 --port 3000
)
pause
