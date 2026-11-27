@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PROJECT_DIR=%~dp0..\"
set "BOOTSTRAP=%PROJECT_DIR%src\chemotion_sample_manager\bootstrap.py"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist "%BOOTSTRAP%" (
    echo Chemotion Sample Manager source was not found. Keep this launcher in the project folder.
    goto :launch_failed
)

rem Keep the console available during automatic first-time setup. The bootstrap
rem starts the installed environment's pythonw.exe once setup has completed.
py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    py -3.14 "%BOOTSTRAP%" %*
    if errorlevel 1 goto :launch_failed
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%BOOTSTRAP%" %*
    if errorlevel 1 goto :launch_failed
    exit /b 0
)

echo Python 3.14 with Tk support was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH" and keep "tcl/tk and IDLE" selected.

:launch_failed
echo.
echo Chemotion Sample Manager could not start. Review the message above.
if exist "%PROJECT_DIR%logs\launcher.log" echo Startup log: "%PROJECT_DIR%logs\launcher.log"
if not "%~1"=="--setup-only" pause
exit /b 1
