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
from datetime import datetime, timezone

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
OUTCOME_ACCOUNT_ABSENT = "account_absent"  # the phone does not carry this account


class AccountNotOnPhone(Exception):
    """The probe proved the row's handle is not in that phone's switcher.

    Raised by `read_post_count`, because this is the one failure that no
    amount of retrying fixes: the counter it would have to read belongs to an
    account the phone does not carry. Distinct from returning None ("we could
    not read the screen"), which is worth another pass.
    """


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


def _handle_key(value) -> str:
    """Normalise a target handle for comparison ('@Jiji.LL12 ' -> 'jiji.ll12')."""
    return str(value or "").strip().lstrip("@").lower()


def _same_account_test(entry, shares):
    """A predicate matching shares taken on the *same account* as `entry`, or
    None when this evidence cannot be used on this phone at all.

    Two accounts on one phone have unrelated counters, so a reading only means
    anything next to another reading of the same counter. There are two cases:

    * `entry` carries a handle -- the row was account-driven. Match on profile
      *and* handle, which is what tells the phone's two accounts apart.
    * `entry` carries no handle. **That is the single-account case, not a
      missing field**: the posting flow only sets `target_handle` when it has
      to switch accounts first ("no handle means a single-account phone, and
      this does nothing at all", `instagram_reel`). On such a phone the profile
      *is* the account and the profile id alone is a sound key.

    Refusing the second case is how this evidence came to be dead for most of
    the fleet: posting is profile-driven now, so almost every record has an
    empty handle, and the one check that can settle a share with no device
    returned None before it read anything. 119 shares sat unresolved on
    2026-08-19, holding their variants and starving 23 profiles.

    The safety condition for the second case is that the phone really does hold
    one account. A handle appearing on *any* share for this profile means it
    holds two, whatever the MLX tags say -- the tags are known to be incomplete
    (8 carry it; roughly twenty phones hold two accounts), so the ledger's own
    evidence decides rather than the label.
    """
    entry_handle = _handle_key(entry.target_handle)
    if entry_handle:
        return lambda share: (str(share.profile_id) == str(entry.profile_id)
                              and _handle_key(share.target_handle) == entry_handle)

    for share in shares:
        if (str(share.profile_id) == str(entry.profile_id)
                and _handle_key(share.target_handle)):
            return None        # two accounts here; the counter is ambiguous
    return lambda share: str(share.profile_id) == str(entry.profile_id)


def disprove_from_later_share(entry, shares) -> tuple | None:
    """Prove a parked post did NOT land, using the next post's opening count.

    The mirror of `confirm_from_later_share`, and the half that was missing.
    Confirming a share stops it being re-sent, which the ledger already did by
    blocking; **disproving is what lets the clip go out again**, and without it
    a share that was tapped and never proved holds its variant for good. The
    retry pass then refuses the row for ever, the queue reports the profile's
    only Ready variant as held, and the profile stops posting -- which is what
    `No Recent Success` has mostly been.

    Deliberately narrower than the confirming direction:

    * **Flat only.** The next reading must equal this one. A *decrease* proves
      nothing -- a deleted post, or a counter read off the wrong screen -- and
      `77 -> 1` is a misread, not a disappearance.
    * **Exactly one share in the window**, this one. With two shares and a flat
      counter neither landed, but saying so needs both rows, and this rules on
      one; the extra caution costs a cycle and avoids reasoning about a set.

    Returns (OUTCOME_FAILED, detail) or None. None is the safe answer: the
    caller goes on blocking the clip exactly as it does today.
    """
    if entry.baseline_count < 0 or not entry.baseline_exact:
        return None
    same_account = _same_account_test(entry, shares)
    if same_account is None:
        return None

    later = [s for s in shares
             if same_account(s) and (s.shared_at or 0.0) > (entry.shared_at or 0.0)
             and s.baseline_count >= 0 and s.baseline_exact]
    if not later:
        return None

    nxt = min(later, key=lambda s: s.shared_at or 0.0)
    if nxt.baseline_count != entry.baseline_count:
        return None

    between = [s for s in shares
               if same_account(s)
               and (entry.shared_at or 0.0) <= (s.shared_at or 0.0) < (nxt.shared_at or 0.0)]
    if len(between) != 1:
        return None

    who = f"@{_handle_key(entry.target_handle)}" if entry.target_handle else "this phone"
    return (OUTCOME_FAILED,
            f"the next post on {who} opened at {nxt.baseline_count}, the same count "
            f"this one read before sharing -- nothing landed in between, so this reel did not")


