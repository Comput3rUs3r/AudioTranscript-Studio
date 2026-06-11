@echo off
cd /d "%~dp0"

echo AudioTranscript Studio CUDA Installer
echo ------------------------------------
echo.

py -3.11 --version >nul 2>nul
if %errorlevel%==0 (
    py -3.11 setup-cuda.py
) else (
    python setup-cuda.py
)

echo.
echo Installer finished.
echo If there were no fatal errors, you can now run run-gui.bat.
echo.
pause
