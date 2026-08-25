"""Robot-check history per mailbox, so a retry pins the same proxy.

2026-08-24: the same four test mailboxes hit `google_robot_check` on every
platform tried today -- GeeLark across three separate rounds, then real
MultiLogin -- each round on a *different* profile, so a *different* proxy.
Google's own account-risk scoring reads "this account signing in from a
rotating set of IPs in a short window" as suspicious on its own, which is
plausibly why the accounts got worse instead of better as testing went on.

The fix: once a mailbox has been tried on a profile, a retry stays on that
*same* profile (same proxy) rather than jumping to a fresh one -- one
rotation allowed if that profile itself turns out unusable, never more.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE_PATH = Path.home() / ".adb_bot" / "mailbox_robot_check.json"

# Matches the phone-side retry ceiling already used for install/sign-in
# stalls (google_signin.DEFAULT_SIGNIN_RETRIES) -- five tries is what "give
# this proxy a real chance" means everywhere else in this codebase.
MAX_ATTEMPTS_BEFORE_COOLDOWN = 5
COOLDOWN_SECONDS = 6 * 60 * 60
MAX_PROXY_ROTATIONS = 1


def _load(path: Path | None = None) -> dict:
    path = path or STATE_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def record_robot_check(address: str, profile_id: str, path: Path | None = None,
                       clock=time.time) -> dict:
    """Log one robot_check hit for `address` on `profile_id`; returns the entry.

    Landing on a different profile than last time counts as the one allowed
    rotation and restarts the attempt count -- a fresh proxy's first try is
    not the fifth failure of a streak that happened on a different one.
    """
    data = _load(path)
    entry = data.get(address, {"profile_id": "", "attempts": 0,
                               "rotations": 0, "cooldown_until": 0})
    if entry.get("profile_id") and entry["profile_id"] != profile_id:
        entry["rotations"] = entry.get("rotations", 0) + 1
        entry["attempts"] = 1
    else:
        entry["attempts"] = entry.get("attempts", 0) + 1
    entry["profile_id"] = profile_id
    entry["last_seen"] = clock()
    if entry["attempts"] >= MAX_ATTEMPTS_BEFORE_COOLDOWN:
        entry["cooldown_until"] = clock() + COOLDOWN_SECONDS
        entry["attempts"] = 0
    data[address] = entry
    _save(data, path)
    return entry


def clear(address: str, path: Path | None = None) -> None:
    """A successful sign-in wipes the streak -- the account is not burned."""
    data = _load(path)
    if address in data:
        del data[address]
        _save(data, path)


def pinned_profile(address: str, path: Path | None = None) -> str | None:
    """The profile a retry on `address` should use, or None for a first try."""
    return _load(path).get(address, {}).get("profile_id") or None


def ready_at(address: str, path: Path | None = None,
            clock=time.time) -> float:
    """Unix time `address` may be tried again, or 0.0 if not in cooldown."""
    entry = _load(path).get(address)
    if not entry:
        return 0.0
    until = entry.get("cooldown_until", 0)
    return until if until > clock() else 0.0


def may_use_profile(address: str, profile_id: str, path: Path | None = None) -> bool:
    """Whether `profile_id` is an allowed proxy for `address` right now.

    True for a first-ever try, for the pinned profile itself, or for one
    replacement profile once the rotation budget (`MAX_PROXY_ROTATIONS`) is
    still unspent. False past that -- the caller should reuse the pinned
    profile rather than reach for yet another fresh one.
    """
    entry = _load(path).get(address)
    if not entry or not entry.get("profile_id"):
        return True
    if entry["profile_id"] == profile_id:
        return True
    return entry.get("rotations", 0) < MAX_PROXY_ROTATIONS
