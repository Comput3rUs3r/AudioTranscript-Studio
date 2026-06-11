@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Virtual environment not found.
    echo Please run install-cuda.bat first.
    echo.
    pause
    exit /b
)

call venv\Scripts\activate.bat

set PATH=%CD%\venv\Scripts;%CD%\venv\Lib\site-packages\torch\lib;%CD%\venv\Lib\site-packages\nvidia\cudnn\bin;%PATH%

python split_audio_gui.py

echo.
echo Program closed.
pause
