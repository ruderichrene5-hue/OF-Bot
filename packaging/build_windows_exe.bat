@echo off
REM Builds "ADB Bot.exe" on Windows using PyInstaller.
REM
REM Double-click this file (or run it from a command prompt). It creates a
REM local build virtual environment, installs all dependencies as prebuilt
REM wheels (no compiler needed), installs PyInstaller, and builds a single-file
REM windowed executable at dist\ADB Bot.exe.
REM
REM Requirements on the build machine: Python 3.10+ on PATH. Tesseract-OCR and
REM the Android platform-tools (adb) must be installed to actually RUN the app.

setlocal
cd /d "%~dp0\.."

set PYTHON=python
where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo Error: Python was not found on PATH. Install Python 3.10+ and retry.
    pause
    exit /b 1
)

if not exist build_venv (
    echo Creating build virtual environment in .\build_venv
    %PYTHON% -m venv build_venv
    if errorlevel 1 (
        echo Error: failed to create the virtual environment.
        pause
        exit /b 1
    )
)

call build_venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install --only-binary :all: -r packaging\requirements-windows.txt
if errorlevel 1 (
    echo Error: failed to install dependencies.
    pause
    exit /b 1
)
python -m pip install pyinstaller
if errorlevel 1 (
    echo Error: failed to install PyInstaller.
    pause
    exit /b 1
)

echo.
echo Building ADB Bot.exe ...
pyinstaller packaging\adb_bot_windows.spec --distpath dist --workpath build --noconfirm
if errorlevel 1 (
    echo Error: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo Build complete: "%CD%\dist\ADB Bot.exe"
echo You can double-click that file to run the app.
pause
