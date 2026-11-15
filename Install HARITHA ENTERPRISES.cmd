@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py.exe >nul 2>&1
    if not errorlevel 1 (
        py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))"
        if errorlevel 1 goto oldpython
        py -3 -m venv .venv
    ) else (
        python -c "import sys; sys.exit(sys.version_info < (3, 12))"
        if errorlevel 1 goto oldpython
        python -m venv .venv
    )
    if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt
if errorlevel 1 goto failed

powershell.exe -NoProfile -File "Create Desktop Shortcut.ps1"
if errorlevel 1 echo Could not create the desktop shortcut. Use Start HARITHA ENTERPRISES.cmd instead.
echo.
echo Installed. Use the HARITHA ENTERPRISES desktop shortcut or Start HARITHA ENTERPRISES.cmd.
pause
exit /b 0

:failed
echo.
echo Setup failed. Install Python 3.12 or newer and try again.
pause
exit /b 1

:oldpython
echo Python 3.12 or newer is required.
pause
exit /b 1
