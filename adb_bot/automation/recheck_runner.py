"""Resolve posts that could not be proven while the run was still happening.

The posting flow now spends about 45 seconds trying to confirm a reel and then
stops, because a profile may not occupy a phone for longer than five minutes and
because an unproven post is no longer reported as a failure. What it does
instead is park the row in `Verifying` with a `Recheck After` stamp. This module
is what comes back for it.

Why later is easier: the in-run check is racing Instagram's upload pipeline --
the counter is a cached aggregate that routinely takes a minute or more to move,
so a fast "no" says nothing. Fifteen minutes on, nothing is in flight. The
counter has settled, the reel is in the grid, and the same probe that was
guessing during the run gives a clean answer. The hard part of verification was
never the reading, it was the timing.

The comparison is exact rather than heuristic: `post_ledger` carries the post
count captured just before Share, so this asks "is it higher than it was?" and
not "does this screen look like success?".

`decide_recheck` is pure and takes the two counts, so the whole ruling is unit
tested without a device.
"""

from __future__ import annotations

import time

from adb_bot.clients import airtable as at
from adb_bot.automation import post_ledger

# A share this old with no answer is written off. Long enough that no plausible
# upload is still running, short enough that the row doesn't sit in Verifying
# forever -- an un-resolvable row is itself a signal that something is broken.
MAX_UNRESOLVED_AGE_SECONDS = 24 * 3600

OUTCOME_POSTED = "posted"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"      # still can't tell; leave it for the next pass
OUTCOME_ABANDONED = "abandoned"  # too old to keep asking


def decide_recheck(baseline_count: int, baseline_exact: bool, current, age_seconds: float,
                   max_age_seconds: float = MAX_UNRESOLVED_AGE_SECONDS) -> tuple:
    """Rule on one parked post. Returns (outcome, detail).

    `current` is a reel_verify.Count or None.

    The reasoning that matters is the *equal* case. During a run, "the count did
    not move" means almost nothing -- the counter lags. Fifteen minutes later it
    means the post is not there, and that is the one moment we can say Failed
    with confidence. Being able to say that is the entire point of waiting.
    """
    if age_seconds >= max_age_seconds:
        return (OUTCOME_ABANDONED,
                f"no answer after {age_seconds / 3600:.1f}h -- giving up on proving this one")

    # Without a trustworthy baseline there is nothing to compare against. Say so
    # rather than inventing a verdict: a wrong Failed here re-posts the reel.
    if baseline_count < 0 or not baseline_exact:
        return (OUTCOME_UNKNOWN,
                "no exact baseline count was captured before the post, so a +1 cannot be checked")
    if current is None:
        return (OUTCOME_UNKNOWN, "could not read the profile's post count")
    if not current.exact:
        return (OUTCOME_UNKNOWN, f"profile count is rounded ({current.value}); +1 is invisible")

    if current.value > baseline_count:
        return (OUTCOME_POSTED, f"post count {baseline_count} -> {current.value}")
    if current.value == baseline_count:
        return (OUTCOME_FAILED,
                f"post count is still {baseline_count} after {age_seconds / 60:.0f} min -- "
                "the reel did not land")
    # Fewer posts than before: something was deleted, or we're reading a
    # different account. Either way it is not proof this reel posted.
    return (OUTCOME_UNKNOWN,
            f"post count went down ({baseline_count} -> {current.value}); not a reliable comparison")


def apply_recheck_outcome(airtable, queue_id: str, account_id, account_name: str,
                          outcome: str, detail: str, variant_id: str = "",
                          flow: str = "instagram_reel_upload_u2", logger=None) -> bool:
    """Write one resolved recheck back to Airtable. Returns True when the row
    reached a terminal state (so the caller can stop tracking it).

    `account_id` may be None/empty for a profile-driven row (no Accounts row
    exists). The queue-row write-back is what actually closes the question, so it
    must not depend on the link: `create_run_log` omits the Account link and
    `set_account_result` no-ops, and the row still leaves `Verifying`."""
    if outcome == OUTCOME_POSTED:
        airtable.mark_post_result(queue_id, at.POST_STATUS_POSTED, at.ISSUE_NONE)
        if variant_id:
            airtable.mark_variant_used(variant_id)
        airtable.create_run_log(account_id, account_name, flow, at.RESULT_DONE,
                                f"confirmed by deferred recheck ({detail})")
        airtable.set_account_result(account_id, f"{at.RESULT_DONE}: {flow} (recheck confirmed)")
        return True

    if outcome in (OUTCOME_FAILED, OUTCOME_ABANDONED):
        # Now a retry is genuinely safe -- for OUTCOME_FAILED we have positive
        # evidence the reel is not on the account, which is exactly what the
        # in-run check could not establish.
        issue = at.ISSUE_NEEDS_RETRY if outcome == OUTCOME_FAILED else at.ISSUE_OTHER
        airtable.mark_post_result(queue_id, at.POST_STATUS_FAILED, issue)
        airtable.create_run_log(account_id, account_name, flow, at.RESULT_FAILED,
                                f"deferred recheck: {detail}")
        airtable.set_account_result(account_id, f"{at.RESULT_FAILED}: {flow} (recheck: {detail})")
        return True

    # Still unknown: push the stamp out and look again next pass rather than
    # guessing. The ledger keeps blocking a re-post in the meantime.
    airtable.mark_post_pending_verification(
        queue_id, note=f"recheck inconclusive: {detail}")
    return False


