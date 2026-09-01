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
from adb_bot.automation.flows import waits
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


def _emit(logger, level, message, *args):
    method = getattr(logger, level, None) if logger is not None else None
    if callable(method):
        method(message, *args)


def _open_instagram_u2(flow, target, adb_client, logger=None):
    """Bring Instagram to the foreground and to a stable, readable state.

    `_launch()` only brings up ADB -- it says nothing about what's on
    screen. Found live 2026-08-31: skipping this step gave a 100%
    "could not read the profile's post count" rate, because every phone
    landed on the recheck straight from a cold ADB connect, not already
    sitting in Instagram. Mirrors the setup `InstagramReelUploadU2Flow.run()`
    does for a real post, minus the parts (account baseline, composer) that
    only matter for posting.

    Returns a connected uiautomator2 device, or None if Instagram never
    came up.
    """
    for command in flow.build_launch_commands(target):
        adb_client.run_command(command)
        is_start = "monkey" in command or "am start" in command
        waits.settle(
            5 if is_start else 2,
            ready=(lambda: flow._ig_is_foreground(target, adb_client)) if is_start else None,
            logger=logger, what="Instagram in foreground",
        )
    try:
        d = u2.connect(target)
        d.implicitly_wait(flow.SELECTOR_WAIT_SECONDS)
    except Exception as exc:
        _emit(logger, "warning", "geelark_recheck: uiautomator2 could not connect to %s (%s)",
             target, exc)
        return None

    waits.settle(
        10,
        ready=waits.u2_ready(
            d,
            {"resourceId": "com.instagram.android:id/feed_tab"},
            {"resourceIdMatches": r"com\.instagram\.android:id/.*(tab_bar|profile_tab).*"},
        ),
        logger=logger, what="Instagram UI loaded",
    )
    flow._ensure_feed_usable_u2(d, target, adb_client, logger=logger)
    return d

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

    # post_ledger.py is written by ONE shared code path used by both MLX and
    # GeeLark (see ShareRecord.platform) -- `pending` above is everything
    # unresolved on BOTH fleets. Filter to GeeLark's own records BEFORE any
    # other processing (including the age-ceiling short-circuit below),
    # never after: sorting this out from a live phone list after the fact is
    # exactly what produced a wrong report live on 2026-08-31 (495 of 750
    # "GeeLark" shares that day were actually MLX). A record explicitly
    # tagged "mlx" is always excluded; a record tagged "geelark" is always
    # kept; a record with no tag at all (written before this field existed)
    # falls back to the phone-list check, which is what covers tonight's
    # existing backlog.
    try:
        geelark_ids = {str(row.get("id"))
                       for row in GeelarkPhoneClient(transport).list_phones()}
    except Exception as exc:
        if logger:
            logger.warning("geelark_recheck: could not list Geelark phones (%s)", exc)
        return []

    def _is_geelark(record) -> bool:
        if record.platform == "geelark":
            return True
        if record.platform:
            return False
        return record.profile_id in geelark_ids

    pending = [r for r in pending if _is_geelark(r)]
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

            d = _open_instagram_u2(flow, target, adb_client, logger=logger)
            if d is None:
                for record in records:
                    results.append({"phone_id": phone_id, "media_hash": record.media_hash,
                                    "outcome": "could_not_open_instagram"})
                continue

            # No account switching on Geelark -- unlike MLX, a Geelark phone
            # never carries two Instagram accounts (confirmed 2026-09-01;
            # target_handle is never set anywhere in the Geelark code path).
            # One post-count read is meaningful for every pending record on
            # this phone.
            current = None
            if flow._open_profile_tab_u2(d, target, logger=logger):
                waits.settle(2, ready=waits.u2_ready(d, *flow._POST_COUNT_SELECTORS),
                            logger=logger, what="profile header")
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
