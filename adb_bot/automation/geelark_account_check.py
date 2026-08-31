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
                              now: datetime | None = None) -> list[dict]:
    """Check every Active_Posting phone not yet touched today. Returns one
    dict per phone checked: {"phone_id", "name", "result"} -- result is
    "healthy", the new tag it was retagged to, "could_not_reach_over_adb",
    or "error"."""
    transport = transport or GeelarkTransport()
    log_path = get_app_data_dir() / LOG_FILENAME
    day_start = _berlin_day_start_epoch(now)
    already = _already_checked_today(log_path, day_start)

    candidates = [p for p in phones_by_tag(TAG_ACTIVE_POSTING, transport)
                 if str(p.get("id")) not in already]
    if not candidates:
        return []

    results: list[dict] = []
    for phone in candidates:
        phone_id = str(phone.get("id"))
        name = str(phone.get("serialName") or phone_id)
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
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": time.time(), "phone_id": phone_id,
                                        "name": name, "result": result}) + "\n")
        except Exception as exc:
            if logger:
                logger.warning("geelark_account_check: could not write log for %s (%s)",
                              name, exc)
        results.append({"phone_id": phone_id, "name": name, "result": result})
    return results
