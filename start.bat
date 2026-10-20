@echo off
REM One-click start: API + web UI on a single port.
REM
REM Everything is served from the FastAPI backend, so there is no second web
REM server, no CORS, and no "which port was it?" confusion.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   Could not find .venv\Scripts\python.exe
    echo   Create the virtual environment first, then re-run this file.
    echo.
    pause
    exit /b 1
)

REM Open the browser a few seconds after the server starts, in a helper window,
REM so the server can keep running in the foreground.
start "" cmd /c "timeout /t 3 >nul & start "" http://localhost:8001/app/"

echo.
echo   Sports Analyzer
echo   API + UI:  http://localhost:8001/app/
echo   API docs:  http://localhost:8001/docs
echo.
echo   Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8001 --app-dir backend
pause
