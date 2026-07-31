#!/usr/bin/env bash
# Builds "ADB Bot.app" on macOS using PyInstaller.
#
# Run this once (double-click, or `bash packaging/build_mac_app.command` in
# Terminal) on the machine that will run the bot. It assumes python3 and
# tesseract are already installed (adb must also be on PATH for the app itself
# to work, though it isn't needed for the build).
#
# After it finishes, drag "dist/ADB Bot.app" to /Applications. On first launch,
# right-click the app and choose "Open" once to bypass Gatekeeper's
# "unidentified developer" warning (this app isn't code-signed/notarized).

set -e
cd "$(dirname "$0")/.."

PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Error: Python is not installed. Install Python 3.10+ and try again."
    exit 1
fi

if ! command -v tesseract >/dev/null 2>&1; then
    echo "Warning: 'tesseract' was not found on PATH. OCR-dependent steps will"
    echo "not work until it is installed (e.g. 'brew install tesseract')."
fi

if [ ! -d "venv" ]; then
    echo "Creating virtual environment in ./venv"
    "$PYTHON" -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo "Building ADB Bot.app with PyInstaller..."
pyinstaller packaging/adb_bot.spec --distpath dist --workpath build --noconfirm

echo ""
echo "Build complete: $(pwd)/dist/ADB Bot.app"
echo "Drag it to /Applications, then right-click -> Open on first launch."
