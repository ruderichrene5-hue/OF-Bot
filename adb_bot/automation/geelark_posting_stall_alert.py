"""Alerts to Telegram when GeeLark day-posting goes quiet during its active
window, instead of a stall only being noticed when someone happens to ask.

Added 2026-09-06 after a ~35-minute silent stall (a browser-style cookie-
consent page blocking the reel composer on several phones at once) went
unnoticed until asked about directly. Run on a short timer (systemd), not
folded into geelark_scheduler itself: the scheduler process can be the very
thing that's stuck, so the check needs to run independently of it.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
LEDGER_PATH = "/root/.adb_bot/posted_reels.jsonl"
STATE_PATH = Path("/root/.adb_bot/geelark_posting_stall_alert_state.json")
STALL_MINUTES = int(os.environ.get("ADBBOT_GEELARK_STALL_ALERT_MINUTES", "25"))
REALERT_MINUTES = int(os.environ.get("ADBBOT_GEELARK_STALL_REALERT_MINUTES", "60"))
# Matches geelark_scheduler's own active-posting window -- no point alerting
# on quiet minutes during Warmup-only overnight hours, that's expected.
# Day pass now runs 06:00-00:00 Berlin (was 06:00-23:00), changed 2026-09-06.
ACTIVE_START_HOUR = 6
ACTIVE_END_HOUR = 24


def last_geelark_share_ts(ledger_path: str = LEDGER_PATH) -> float | None:
    last = None
    try:
        with open(ledger_path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("platform") != "geelark":
                    continue
                ts = row.get("shared_at")
                if isinstance(ts, (int, float)) and (last is None or ts > last):
                    last = ts
    except FileNotFoundError:
        return None
    return last


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def in_active_window(now: datetime) -> bool:
    return ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR


def check(now: datetime | None = None, logger=None) -> bool:
    """Send an alert if posting has been quiet too long. Returns True if one
    was sent."""
    now = now or datetime.now(BERLIN)
    if not in_active_window(now):
        return False

    last_ts = last_geelark_share_ts()
    if last_ts is None:
        return False
    gap_minutes = (time.time() - last_ts) / 60.0

    state = _load_state()
    if gap_minutes < STALL_MINUTES:
        if state.get("stalled"):
            state["stalled"] = False
            _save_state(state)
        return False

    last_alert = state.get("last_alert_iso")
    if last_alert:
        try:
            since = datetime.fromisoformat(last_alert)
            if datetime.now(BERLIN) - since < timedelta(minutes=REALERT_MINUTES):
                return False
        except ValueError:
            pass

    from adb_bot.clients.telegram import TelegramNotifier
    notifier = TelegramNotifier()
    msg = (f"GeeLark Posting: seit {gap_minutes:.0f} Min kein neuer Post "
          f"(Schwelle {STALL_MINUTES} Min) -- Composer haengt vermutlich fest, "
          f"bitte pruefen.")
    sent = notifier.send(msg, logger=logger)
    state["stalled"] = True
    state["last_alert_iso"] = datetime.now(BERLIN).isoformat()
    _save_state(state)
    return sent


if __name__ == "__main__":
    check()
