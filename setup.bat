@echo off
title IEEE Citation Workflow — Setup
color 0A
echo.
echo ============================================================
echo  IEEE Citation Workflow — Automated Installer
echo ============================================================
echo.
echo This will install all required Python packages.
echo.

:: Check Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not on your PATH.
    echo.
    echo Please install Python 3.11+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo Python found:
python --version
echo.

:: Create required folders
echo Creating folders...
if not exist "uploads" mkdir uploads
if not exist "outputs" mkdir outputs
if not exist "logs" mkdir logs
echo Folders created.
echo.

:: Copy .env.example to .env if not already done
if not exist ".env" (
    echo Creating .env from template...
    copy ".env.example" ".env" >nul
    echo .env created. IMPORTANT: Open .env in Notepad and fill in your
    echo Zotero API key and User ID before running the workflow.
    echo.
)

:: Install Python dependencies
echo Installing Python packages (this may take a few minutes)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Package installation failed.
    echo Try running this as Administrator, or check your internet connection.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Setup complete!
echo ============================================================
echo.
echo Next steps:
echo  1. Open .env in Notepad and add your Zotero API key and User ID
echo  2. Import n8n_workflow.json into n8n
echo  3. Double-click start_server.bat to launch the web interface
echo  4. Open http://localhost:8080 in your browser
echo.
pause
