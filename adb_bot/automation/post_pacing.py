"""Minimum wall-clock gap between consecutive posts, per account.

Operator instruction (Rene, 2026-10-01): when the Nikki queue is turned loose on
a backlog, a given account's posts should go out at least
`ADBBOT_POST_MIN_GAP_MINUTES` (default 90) apart -- not every due row for that
account firing back to back just because the posting loop reruns every few
minutes.

This throttles the *plan*, the same way `daily_post_counter` does: a run that
finds an account's gap not yet elapsed keeps that account's due row untouched
(Pending), and a later tick picks it up once the gap has passed. It does not
touch the loop's own schedule -- the loop can keep running every 5 minutes, it
will just find that one account not allowed through until its gap elapses.

Keyed per account (`daily_post_counter.target_key(launch_id, target_handle)`,
i.e. by phone+handle) rather than globally -- changed 2026-10-03 (Rene's
original cut was one shared clock for the whole fleet, which meant one post
anywhere blocked every other account for the next 90 minutes too: ~16
posts/day total regardless of fleet size. Different accounts pacing
independently was the actual ask; only two posts on the *same* account need
the gap between them.
"""

from __future__ import annotations

import json
import os
import time

DEFAULT_PATH = "/root/.adb_bot/post_pacing.json"
DEFAULT_GAP_MINUTES = 90.0


def min_gap_minutes() -> float:
    try:
        return float(os.environ.get("ADBBOT_POST_MIN_GAP_MINUTES", DEFAULT_GAP_MINUTES))
    except ValueError:
        return DEFAULT_GAP_MINUTES


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def last_post_at(key: str, path: str = DEFAULT_PATH) -> float | None:
    data = _load(path)
    # Back-compat: a pre-2026-10-03 file has a single top-level "last_post_at"
    # (the old global clock) instead of a per-key map -- ignore it rather than
    # let it block every account forever on the first run after the upgrade.
    per_key = data.get("accounts")
    if not isinstance(per_key, dict):
        return None
    return per_key.get(key)


def record_post(key: str, now: float | None = None, path: str = DEFAULT_PATH) -> None:
    """Stamp the moment `key` (an account) just posted. Called once per confirmed post."""
    now = now if now is not None else time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = _load(path)
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    accounts[key] = now
    data["accounts"] = accounts
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def gap_elapsed(key: str, now: float | None = None, gap_minutes: float | None = None,
                 path: str = DEFAULT_PATH) -> bool:
    """True once `gap_minutes` (default: env/90) have passed since `key` (an
    account) last posted, or it never has."""
    now = now if now is not None else time.time()
    gap = gap_minutes if gap_minutes is not None else min_gap_minutes()
    last = last_post_at(key, path)
    if last is None:
        return True
    return (now - last) >= gap * 60.0
