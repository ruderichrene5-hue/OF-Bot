#!/usr/bin/env bash
#
# Register the ADB bot's scheduled loops as systemd timers.
# The Linux counterpart of deploy/scheduler/install_tasks.ps1.
#
# Run once on the server as root. Re-running is safe: units are rewritten in
# place and the timers re-enabled.
#
# Frequencies come from adb_bot/automation/schedule_spec.py (RECOMMENDED_INTERVALS),
# which is where they are justified. At the time of writing:
#   pipeline  every 30 min   (same-day spoofing)
#   queue     every 15 min   (fills the Posting Queue)
#   posting   every  5 min   (slots are fixed; just catch each one)
#   recheck   every 15 min   (matches RECHECK_DELAY_SECONDS)
#   retry     every 30 min   (retryable Failed -> Pending)
#   recovery  every 15 min   (un-flagged profiles -> retryable again)
#   warmup    hourly
#   warmup-state every 30 min  (publishes the warm-up day to Airtable + MLX tags)
#   issue-tags   every 15 min  (Needs Human Check -> the MLX 'Issue' tag, both ways)
#   mlx-sync  daily (23:30 local)
#   cleanup   daily (04:00 local)
#
# Loops that are in the recommended set but not yet CLI commands (queue/retry,
# until they land) are skipped with a note rather than installed -- a timer for
# a command that does not exist just fails every tick.
#
# Usage:
#   sudo ./install_units.sh                # register in DRY-RUN (safe; plans only)
#   sudo ./install_units.sh --apply        # register the loops to do real work
#   sudo ./install_units.sh --remove       # unregister every loop
#   sudo ./install_units.sh --status       # what is installed and when it next fires
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
ENV_FILE="${ENV_FILE:-/etc/adbbot/env}"
SERVICE_USER="${SERVICE_USER:-}"
# Every loop that may have a unit on disk, including ones not wired yet -- used
# by --remove, which must clean up whatever a previous run installed. The set we
# *install* is asked of Python below, so there is one definition of it.
LOOPS=(pipeline queue posting recheck retry recovery warmup warmup-state issue-tags mlx-sync cleanup doctor reap-phones second-accounts)

APPLY=0
ACTION=install
for arg in "$@"; do
    case "$arg" in
        --apply)  APPLY=1 ;;
        --remove) ACTION=remove ;;
        --status) ACTION=status ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$ACTION" == "status" ]]; then
    systemctl list-timers 'adbbot-*' --all
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "Error: needs root to write /etc/systemd/system. Re-run with sudo." >&2
    exit 1
fi

if [[ "$ACTION" == "remove" ]]; then
    for loop in "${LOOPS[@]}"; do
        systemctl disable --now "adbbot-$loop.timer" 2>/dev/null || echo "  (no timer adbbot-$loop)"
        rm -f "/etc/systemd/system/adbbot-$loop.timer" "/etc/systemd/system/adbbot-$loop.service"
        echo "Removed adbbot-$loop"
    done
    systemctl daemon-reload
    exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "Error: Python not found at $PYTHON. Set PYTHON=/path/to/venv/bin/python." >&2
    exit 1
fi

MODE="DRY-RUN"; [[ $APPLY -eq 1 ]] && MODE="APPLY"
echo "Registering ADB bot timers in $MODE mode"
echo "  RepoRoot: $REPO_ROOT"
echo "  Python:   $PYTHON"
echo "  EnvFile:  $ENV_FILE"

# The loops read their tokens from the environment. A systemd service inherits
# nothing from your shell, so without this file the timers fail with "no token"
# even though running the loop by hand works.
if [[ ! -f "$ENV_FILE" ]]; then
    echo
    echo "  WARNING: $ENV_FILE does not exist yet. Create it before enabling --apply:"
    echo "    sudo install -d -m 750 \"\$(dirname $ENV_FILE)\""
    echo "    sudo tee $ENV_FILE >/dev/null <<'EOF'"
    echo "    MULTILOGIN_TOKEN=..."
    echo "    AIRTABLE_TOKEN=..."
    echo "    AIRTABLE_BASE_ID=..."
    echo "    EOF"
    echo "    sudo chmod 600 $ENV_FILE"
    echo
fi

# Which of the recommended loops this checkout can actually run. Asked of the
# code rather than hard-coded here, so a loop added to run_loop.py is installed
# by the next run of this script with no edit.
mapfile -t INSTALL_LOOPS < <(
    PYTHONPATH="$REPO_ROOT" "$PYTHON" -c \
        'from adb_bot.automation import scheduling; print("\n".join(scheduling.installable_loops()))'
)
if [[ ${#INSTALL_LOOPS[@]} -eq 0 ]]; then
    echo "Error: could not determine the loop set (is the venv installed?)." >&2
    exit 1
fi

# Delegate the unit text to the same builders the UI and tests use, so there is
# exactly one definition of what a loop's unit looks like.
"$PYTHON" - "$REPO_ROOT" "$APPLY" "$ENV_FILE" "$SERVICE_USER" "${INSTALL_LOOPS[@]}" <<'PYEOF'
import sys
from pathlib import Path

repo_root, apply_flag, env_file, service_user = sys.argv[1:5]
loops = sys.argv[5:]
sys.path.insert(0, repo_root)

from adb_bot.automation import scheduling
from adb_bot.automation import systemd_admin as sd

unit_dir = Path("/etc/systemd/system")
for loop in loops:
    interval = scheduling.recommended_interval(loop)
    (unit_dir / sd.unit_name(loop, "service")).write_text(
        sd.build_service_unit(loop, apply=apply_flag == "1", python=f"{repo_root}/.venv/bin/python",
                              working_dir=repo_root, user=service_user or None,
                              env_file=env_file),
        encoding="utf-8")
    (unit_dir / sd.unit_name(loop, "timer")).write_text(
        sd.build_timer_unit(loop, interval), encoding="utf-8")
    print(f"  wrote adbbot-{loop}.service + .timer (every {interval} min)")
for loop in scheduling.pending_loops():
    print(f"  skipped {loop}: not a run_loop command yet -- re-run this script once it lands")
PYEOF

systemctl daemon-reload
for loop in "${INSTALL_LOOPS[@]}"; do
    systemctl enable --now "adbbot-$loop.timer"
    echo "  enabled adbbot-$loop.timer"
done

echo
echo "Done. Check with: systemctl list-timers 'adbbot-*'"
echo "Logs:             journalctl -u adbbot-posting.service -f"
if [[ $APPLY -eq 0 ]]; then
    echo "These are DRY-RUN timers (plan only). Re-run with --apply once the logs look right."
fi
