#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
exec osascript -e 'tell app "Terminal" to do script "cd \"'"$SCRIPT_DIR"'\" && ./install_requirements.command"'
