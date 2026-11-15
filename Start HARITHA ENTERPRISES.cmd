@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "desktop_launcher.py"
    exit /b 0
)

echo Run Install HARITHA ENTERPRISES.cmd first.
pause
exit /b 1
