#!/usr/bin/env bash
#
# Install the MultiLogin agent's systemd unit (adbbot-mlx-agent.service).
#
# Why this is not part of install_units.sh
# ----------------------------------------
# install_units.sh installs *loops*: it iterates a LOOPS array, asks Python for
# the installable set, and writes a Type=oneshot service + a timer per loop from
# the shared builders in adb_bot/automation/systemd_admin.py. Its --apply flag
# means "run the loops for real instead of planning", and it finishes by
# enabling every timer.
#
# The MLX agent is none of that. It is one always-on Type=simple daemon, it is
# not a run_loop command, it has no interval, no dry-run mode and no timer.
# Bolting it in would mean special-casing it out of every loop over LOOPS, out
# of the Python builders, and out of --apply -- and it would tie "start the
# agent" to a flag whose documented meaning is "start posting for real".
# So it lives here, in its own small installer, and the unit itself is a static
# checked-in file rather than generated: its contents describe the machine's MLX
# install, not anything in this repo.
#
# Usage:
#   sudo ./install_mlx_agent.sh            # write the unit + daemon-reload. Does
#                                          # NOT enable or start it.
#   sudo ./install_mlx_agent.sh --enable   # ...and enable + start it now
#   sudo ./install_mlx_agent.sh --remove   # disable, stop, delete the unit
#   sudo ./install_mlx_agent.sh --status   # is it up, and who owns :45001
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=adbbot-mlx-agent.service
SRC="$HERE/$UNIT"
DEST_DIR="${DEST_DIR:-/etc/systemd/system}"
DEST="$DEST_DIR/$UNIT"
AGENT_BIN=/opt/mlx/agent.bin

ACTION=install
for arg in "$@"; do
    case "$arg" in
        --enable) ACTION=enable ;;
        --remove) ACTION=remove ;;
        --status) ACTION=status ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$ACTION" == "status" ]]; then
    systemctl status "$UNIT" --no-pager || true
    echo
    echo "Listener on :45001 (the agent's launcher child):"
    ss -lntp 2>/dev/null | grep 45001 || echo "  NOTHING IS LISTENING -- every phone launch will fail."
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "Error: needs root to write $DEST_DIR. Re-run with sudo." >&2
    exit 1
fi

if [[ "$ACTION" == "remove" ]]; then
    systemctl disable --now "$UNIT" 2>/dev/null || echo "  (unit was not enabled)"
    rm -f "$DEST"
    systemctl daemon-reload
    echo "Removed $UNIT"
    echo "NOTE: the agent is now unsupervised. Nothing will restart it."
    exit 0
fi

if [[ ! -x "$AGENT_BIN" ]]; then
    echo "Error: $AGENT_BIN is missing or not executable." >&2
    echo "       The MultiLogin X Linux client is not installed where the unit expects it." >&2
    exit 1
fi

install -m 644 "$SRC" "$DEST"
echo "Wrote $DEST"

if ! systemd-analyze verify "$DEST"; then
    echo "Warning: systemd-analyze reported problems with the unit (see above)." >&2
fi

systemctl daemon-reload
echo "Reloaded systemd"

# A hand-started agent is invisible to systemd: `systemctl start` would launch a
# second one, which then fails to bind :45001 and crash-loops. Detect the loose
# process and make the human retire it deliberately.
STRAY="$(pgrep -f '^/opt/mlx/agent\.bin$' || true)"
STRAY_LOOSE=""
for pid in $STRAY; do
    # Anything already inside the unit's cgroup is ours, not a stray.
    if ! grep -qs "$UNIT" "/proc/$pid/cgroup"; then
        STRAY_LOOSE="$STRAY_LOOSE $pid"
    fi
done

if [[ -n "${STRAY_LOOSE// /}" ]]; then
    echo
    echo "  A hand-started agent is running outside systemd (pid:${STRAY_LOOSE})."
    echo "  Stop it before starting the unit, or the new one cannot bind :45001:"
    echo
    echo "    sudo kill${STRAY_LOOSE}"
    echo "    sleep 5 && ss -lntp | grep 45001    # expect NO output"
    echo
    if [[ "$ACTION" == "enable" ]]; then
        echo "Refusing to --enable while the loose agent is up. Stop it, then re-run." >&2
        exit 1
    fi
fi

if [[ "$ACTION" == "enable" ]]; then
    systemctl enable --now "$UNIT"
    echo "  enabled + started $UNIT"
    echo
    sleep 5
    ss -lntp 2>/dev/null | grep 45001 || echo "  :45001 not up yet -- check journalctl -u $UNIT -f"
    exit 0
fi

cat <<EOF

Installed but NOT started. To take over from the hand-started agent:

    sudo kill \$(pgrep -f '^/opt/mlx/agent\.bin\$')   # stop the loose one
    sleep 5 && ss -lntp | grep 45001                  # expect NO output
    sudo systemctl enable --now $UNIT                 # supervised from here on
    sleep 10 && ss -lntp | grep 45001                 # expect a LISTEN line

Logs:   journalctl -u $UNIT -f
        /root/mlx/logs/agent_\$(date +%Y%m%d).log
EOF
