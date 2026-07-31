#!/usr/bin/env bash
# Start the desktop UI (macOS, or Linux with a display -- X11 forwarding/VNC).
set -e
cd "$(dirname "$0")"

# .venv is what the runbook, the systemd units and schedule_spec.python_exe()
# all use; ./venv is accepted for older checkouts.
VENV=""
for candidate in .venv venv; do
    if [ -f "$candidate/bin/activate" ]; then
        VENV="$candidate"
        break
    fi
done

if [ -z "$VENV" ]; then
    echo "Error: no virtual environment found (.venv or venv)."
    echo "Run './install_requirements.sh' first."
    exit 1
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"

if [ "$(uname)" = "Linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "Error: no display detected (DISPLAY and WAYLAND_DISPLAY are unset)."
    echo "The UI needs a desktop session. Over SSH use 'ssh -X', or attach to a VNC session."
    echo "Everything the loops need can also be run headlessly, e.g.:"
    echo "  python -m adb_bot.automation.run_loop doctor"
    exit 1
fi

echo "Starting UI..."
python -m adb_bot.ui