def confirm_from_later_share(entry, shares) -> tuple | None:
    """Prove a parked post landed using the *next* post's opening count.

    Returns (OUTCOME_POSTED, detail) or None when this evidence does not apply.

    Every share records the account's post count read moments before Share. So
    a later share on the same account is a second reading of the same counter,
    taken under ideal conditions -- no upload in flight, no phone to hold, no
    15-minute wait. If that later reading is higher, posts landed in between.

    This exists because the device probe is not always available and is not
    always trustworthy: on a two-account phone it has to switch accounts first,
    and when the switcher refuses (the account is signed out, or absent) the
    probe correctly declines to report a count -- forever. Jil 5 sat on three
    rows in `Verifying` for a day whose own baselines read 106, 107, 109: the
    proof each one landed was already on disk, in the next row.

    Conservative on purpose:

    * Both readings must be exact, on the same profile *and* the same handle.
      Profile alone is not enough -- two accounts share a phone, and their
      counters are unrelated.
    * A *decrease* proves nothing (a deletion, or a count read off the other
      account) and returns None rather than a verdict.
    * The rise must cover every share recorded in the window. Two shares and a
      +1 means one of them landed and this does not say which.
    """
    if entry.baseline_count < 0 or not entry.baseline_exact:
        return None
    same_account = _same_account_test(entry, shares)
    if same_account is None:
        return None
    handle = _handle_key(entry.target_handle) or str(entry.profile_id)

    later = [s for s in shares
             if same_account(s) and (s.shared_at or 0.0) > (entry.shared_at or 0.0)
             and s.baseline_count >= 0 and s.baseline_exact]
    if not later:
        return None

    nxt = min(later, key=lambda s: s.shared_at or 0.0)
    risen = nxt.baseline_count - entry.baseline_count
    if risen <= 0:
        return None

    # Everything shared on this account between the two readings, this one
    # included. The counter cannot tell two shares apart, so it only proves
    # this share landed if it accounts for all of them.
    between = [s for s in shares
               if same_account(s)
               and (entry.shared_at or 0.0) <= (s.shared_at or 0.0) < (nxt.shared_at or 0.0)]
    if risen < len(between):
        return None

    who = f"@{_handle_key(entry.target_handle)}" if entry.target_handle else "this phone"
    return (OUTCOME_POSTED,
            f"the next post on {who} opened at {nxt.baseline_count}, up from "
            f"{entry.baseline_count} before this one -- the counter moved, so this reel landed")


