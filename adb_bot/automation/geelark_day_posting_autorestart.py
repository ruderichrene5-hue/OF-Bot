"""Restarts adbbot-geelark-day-posting.service on its own when it finishes
early with real backlog still untouched, instead of sitting idle until the
next fixed fire time (06:00/15:00 Berlin) or a person noticing and
restarting it by hand.

Added 2026-09-07 after exactly that happened live: a run finished cleanly
at 14:35 (not a crash, not a timeout -- its own worklist logic decided it
was done for that pass) with 99 of 182 Active_Posting-tagged phones never
touched that day, and sat idle for 49 minutes until asked about, restarted
by hand, twice, before this existed. Direct instruction: "weitermachen
nicht stoppen es ist nichtmal durch" -- the fleet should keep working
while there is real backlog and the active window is still open, without
needing a person to notice and restart it.

Deliberately conservative about what counts as "backlog": a phone with no
content for today already gets skipped fast and cheaply by the pass itself
(geelark_lifecycle's own "no content -> never launched" rule), so a false
positive here just costs one more quick pass, not a real problem -- but
restarting outside the active window, or while night-sequence is running,
would be wrong, so both are checked before ever starting anything.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

# Mirrors geelark_scheduler's day/night boundary -- day pass now runs
# 06:00-00:00 Berlin (2x/day fires, see CLAUDE.md), changed 2026-09-06.
ACTIVE_START_HOUR = 6
ACTIVE_END_HOUR = 24

DAY_SERVICE = "adbbot-geelark-day-posting.service"
NIGHT_SERVICE = "adbbot-geelark-night-sequence.service"


def in_active_window(now: datetime) -> bool:
    return ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR


_RUNNING_STATES = {"active", "activating", "reloading", "deactivating"}


def _service_active(name: str) -> bool:
    """True while the unit is doing anything at all, not just "active".

    `systemctl is-active --quiet` alone is the wrong check here: it only
    returns 0 for the exact "active" state, and a long-running Type=oneshot
    ExecStart shows "activating" for its *entire* run (there is no separate
    "active (running)" for oneshot) -- confirmed live 2026-09-07 testing
    this exact function while day-posting was genuinely mid-run. Trusting
    that return code would have made this checker think a live run was
    idle and started a second one on top of it -- the same overlap bug this
    session already fixed once for the day/night boundary, this time
    self-inflicted.
    """
    result = subprocess.run(["systemctl", "show", name, "-p", "ActiveState"],
                            capture_output=True, text=True)
    state = result.stdout.strip().removeprefix("ActiveState=")
    return state in _RUNNING_STATES


def _has_backlog(now: datetime) -> bool:
    """True if any Active_Posting-tagged phone has not shared today yet.
    Same undercount risk as everywhere else that reads this: a phone with
    no content today looks identical to one with unposted content -- see
    module docstring for why that is an acceptable false positive here."""
    from adb_bot.automation.geelark_lifecycle import phones_by_tag, TAG_ACTIVE_POSTING
    from adb_bot.automation.post_ledger import PostLedger

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    tagged_ids = {str(r.get("id")) for r in phones_by_tag(TAG_ACTIVE_POSTING)}
    if not tagged_ids:
        return False
    posted_ids = {
        rec.profile_id for rec in PostLedger().load().values()
        if rec.shared_at and rec.shared_at >= today_start
        and getattr(rec, "platform", "") == "geelark"
    }
    return bool(tagged_ids - posted_ids)


def check(now: datetime | None = None, logger=None) -> bool:
    """Start the day-posting service if it's idle, the window is open, and
    there's real backlog. Returns True if a restart was triggered."""
    now = now or datetime.now(BERLIN)
    if not in_active_window(now):
        return False
    if _service_active(DAY_SERVICE):
        return False
    if _service_active(NIGHT_SERVICE):
        # Same mutual exclusion as the service's own ExecStartPre guard --
        # this checker must not race that guard by starting a job that
        # would just get refused.
        return False
    if not _has_backlog(now):
        return False

    if logger:
        logger.info("geelark_day_posting_autorestart: day-posting is idle with "
                   "backlog remaining -- restarting")
    subprocess.run(["systemctl", "start", DAY_SERVICE])
    return True


if __name__ == "__main__":
    from adb_bot.core.logger import get_logger
    logger = get_logger("adb_bot")
    started = check(logger=logger)
    print("restarted" if started else "nothing to do")
