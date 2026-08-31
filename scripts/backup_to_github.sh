#!/usr/bin/env bash
# Daily mirror push of the repo's current state to a personal, independent GitHub
# repo (see /etc/adbbot/env: ADBBOT_BACKUP_GITHUB_*). Purely additive -- never
# touches the `origin` remote or anyone else's access to it. This is a snapshot
# copy for "I still have the version from before it was deleted", not a sync:
# each run overwrites the backup repo with the current local state.
set -eo pipefail

# Deliberately no `set -u` here: /etc/adbbot/env carries a scrypt password hash
# (ADBBOT_SITE_PASSWORD_HASH) with a literal `$digit` in it, which bash treats
# as a positional-parameter reference on source -- nounset then aborts on it.
# Every other loop in this repo sources this same file without -u for exactly
# this reason; matching that convention here rather than touching the env file.
set -a
source /etc/adbbot/env
set +a

: "${ADBBOT_BACKUP_GITHUB_USER:?ADBBOT_BACKUP_GITHUB_USER not set in /etc/adbbot/env}"
: "${ADBBOT_BACKUP_GITHUB_REPO:?ADBBOT_BACKUP_GITHUB_REPO not set in /etc/adbbot/env}"
: "${ADBBOT_BACKUP_GITHUB_TOKEN:?ADBBOT_BACKUP_GITHUB_TOKEN not set in /etc/adbbot/env}"

cd /root/adb_bot

url="https://${ADBBOT_BACKUP_GITHUB_USER}:${ADBBOT_BACKUP_GITHUB_TOKEN}@github.com/${ADBBOT_BACKUP_GITHUB_USER}/${ADBBOT_BACKUP_GITHUB_REPO}.git"

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') backup: pushing mirror to ${ADBBOT_BACKUP_GITHUB_USER}/${ADBBOT_BACKUP_GITHUB_REPO}"
git push --mirror "$url"
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') backup: done"