def _age_from_recheck_stamp(fields: dict, now_epoch: float):
    """Seconds since this row's `Recheck After` came due, or None if unreadable.

    The fallback age, for the one case with no ledger entry to read
    `shared_at` off. It is a *lower* bound on how long the share has gone
    unproven -- the stamp is written at share time plus the wait -- which is
    the safe direction: it can only delay a write-off, never bring one
    forward. None when the field is missing or unparseable, and the caller
    then leaves the row alone rather than writing it off on a guess.
    """
    raw = fields.get(at.F_PQ_RECHECK_AFTER)
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    # Airtable stamps UTC; a value that somehow arrives naive is read as UTC
    # rather than as local time, which would shift the age by the offset and,
    # west of Greenwich, write rows off early.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, now_epoch - stamp.timestamp())


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

    if outcome in (OUTCOME_FAILED, OUTCOME_ABANDONED, OUTCOME_ACCOUNT_ABSENT):
        # Now a retry is genuinely safe -- for OUTCOME_FAILED we have positive
        # evidence the reel is not on the account, which is exactly what the
        # in-run check could not establish.
        # `Account Not On Phone` is its own issue code because it is the one
        # `retry_runner` must not re-queue: the handle is missing, so every
        # retry costs a launch and a boot to reach the same answer.
        if outcome == OUTCOME_FAILED:
            issue = at.ISSUE_NEEDS_RETRY
        elif outcome == OUTCOME_ACCOUNT_ABSENT:
            issue = at.ISSUE_ACCOUNT_MISSING
        else:
            issue = at.ISSUE_OTHER
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
    tally = {"checked": 0, "posted": 0, "failed": 0, "unknown": 0, "abandoned": 0,
             "account_absent": 0}

    try:
        rows = airtable.list_posts_awaiting_recheck()
    except Exception as exc:
        log("warning", "Could not list posts awaiting recheck: %s", exc)
        return tally

    entries = {r.queue_id: r for r in store.pending() if r.queue_id}
    # Every share, not just the unresolved ones: the reading that proves a
    # parked post landed is usually the next post's, which has itself already
    # been confirmed and left `pending()`.
    all_shares = list(store.load().values())

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
            # a different host posted it, the ledger was pruned, or the row was
            # renumbered underneath it (the 2026-08-10 base migration did
            # exactly that to 23 shares). We cannot compare counts, so we still
            # do not invent a verdict.
            #
            # What we must not do is ask forever. `decide_recheck` writes a
            # share off at MAX_UNRESOLVED_AGE_SECONDS precisely so a row cannot
            # sit in Verifying for good -- but it reads the age off
            # `entry.shared_at`, which is behind this lookup, so a row with no
            # entry could never reach it. Seven rows sat here for two days
            # because of that, re-logged every pass and resolving on none of
            # them, and the loop watchdog called it a stall (which it was).
            age = _age_from_recheck_stamp(fields, now())
            if age is not None and age >= MAX_UNRESOLVED_AGE_SECONDS:
                log("warning", "No local ledger entry for queue row %s and its recheck came "
                    "due %.1fh ago; writing it off", queue_id, age / 3600)
                apply_recheck_outcome(
                    airtable, queue_id, account_id, account_name, OUTCOME_ABANDONED,
                    f"no local ledger entry and still unproven {age / 3600:.1f}h after the "
                    f"recheck came due -- giving up on proving this one",
                    variant_id=variant_id, flow=flow, logger=logger)
                tally["abandoned"] += 1
                continue
            log("warning", "No local ledger entry for queue row %s; leaving it in Verifying", queue_id)
            tally["unknown"] += 1
            continue

        # Free evidence first. A later share on the same account already read
        # the counter, so when that settles the question there is no reason to
        # open a phone -- and for an account the probe cannot switch to, this is
        # the only answer that will ever come.
        settled = confirm_from_later_share(entry, all_shares)
        if settled is not None:
            outcome, detail = settled
            log("info", "Recheck for %s (%s): %s -- %s (no device needed)",
                account_name, entry.profile_id, outcome, detail)
            apply_recheck_outcome(airtable, queue_id, account_id, account_name,
                                  outcome, detail, variant_id=variant_id,
                                  flow=flow, logger=logger)
            store.resolve(entry.profile_id, entry.media_hash,
                          post_ledger.STATUS_CONFIRMED, detail)
            tally["posted"] += 1
            continue

        tally["checked"] += 1
        try:
            current = read_post_count(entry.profile_id, fields)
        except AccountNotOnPhone as exc:
            # Terminal, and on purpose. The ledger is deliberately left blocking
            # the clip: we never learned whether it posted, only that this phone
            # can never tell us. Re-sending it on the strength of "no answer" is
            # how an account gets the same reel twice.
            detail = str(exc) or "the row's handle is not in this phone's account switcher"
            log("warning", "Recheck for %s (%s): giving up -- %s",
                account_name, entry.profile_id, detail)
            apply_recheck_outcome(airtable, queue_id, account_id, account_name,
                                  OUTCOME_ACCOUNT_ABSENT, detail, variant_id=variant_id,
                                  flow=flow, logger=logger)
            tally["account_absent"] += 1
            continue
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
