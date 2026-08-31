"""Deferred recheck for GeeLark's post_ledger entries.

recheck_runner.py (the MLX-side equivalent) is entirely Airtable-bound --
queue_id, POST_STATUS_*, apply_recheck_outcome all write back to a Posting
Queue row that only MLX profiles have. GeeLark phones have no Airtable row
at all, so every "uncertain" GeeLark share (post_ledger.STATUS_SHARED) had
no path back to CONFIRMED/DISPROVED -- it just sat there forever, blocking
a resend of that exact clip and never telling anyone what actually
happened. Found live 2026-08-31: 130 unresolved shares in one day with no
mechanism that would ever touch them.

Reuses recheck_runner.decide_recheck for the actual verdict -- that logic
is already generic (baseline count in, current count in, age in, verdict
out) and already carries the reasoning that matters: an unmoved count
means nothing minutes after Share, and means "it did not land" once
enough time has passed for Instagram's own counter to have settled. See
that function's docstring. What's GeeLark-specific here is only the
launch/read/write-back plumbing around it.
"""

from __future__ import annotations

import time

from adb_bot.automation import post_ledger
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
from adb_bot.automation.geelark_lifecycle import _launch, stop_session
from adb_bot.automation.recheck_runner import (
    MAX_UNRESOLVED_AGE_SECONDS,
    OUTCOME_FAILED,
    OUTCOME_POSTED,
    decide_recheck,
)
from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.clients.geelark.transport import GeelarkTransport

try:
    import uiautomator2 as u2
except Exception:  # pragma: no cover - matches instagram_reel.py's own guard
    u2 = None

# Same floor as the MLX module's own reasoning: give Instagram's post-count
# cache time to settle before trusting a comparison against it. Explicit
# here as a pending() filter rather than left implicit in scheduling
# cadence, since this pass has no fixed relationship to when a share
# actually happened.
RECHECK_AFTER_SECONDS = 15 * 60


def run_geelark_recheck(adb_client, transport=None, logger=None) -> list[dict]:
    """Work every GeeLark share old enough to recheck. Returns one dict per
    ledger entry processed: {"phone_id", "media_hash", "outcome", "detail"}."""
    transport = transport or GeelarkTransport()
    ledger = post_ledger.PostLedger()
    pending = ledger.pending(older_than_seconds=RECHECK_AFTER_SECONDS)
    if not pending:
        return []

    results: list[dict] = []
    now = time.time()

    # decide_recheck's own age check short-circuits to ABANDONED before it
    # ever looks at `current` -- so a record already past
    # MAX_UNRESOLVED_AGE_SECONDS gets the same verdict whether or not we
    # spend a phone launch reading its post count. Settle those without
    # launching anything; only the rest are worth reaching a phone for.
    worklist = []
    for record in pending:
        age_seconds = max(0.0, now - (record.shared_at or 0.0))
        if age_seconds >= MAX_UNRESOLVED_AGE_SECONDS:
            outcome, detail = decide_recheck(record.baseline_count, record.baseline_exact,
                                             None, age_seconds)
            if logger:
                logger.info("geelark_recheck: %s (%s): %s -- %s (no launch, already too old)",
                           record.profile_id, record.media_hash[:12], outcome, detail)
            results.append({"phone_id": record.profile_id, "media_hash": record.media_hash,
                            "outcome": outcome, "detail": detail})
        else:
            worklist.append(record)
    if not worklist:
        return results

    try:
        geelark_ids = {str(row.get("id"))
                       for row in GeelarkPhoneClient(transport).list_phones()}
    except Exception as exc:
        if logger:
            logger.warning("geelark_recheck: could not list Geelark phones (%s)", exc)
        return results

    by_phone: dict[str, list] = {}
    for record in worklist:
        if record.profile_id in geelark_ids:
            by_phone.setdefault(record.profile_id, []).append(record)
    if not by_phone:
        return results

    if u2 is None:
        if logger:
            logger.warning("geelark_recheck: uiautomator2 is not importable; skipping")
        return results

    flow = InstagramReelUploadU2Flow()
    for phone_id, records in by_phone.items():
        session = None
        try:
            session, target = _launch(phone_id, transport, logger, adb_client)
            if not target:
                for record in records:
                    results.append({"phone_id": phone_id, "media_hash": record.media_hash,
                                    "outcome": "could_not_reach_over_adb"})
                continue

            d = u2.connect(target)
            current = None
            if flow._open_profile_tab_u2(d, target, logger=logger):
                current = flow._read_post_count_u2(d, target, logger=logger)

            for record in records:
                age_seconds = max(0.0, time.time() - (record.shared_at or 0.0))
                outcome, detail = decide_recheck(
                    record.baseline_count, record.baseline_exact, current, age_seconds,
                )
                if logger:
                    logger.info("geelark_recheck: %s (%s): %s -- %s",
                               phone_id, record.media_hash[:12], outcome, detail)
                if outcome == OUTCOME_POSTED:
                    ledger.resolve(record.profile_id, record.media_hash,
                                   post_ledger.STATUS_CONFIRMED, f"geelark_recheck: {detail}")
                elif outcome == OUTCOME_FAILED:
                    # Only ever done on positive evidence of absence -- this
                    # is what re-opens the clip for another send.
                    ledger.resolve(record.profile_id, record.media_hash,
                                   post_ledger.STATUS_DISPROVED, f"geelark_recheck: {detail}")
                # OUTCOME_UNKNOWN / OUTCOME_ABANDONED: leave the ledger as is
                # -- still blocking, on purpose (see post_ledger.blocks_repost).
                results.append({"phone_id": phone_id, "media_hash": record.media_hash,
                                "outcome": outcome, "detail": detail})
        except Exception as exc:
            if logger:
                logger.exception("geelark_recheck: raised for %s (%s)", phone_id, exc)
            results.append({"phone_id": phone_id, "outcome": "error", "detail": str(exc)})
        finally:
            if session is not None:
                try:
                    stop_session(session, logger=logger)
                except Exception as exc:
                    if logger:
                        logger.warning("geelark_recheck: stop_session failed for %s (%s)",
                                      phone_id, exc)
    return results
