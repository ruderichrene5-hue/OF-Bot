"""Hourly GeeLark posting-throughput check: alerts to Telegram when a
completed clock hour's share count is far below the last few hours', instead
of a real slowdown only being noticed when someone happens to ask why the
numbers look low.

Added 2026-09-06/07 after a confirmed real ~40-50% throughput drop (composer
false-failures from a stuck/resumed draft skipping the gallery screen --
fixed in instagram_reel.py's stuck-draft discard) that first looked like
normal variance and needed explicit pushback before it was actually
investigated and found to be a real bug, not noise.

Deliberately separate from geelark_posting_stall_alert.py: that one catches
posting going fully *silent* (no share at all in N minutes). This one
catches posting that never stops but does much less than usual -- a stall
check alone would stay quiet through exactly the kind of drop that prompted
this file to be written.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
LEDGER_PATH = "/root/.adb_bot/posted_reels.jsonl"
STATE_PATH = Path("/root/.adb_bot/geelark_hourly_throughput_state.json")

# Same active window as geelark_posting_stall_alert.py -- day pass now runs
# 06:00-00:00 Berlin (2x/day fires at 06:00/15:00, 9-modem pool), changed
# 2026-09-06. Outside this window only Warmup runs, low volume is expected.
ACTIVE_START_HOUR = 6
ACTIVE_END_HOUR = 24

BASELINE_HOURS = 3          # how many completed hours before "current" to average
MIN_BASELINE = 8            # baseline below this -> a "drop" is just noise, skip
DROP_THRESHOLD = 0.65       # current hour <= 65% of baseline counts as a real drop
REALERT_MINUTES = 120       # don't re-alert for the same ongoing slump more than this often


def _hour_bucket_counts(hours_back: int, now: datetime) -> dict[int, int]:
    """hours-ago -> geelark share count, for hours-ago in 0..hours_back.
    hours-ago=0 is the hour still in progress (partial, not used to judge a
    drop), 1 is the most recently *completed* clock hour."""
    counts = {i: 0 for i in range(hours_back + 1)}
    cutoff = now - timedelta(hours=hours_back + 1)
    try:
        with open(LEDGER_PATH) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("platform") != "geelark":
                    continue
                ts = row.get("shared_at")
                if not isinstance(ts, (int, float)):
                    continue
                dt = datetime.fromtimestamp(ts, BERLIN)
                if dt < cutoff or dt > now:
                    continue
                hours_ago = int((now - dt).total_seconds() // 3600)
                if hours_ago in counts:
                    counts[hours_ago] += 1
    except FileNotFoundError:
        pass
    return counts


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def check(now: datetime | None = None, logger=None) -> bool:
    """Send an alert if the most recently completed hour is a real drop
    against the recent baseline. Returns True if one was sent."""
    now = now or datetime.now(BERLIN)
    if not (ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR):
        return False

    counts = _hour_bucket_counts(BASELINE_HOURS + 1, now)
    current = counts.get(1, 0)
    baseline_hours = [counts.get(i, 0) for i in range(2, 2 + BASELINE_HOURS)]
    if not baseline_hours:
        return False
    baseline = sum(baseline_hours) / len(baseline_hours)

    if baseline < MIN_BASELINE:
        return False
    if current > baseline * DROP_THRESHOLD:
        return False

    state = _load_state()
    last_alert = state.get("last_alert_iso")
    if last_alert:
        try:
            since = now - datetime.fromisoformat(last_alert)
            if since < timedelta(minutes=REALERT_MINUTES):
                return False
        except ValueError:
            pass

    drop_pct = (1 - current / baseline) * 100 if baseline else 0.0
    from adb_bot.clients.telegram import TelegramNotifier
    notifier = TelegramNotifier()
    msg = (f"GeeLark Posting: letzte volle Stunde nur {current} Shares vs. "
          f"~{baseline:.0f} im Schnitt der {len(baseline_hours)} Stunden davor "
          f"({drop_pct:.0f}% Einbruch) -- kein normales Rauschen mehr bei "
          f"dieser Groessenordnung, bitte pruefen.")
    sent = notifier.send(msg, logger=logger)
    state["last_alert_iso"] = now.isoformat()
    _save_state(state)
    return sent


if __name__ == "__main__":
    check()
