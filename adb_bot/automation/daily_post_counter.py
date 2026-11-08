"""A true per-calendar-day post counter, persisted across posting-loop runs.

`posting_planner._cap_per_profile` / `ADBBOT_MAX_POSTS_PER_PROFILE` only ration
one *run*: the posting timer fires every 5 minutes, so a per-run cap of 2 still
lets a 14-row backlog (a profile that was parked and just got unflagged, or a
fixed-slot model whose picked times were surplus-vs-Ready-content) drain within
an hour or two -- which is exactly the "14 reels at once" shape this module
exists to stop. This is a *daily* ceiling: once an account has hit it, further
due rows are left Pending (not discarded) and pick back up automatically once
the day rolls over in Europe/Berlin.

Explicit operator instruction (Rene, 2026-09-11): "niemals dann alle posten
bitte" / "max 9 reels pro account" -- a deliberate reversal of the 2026-08-28
decision to run uncapped (see memory: adbbot-cleared-profile-backlog.md).
That decision was Caio's; this one is Rene's. Both operators share this
server, so if this conflicts with how Caio wants the fleet to behave, it is a
one-line env change (`ADBBOT_DAILY_MAX_POSTS_PER_PROFILE=0` disables it) or
delete this file's call site in posting_runner.py -- nothing else depends on
this module.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_PATH = "/root/.adb_bot/daily_post_counts.json"
DEFAULT_TZ = "Europe/Berlin"


def today_key(now: datetime | None = None, tz_name: str = DEFAULT_TZ) -> str:
    """Today's date (YYYY-MM-DD) in `tz_name`, so the day rolls over at local
    midnight, not UTC midnight -- consistent with the posting window itself."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def target_key(launch_id: str, target_handle: str | None) -> str:
    """One counter per (phone, handle) -- matches `_cap_per_profile`'s keying,
    so a two-account phone counts each handle separately."""
    return f"{launch_id}|{(target_handle or '').strip().lower()}"


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # atomic on the same filesystem -- no half-written file


def get_count(day: str, key: str, path: str = DEFAULT_PATH) -> int:
    return int((_load(path).get(day) or {}).get(key, 0))


def increment(day: str, key: str, path: str = DEFAULT_PATH) -> int:
    """+1 and return the new count. Uses an flock so two loop processes
    (posting + retry, both able to confirm a post) can't race a read-modify-
    write into a lost increment."""
    import fcntl

    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = f"{path}.lock"
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            data = _load(path)
            # Days older than today are noise this file otherwise keeps
            # forever; prune on write instead of needing a cron job for it.
            for stale in [d for d in data if d < day]:
                del data[stale]
            day_counts = data.setdefault(day, {})
            new_count = int(day_counts.get(key, 0)) + 1
            day_counts[key] = new_count
            _save(path, data)
            return new_count
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
