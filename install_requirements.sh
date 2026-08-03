#!/usr/bin/env bash
# Create the project venv and install Python deps (macOS and Linux).
# System packages (adb, tesseract, ffmpeg) are NOT installed here -- see
# deploy/RUNBOOK.md, which lists the apt/brew line for your platform.
set -e
cd "$(dirname "$0")"

PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Error: Python is not installed. Install Python 3.12 and try again."
    exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0) if sys.version_info >= (3,10) else sys.exit(1)' >/dev/null 2>&1; then
    echo "Error: Python 3.10+ is required. Detected: $($PYTHON --version)"
    exit 1
fi

# .venv, to match the runbook, the systemd units and schedule_spec.python_exe().
# An existing ./venv from an older checkout is reused rather than orphaned.
VENV=".venv"
if [ ! -d "$VENV" ] && [ -d "venv" ]; then
    VENV="venv"
fi
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment in ./$VENV"
    "$PYTHON" -m venv "$VENV"
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Python deps installed into ./$VENV"
if [ "$(uname)" = "Linux" ]; then
    cat <<'EOF'
Still needed on Linux (system packages, not pip):
  sudo apt install android-tools-adb tesseract-ocr ffmpeg
Then check everything at once:
  .venv/bin/python -m adb_bot.automation.run_loop doctor
EOF
else
    echo "Run './run_ui.sh' to start the UI."
fi
