#!/usr/bin/env bash
#
# Copy this project to the Linux server without git.
#
# Sends only source (~4 MB). Deliberately excluded: virtualenvs, PyInstaller
# output, __pycache__, logs, and dev_settings.json -- a Windows/macOS venv
# contains platform-specific binaries and absolute paths, so copying one to
# Linux produces an installation that looks complete and cannot run.
#
# Usage:
#   ./deploy/sync_to_server.sh user@server                     # to /opt/adb_bot
#   ./deploy/sync_to_server.sh user@server /srv/adb_bot        # custom path
#   DRY_RUN=1 ./deploy/sync_to_server.sh user@server           # show what would go
#
# Re-running is the normal way to push an update: rsync sends only what changed.
# It does NOT restart anything -- see the note printed at the end.
#
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${2:-/opt/adb_bot}"

if [[ -z "$TARGET" ]]; then
    echo "Usage: $0 user@server [remote-dir]" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v rsync >/dev/null 2>&1; then
    echo "Error: rsync not found." >&2
    echo "  Windows: use Git Bash with rsync, WSL, or fall back to scp:" >&2
    echo "    tar --exclude-vcs -czf src.tgz adb_bot tests deploy packaging requirements.txt *.sh *.md" >&2
    echo "    scp src.tgz $TARGET:/tmp/ && ssh $TARGET 'mkdir -p $REMOTE_DIR && tar xzf /tmp/src.tgz -C $REMOTE_DIR'" >&2
    exit 1
fi

RSYNC_FLAGS=(-az --delete --human-readable --info=stats1)
[[ -n "${DRY_RUN:-}" ]] && RSYNC_FLAGS+=(--dry-run --itemize-changes)

# --delete keeps the server from accumulating files you deleted locally, but the
# excludes below are also protected: rsync will not delete the server's venv,
# logs or settings just because they are absent here.
echo "Syncing $REPO_ROOT -> $TARGET:$REMOTE_DIR"
rsync "${RSYNC_FLAGS[@]}" \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='build_venv/' \
    --exclude='build/' \
    --exclude='dist/' \
    --exclude='dist_new/' \
    --exclude='__pycache__/' \
    --exclude='*.py[cod]' \
    --exclude='.pytest_cache/' \
    --exclude='logs/' \
    --exclude='dev_settings.json' \
    --exclude='.git/' \
    --exclude='old code that was working/' \
    ./ "$TARGET:$REMOTE_DIR/"

if [[ -n "${DRY_RUN:-}" ]]; then
    echo "(dry run -- nothing was transferred)"
    exit 0
fi

# Windows checkouts can leave CRLF on the shell scripts, which makes Linux fail
# with a confusing "bad interpreter: no such file or directory".
ssh "$TARGET" "cd '$REMOTE_DIR' && sed -i 's/\r$//' install_requirements.sh run_ui.sh deploy/systemd/install_units.sh deploy/sync_to_server.sh 2>/dev/null; chmod +x install_requirements.sh run_ui.sh deploy/systemd/install_units.sh"

cat <<EOF

Done. On the server:
  cd $REMOTE_DIR
  ./install_requirements.sh                                   # first time only
  .venv/bin/python -m adb_bot.automation.run_loop doctor

If the timers are already installed and you changed code, restart is not needed
(each run is a fresh oneshot process), but a changed *unit* does need:
  sudo ./deploy/systemd/install_units.sh [--apply]
EOF
