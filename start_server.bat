@echo off
title IEEE Citation Workflow — Web Server
color 0B
echo.
echo ============================================================
echo  IEEE Citation Workflow — Starting Web Interface
echo ============================================================
echo.

:: Check Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Check that .env exists
if not exist ".env" (
    echo ERROR: .env file not found.
    echo Run setup.bat first, then fill in your API keys.
    pause
    exit /b 1
)

:: Create folders if missing
if not exist "uploads" mkdir uploads
if not exist "outputs" mkdir outputs
if not exist "logs" mkdir logs

echo Starting server...
echo.
echo Once started, open your browser at:
echo   http://localhost:8080
echo.
echo To stop the server, close this window or press Ctrl+C.
echo.

:: Open browser after a short delay
start /min cmd /c "timeout /t 2 >nul && start http://localhost:8080"

:: Start Flask server
python server\app.py

pause
