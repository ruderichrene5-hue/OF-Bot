"""Minimum wall-clock gap between consecutive posts across the whole Posting Queue.

Operator instruction (Rene, 2026-10-01): when the Nikki queue is turned loose on
a backlog, posts should go out one at a time with at least
`ADBBOT_POST_MIN_GAP_MINUTES` (default 90) between them -- not every due row
firing back to back just because the posting loop reruns every few minutes.

This throttles the *plan*, the same way `daily_post_counter` does: a run that
finds the gap not yet elapsed keeps every due row untouched (Pending), and the
next tick picks exactly one more up once the gap has passed. It does not touch
the loop's own schedule -- the loop can keep running every 5 minutes, it will
just find nothing allowed through until the gap elapses.

The gap is global (across every account currently allowed to post, not
per-account): right now that is effectively "Nikki, plus the Lea Main
exception", and a single shared cadence is what was asked for. If Lea Main
ever needs its own independent pacing, split this by a key (e.g. model name)
the same way `daily_post_counter.target_key` splits by (phone, handle).
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


def last_post_at(path: str = DEFAULT_PATH) -> float | None:
    return _load(path).get("last_post_at")


def record_post(now: float | None = None, path: str = DEFAULT_PATH) -> None:
    """Stamp the moment a post just went out. Called once per confirmed post."""
    now = now if now is not None else time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"last_post_at": now}, fh)
    os.replace(tmp, path)


def gap_elapsed(now: float | None = None, gap_minutes: float | None = None,
                 path: str = DEFAULT_PATH) -> bool:
    """True once `gap_minutes` (default: env/90) have passed since the last
    recorded post, or there has never been one."""
    now = now if now is not None else time.time()
    gap = gap_minutes if gap_minutes is not None else min_gap_minutes()
    last = last_post_at(path)
    if last is None:
        return True
    return (now - last) >= gap * 60.0
