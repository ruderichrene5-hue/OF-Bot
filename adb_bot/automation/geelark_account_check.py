"""Daily lightweight account-status sweep for GeeLark's Active_Posting fleet.

A real posting attempt already reads a phone's screen once before doing
anything else (`_check_for_challenge_and_abort`, inside every posting cycle)
and retags it away from Active_Posting if it finds human verification,
in review, logged out, or banned. But that only happens for phones the
scheduled pass actually picked up today -- of 172 Active_Posting phones on
2026-08-31, 92 never got a post attempt at all (no content due, or the day's
worklist simply didn't reach them), so their screen state was never looked
at, and a phone that silently flipped to "human verification" overnight
could sit unnoticed for days.

This module is the same one-screen-read-and-retag check, run as its own
pass over whichever Active_Posting phones haven't been touched *today* by
either a real post or this sweep itself -- open, read once, retag if
unhealthy, close. No scrolling, no SMS, no captcha-solving (that is
human_verification_pass's job, not this one). Requested 2026-08-31: "checke
nur ob die Seite ganz normal lädt oder ob es human gibt oder banned ist ...
damit wir jeden Tag aktualisieren."
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from adb_bot.automation import post_ledger
from adb_bot.automation.geelark_lifecycle import (
    TAG_ACTIVE_POSTING,
    _check_for_challenge_and_abort,
    _launch,
    phones_by_tag,
    stop_session,
)
from adb_bot.clients.geelark.transport import GeelarkTransport
from adb_bot.config.settings import get_app_data_dir

BERLIN = ZoneInfo("Europe/Berlin")
LOG_FILENAME = "geelark_account_check_log.jsonl"


def _berlin_day_start_epoch(now: datetime | None = None) -> float:
    now = now.astimezone(BERLIN) if now else datetime.now(BERLIN)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _already_checked_today(log_path: Path, day_start: float) -> set[str]:
    """Phone ids that don't need this sweep again today: already checked by
    this module's own log, or already checked implicitly by a real posting
    attempt (which reads the screen before doing anything else -- checking
    it again here would just be a second launch for the same answer)."""
    checked: set[str] = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            at = d.get("at")
            if isinstance(at, (int, float)) and at >= day_start:
                checked.add(str(d.get("phone_id")))

    ledger = post_ledger.PostLedger()
    for record in ledger.load().values():
        if record.platform == "mlx":
            continue
        if (record.shared_at or 0.0) >= day_start:
            checked.add(record.profile_id)
    return checked


def run_geelark_account_check(adb_client, transport=None, logger=None,
                              now: datetime | None = None,
                              concurrency: int | None = None) -> list[dict]:
    """Check every Active_Posting phone not yet touched today. Returns one
    dict per phone checked: {"phone_id", "name", "result"} -- result is
    "healthy", the new tag it was retagged to, "could_not_reach_over_adb",
    or "error".

    Runs `concurrency`-wide (default: geelark_scheduler.CONCURRENCY, the
    same 9-modem pool everything else uses) via run_queue -- was a plain
    sequential `for` loop until 2026-09-08, found live doing one phone at a
    time (~2 min each) while 8 of the 9 real proxies sat idle: a 203-phone
    sweep was on track to take ~6.5 hours instead of well under one.
    """
    transport = transport or GeelarkTransport()
    log_path = get_app_data_dir() / LOG_FILENAME
    day_start = _berlin_day_start_epoch(now)
    already = _already_checked_today(log_path, day_start)

    candidates = [p for p in phones_by_tag(TAG_ACTIVE_POSTING, transport)
                 if str(p.get("id")) not in already]
    if not candidates:
        return []

    from adb_bot.automation.geelark_scheduler import run_queue, CONCURRENCY
    from adb_bot.automation.geelark_recheck import (
        pending_by_phone, resolve_pending_on_open_session,
    )
    concurrency = concurrency or CONCURRENCY

    # Piggyback recheck on this pass's own open sessions -- added 2026-09-08.
    # Only for phones that ALSO need a health check today (this pass's own
    # candidate list); a phone with a pending recheck but no health check
    # due is left to geelark_recheck's own nightly run, which still covers
    # it. Computed once up front (read-only afterwards, safe across worker
    # threads) rather than per-phone, so 203 candidates does not mean 203
    # separate ledger scans.
    recheck_by_phone, _already_resolved = pending_by_phone(transport=transport, logger=logger)
    recheck_results_lock = threading.Lock()
    recheck_results: list[dict] = []

    def work_fn(phone_id: str, name: str) -> dict:
        session = None
        result = "error"
        try:
            session, target = _launch(phone_id, transport, logger, adb_client)
            if not target:
                result = "could_not_reach_over_adb"
            else:
                new_tag = _check_for_challenge_and_abort(
                    target, adb_client, phone_id, transport, name,
                    TAG_ACTIVE_POSTING, logger=logger)
                result = new_tag if new_tag else "healthy"
                # Only on a confirmed-healthy phone -- one that just got
                # retagged away (challenge/banned/etc.) is not a phone whose
                # post-count reading means anything right now.
                pending_records = recheck_by_phone.get(phone_id)
                if result == "healthy" and pending_records:
                    outcomes = resolve_pending_on_open_session(
                        phone_id, pending_records, target, adb_client, logger=logger)
                    with recheck_results_lock:
                        recheck_results.extend(outcomes)
        except Exception as exc:
            if logger:
                logger.exception("geelark_account_check: raised for %s (%s)", name, exc)
            result = "error"
        finally:
            if session is not None:
                try:
                    stop_session(session, logger=logger)
                except Exception as exc:
                    if logger:
                        logger.warning("geelark_account_check: stop_session failed for %s (%s)",
                                      name, exc)
        if logger:
            logger.info("geelark_account_check: %s (%s): %s", name, phone_id, result)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Append-only JSONL, same pattern as post_ledger.py/account_stats.py --
            # safe for several worker threads writing at once (a torn write
            # costs one line, not the whole file).
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": time.time(), "phone_id": phone_id,
                                        "name": name, "result": result}) + "\n")
        except Exception as exc:
            if logger:
                logger.warning("geelark_account_check: could not write log for %s (%s)",
                              name, exc)
        return {"phone_id": phone_id, "name": name, "result": result}

    results = run_queue(candidates, work_fn, concurrency=concurrency, logger=logger)
    if recheck_results and logger:
        logger.info("geelark_account_check: also resolved %d pending recheck record(s) "
                   "piggybacked on this pass's own open sessions", len(recheck_results))
    return results
