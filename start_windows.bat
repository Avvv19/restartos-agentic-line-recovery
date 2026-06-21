@echo off
REM ===== RestartOS one-click launcher (Windows) =====
cd /d "%~dp0"
set PYTHONPATH=%~dp0
where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ from https://python.org and re-run.
  pause & exit /b
)
echo Installing minimal dependency (PyYAML)...
python -m pip install --quiet PyYAML >nul 2>&1
if not exist "_data\_manifest.json" (
  echo Generating the demo dataset ^(first run only^)...
  python dataset\generate.py
)
echo Opening http://localhost:8000 ...
start "" http://localhost:8000
echo Starting the RestartOS engine server. Keep this window open. Press Ctrl+C to stop.
python -m restartos.server --port 8000
pause