def recheck_pending_posts(airtable, read_post_count, ledger=None, logger=None,
                          now=time.time, flow: str = "instagram_reel_upload_u2") -> dict:
    """Work through every Posting Queue row whose Recheck After has passed.

    `read_post_count(profile_id, account_fields) -> reel_verify.Count | None` is
    injected: it is the only part that needs a device, which keeps the decision
    logic above testable and lets a caller supply whatever navigation it has.

    Returns a tally: {'checked', 'posted', 'failed', 'unknown', 'abandoned'}.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level, logger.info)(message, *args)

    store = ledger or post_ledger.PostLedger()
    tally = {"checked": 0, "posted": 0, "failed": 0, "unknown": 0, "abandoned": 0}

    try:
        rows = airtable.list_posts_awaiting_recheck()
    except Exception as exc:
        log("warning", "Could not list posts awaiting recheck: %s", exc)
        return tally

    entries = {r.queue_id: r for r in store.pending() if r.queue_id}

    # Profile names are read at most once, and only if a profile-driven row shows
    # up: a pass over ordinary account rows should not pay for a Profiles table
    # read, and no pass should pay for one per row.
    profile_names: dict = {}
    names_loaded = [False]

    def profile_name_for(profile_id: str) -> str:
        if not names_loaded[0]:
            names_loaded[0] = True
            try:
                profile_names.update(airtable.profile_launch_map() or {})
            except Exception as exc:
                log("warning", "Could not read profile names for the recheck pass: %s", exc)
        return str((profile_names.get(profile_id) or {}).get("name") or "")

    for row in rows or []:
        queue_id = row.get("id")
        fields = row.get("fields", {}) or {}
        accounts = fields.get(at.F_PQ_TARGET_ACCOUNT) or []
        account_id = accounts[0] if accounts else ""
        profiles = fields.get(at.F_PQ_TARGET_PROFILE) or []
        profile_link_id = profiles[0] if profiles else ""
        account_name = str(fields.get(at.F_PQ_NAME) or "")
        if not account_id and profile_link_id:
            # Profile-driven row: no Accounts row to link or stamp, so the
            # profile's name stands in for the handle. A row carrying *both*
            # links keeps the account path -- the Accounts row is what holds the
            # health guards, and quietly targeting the profile would bypass them.
            account_name = profile_name_for(profile_link_id) or account_name
        variants = fields.get(at.F_PQ_SPOOF_VARIANT) or []
        variant_id = variants[0] if variants else ""

        entry = entries.get(queue_id)
        if entry is None:
            # Airtable says Verifying but this machine has no ledger record --
            # a different host posted it, or the ledger was pruned. We cannot
            # compare counts, so don't pretend to: leave it for a human.
            log("warning", "No local ledger entry for queue row %s; leaving it in Verifying", queue_id)
            tally["unknown"] += 1
            continue

        tally["checked"] += 1
        try:
            current = read_post_count(entry.profile_id, fields)
        except Exception as exc:
            log("warning", "Recheck probe failed for %s: %s", entry.profile_id, exc)
            current = None

        outcome, detail = decide_recheck(
            entry.baseline_count, entry.baseline_exact, current,
            max(0.0, now() - (entry.shared_at or 0.0)),
        )
        log("info", "Recheck for %s (%s): %s -- %s", account_name, entry.profile_id, outcome, detail)

        # Unconditional on purpose. Gating this on a linked Account left every
        # profile-driven row stuck in Verifying while the ledger below recorded
        # it as resolved -- two records permanently disagreeing, which is worse
        # than never having rechecked. The write-back needs the queue row, not
        # the account.
        apply_recheck_outcome(airtable, queue_id, account_id, account_name,
                              outcome, detail, variant_id=variant_id,
                              flow=flow, logger=logger)

        if outcome == OUTCOME_POSTED:
            store.resolve(entry.profile_id, entry.media_hash,
                          post_ledger.STATUS_CONFIRMED, detail)
            tally["posted"] += 1
        elif outcome == OUTCOME_FAILED:
            # Clearing the ledger is what re-opens this clip for another send.
            # Only ever done on positive evidence of absence.
            store.resolve(entry.profile_id, entry.media_hash,
                          post_ledger.STATUS_DISPROVED, detail)
            tally["failed"] += 1
        elif outcome == OUTCOME_ABANDONED:
            # Deliberately NOT disproved: we still don't know, so the clip stays
            # blocked. Abandoning the question is not the same as answering it.
            tally["abandoned"] += 1
        else:
            tally["unknown"] += 1

    return tally
