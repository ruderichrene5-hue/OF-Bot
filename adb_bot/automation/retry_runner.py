"""Put failed posts back in the queue -- when, and only when, that is safe.

Airtable marks a row `Failed` and nothing has ever marked it back. Every failure
is therefore terminal: a dropped ADB connection at 03:00 costs a post and a
morning of manual clicking, which is the opposite of a cycle that runs
unattended. This module is the step that was missing.

The reason it is code and not an Airtable automation is the ledger. Airtable
knows a row failed; it does not know whether the reel actually went out. Those
are different facts, and the expensive one is the second: a post_ledger record in
`shared` means Share was tapped and the outcome was never resolved, so the reel
may well be live on the account. Re-queueing that row is how you get a double
post -- the exact failure post_ledger exists to prevent. So the ledger, not
Airtable, has the final say, and `ShareRecord.blocks_repost()` is that say.

Everything unknown resolves to "don't retry". A profile we cannot resolve, a
variant file that has been deleted, a hash that will not compute -- each of those
means we cannot ask the ledger the question, and an unanswerable question is
never a yes. A missed retry costs one post; a wrong retry costs an account.

And a retry never re-sends the clip that was already sent. Even the best evidence
of absence -- a post count that has not moved for hours -- is evidence rather than
proof, and Instagram does sometimes publish a reel long after the phone let go of
it. So a row whose clip reached Share is re-queued carrying a *different* video:
if the original does turn up later the account has posted two different reels an
hour apart, which is simply its normal cadence, instead of the same reel twice.
When there is nothing fresh to send, the slot is dropped. That trade is
deliberate and it is not close -- a lost post costs a slot, a duplicate costs the
client's trust.

`decide_retry` is pure and takes the already-resolved facts, so the whole ruling
is unit tested without Airtable, without a device and without a real video.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.clients import airtable as at
from adb_bot.automation import post_ledger

# How many attempts a row gets before it stops being retried automatically.
# The posting runner bumps Retry Count on each failed attempt, so this counts
# real attempts: 3 means the row is tried up to three times and then waits for a
# person. A row that has failed three times is not failing by chance.
DEFAULT_MAX_RETRIES = 3

# Backoff: 15min * 2**retry_count, capped. 15 minutes is the same settling time
# the deferred recheck uses -- long enough that whatever transient broke the run
# (phone rebooting, MLX restarting, Instagram throttling a device) has had a
# chance to clear, short enough to still land inside the same posting window.
# The cap keeps a third retry from drifting into the middle of the night: 4h
# after a 15/30/60-minute ladder still leaves the row inside a normal day.
RETRY_BACKOFF_BASE_SECONDS = 15 * 60
RETRY_BACKOFF_MAX_SECONDS = 4 * 3600

OUTCOME_RETRY = "retry"              # eligible; goes back to Pending
OUTCOME_NOT_FAILED = "not_failed"    # not a Failed row at all -- nothing to do
OUTCOME_NEEDS_HUMAN = "needs_human"  # banned / verification: retrying is harmful
OUTCOME_EXHAUSTED = "exhausted"      # out of retries; a person should look
OUTCOME_UNRESOLVED = "unresolved"    # can't identify the clip or the account
OUTCOME_BLOCKED = "blocked"          # the ledger says this clip may be live
OUTCOME_CLIP_SPENT = "clip_spent"    # this clip has had all the sends it gets
OUTCOME_NO_VARIANT = "no_variant"    # nothing fresh to retry with; won't re-send


def retry_delay_seconds(retry_count: int,
                        base: float = RETRY_BACKOFF_BASE_SECONDS,
                        cap: float = RETRY_BACKOFF_MAX_SECONDS) -> float:
    """Exponential backoff for the n-th retry of a row, capped.

    Exponential rather than fixed because the failures that repeat are the ones
    worth spacing out: hammering a device that is mid-reboot every 15 minutes
    just burns the row's remaining attempts on the same broken state.
    """
    try:
        retry = max(0, int(retry_count))
    except (TypeError, ValueError):
        retry = 0
    # 2**retry grows fast; clamp the exponent too so a hand-edited Retry Count of
    # 40 cannot overflow into a nonsense timestamp before the cap is applied.
    return min(float(cap), float(base) * (2 ** min(retry, 16)))


def _row_verdict(fields: dict, max_retries: int = DEFAULT_MAX_RETRIES):
    """The part of the ruling that needs only the Airtable row. Returns
    (outcome, detail) for a row that must not be retried, or None when the row
    passes and the ledger still has to be consulted.

    Split out so the pass can skip hashing a video for a row it was never going
    to retry anyway.
    """
    status = at._select_name(fields.get(at.F_PQ_POST_STATUS))
    if status != at.POST_STATUS_FAILED:
        return (OUTCOME_NOT_FAILED, f"post status is {status or 'empty'}, not Failed")

    issue = at._select_name(fields.get(at.F_PQ_ISSUE_TYPE))
    if issue != at.ISSUE_NEEDS_RETRY:
        # Only "Failed - Needs Retry" is a machine-fixable failure. A ban or a
        # verification prompt is a fact about the *account*, and posting into it
        # again does not fix it -- at best the attempt is wasted, at worst it
        # confirms to Instagram that the account is automated. Issue Type is also
        # how a human parks a row deliberately; retrying that would undo them.
        return (OUTCOME_NEEDS_HUMAN,
                f"issue type is {issue or 'empty'} -- not a retryable failure")

    try:
        retry = int(fields.get(at.F_PQ_RETRY_COUNT) or 0)
    except (TypeError, ValueError):
        retry = 0
    if retry >= max_retries:
        return (OUTCOME_EXHAUSTED,
                f"retry count {retry} has reached the limit of {max_retries}")
    return None


def decide_retry(fields: dict, profile_id: str, media_hash: str, ledger_record,
                 max_retries: int = DEFAULT_MAX_RETRIES, share_attempts: int = 0,
                 max_share_attempts: int = post_ledger.MAX_SHARE_ATTEMPTS) -> tuple:
    """Rule on one failed row. Returns (outcome, detail).

    `ledger_record` is the post_ledger record for (profile_id, media_hash), or
    None when this machine has never sent this clip to this account.

    The order is deliberate: the cheap row rules first, then "do we even know
    what we would be re-sending?", and the ledger last -- because the ledger is
    the only check whose answer can be "this reel might already be live", and it
    is worth nothing if it is consulted with a profile id we guessed at.
    """
    verdict = _row_verdict(fields, max_retries)
    if verdict is not None:
        return verdict

    if not profile_id:
        # Without the launch id there is no ledger key, so we cannot tell a clip
        # that never posted from one that did. Airtable's word alone is not
        # enough to re-send on.
        return (OUTCOME_UNRESOLVED,
                "could not resolve the target profile's MLX API ID; cannot check the ledger")
    if not media_hash:
        return (OUTCOME_UNRESOLVED,
                "could not fingerprint the spoofed video; cannot check the ledger")

    if ledger_record is not None and ledger_record.blocks_repost():
        # `shared` lands here as well as `confirmed`, and that is the whole
        # point: an unresolved share means Share was tapped and nobody ever
        # proved the outcome either way. Only a `disproved` record -- positive
        # evidence the reel is not on the account, which the deferred recheck
        # writes -- clears this.
        return (OUTCOME_BLOCKED,
                f"ledger says this clip is {ledger_record.status} on {profile_id}; "
                "re-sending it risks a double post")

    # Spent even though the ledger is clear. A disproof is evidence, not proof,
    # and on an account whose counter never moves every disproof looks equally
    # convincing -- which is how one clip was sent 13 times. `Retry Count` cannot
    # catch that: it lives on the queue row, so a re-planned row starts from zero
    # against a clip that has already been through this.
    if share_attempts >= max_share_attempts:
        return (OUTCOME_CLIP_SPENT,
                f"this clip has already been sent {share_attempts} time(s) to {profile_id}, "
                f"the limit is {max_share_attempts}")

    return (OUTCOME_RETRY, "retryable failure with a clear ledger")


def _first_link(fields: dict, key: str):
    links = fields.get(key) or []
    return links[0] if links else None


def resolve_profile_id(fields: dict, accounts_by_id: dict, profiles_by_recid: dict) -> str:
    """The 18-digit MLX API ID this row targets, or "" when it cannot be found.

    A queue row links either a Target Account (-> Profile -> MLX API ID) or a
    Target Profile directly; both shapes exist in the base, exactly as
    posting_planner handles them. This is the ledger's key half, so a miss must
    return "" and never a plausible-looking substitute like the record id.
    """
    profile_recid = None
    account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
    if account_id:
        account = accounts_by_id.get(account_id) or {}
        profile_recid = _first_link(account, at.F_ACC_PROFILE)
    if not profile_recid:
        profile_recid = _first_link(fields, at.F_PQ_TARGET_PROFILE)
    if not profile_recid:
        return ""
    info = profiles_by_recid.get(profile_recid) or {}
    return str(info.get("launch_id") or "")


def _profile_record_id(fields: dict, accounts_by_id: dict) -> str:
    """The Profiles (Cloning) *record* id a queue row targets.

    Distinct from :func:`resolve_profile_id`, which returns the 18-digit MLX
    launch key -- that is the ledger's key and cannot address an Airtable row.
    """
    account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
    if account_id:
        account = accounts_by_id.get(account_id) or {}
        recid = _first_link(account, at.F_ACC_PROFILE)
        if recid:
            return recid
    return _first_link(fields, at.F_PQ_TARGET_PROFILE) or ""


def _profile_issue_reason(outcome: str, fields: dict) -> str | None:
    """Which `Issue Reason` a non-retryable outcome deserves, or None to skip.

    Only outcomes a person can actually act on are flagged. `blocked` and
    `unresolved` are deliberately excluded: they mean the bot and the ledger
    disagree about what already happened, which is an operator/data question
    rather than something wrong with the profile itself.
    """
    if outcome in (OUTCOME_EXHAUSTED, OUTCOME_CLIP_SPENT):
        # A spent clip is the same fact as an exhausted row from a person's point
        # of view -- this slot is not going to post and nothing automatic will
        # change that. It usually means the account cannot post at all, which is
        # the thing worth looking at.
        return at.PROFILE_ISSUE_EXHAUSTED
    if outcome == OUTCOME_NEEDS_HUMAN:
        issue = at._select_name(fields.get(at.F_PQ_ISSUE_TYPE)) or ""
        if issue == at.ISSUE_BANNED_BLOCKED:
            return at.PROFILE_ISSUE_BANNED
        if issue == at.ISSUE_HUMAN_VERIFICATION:
            return at.PROFILE_ISSUE_VERIFICATION
        # `Other` is how a person parks a row by hand (the six duplicate rows
        # parked on 2026-08-04 use it). Flagging the profile for a row somebody
        # deliberately retired says the profile is broken when the operator was
        # just tidying up -- and it buries the profiles that really are.
        return None
    return None


def resolve_media(fields: dict, variants_by_id: dict) -> tuple:
    """(path, media_hash) for the row's Spoof Variant.

    The hash is sha256 of the file's bytes via post_ledger.media_fingerprint --
    the same function the posting flow used to write the ledger entry, because a
    lookup computed any other way would silently miss. Returns "" for the hash
    when the variant, the path or the file itself is gone; the caller treats that
    as "cannot check", never as "safe".
    """
    variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT)
    path = str((variants_by_id.get(variant_id) or {}).get("file_path") or "") if variant_id else ""
    if not path:
        return ("", "")
    if not Path(path).is_file():
        # The variant was cleaned up (retention) or the drive isn't mounted.
        # Either way there is nothing to re-post and nothing to hash.
        return (path, "")
    return (path, post_ledger.media_fingerprint(path))


def pick_replacement_variant(fields: dict, ready_variants: list, claimed_ids=None,
                             current_variant_id: str = ""):
    """A fresh Ready variant this row could carry instead, or None.

    Used when the row's own clip has already been sent. The target has to match
    exactly -- the row's Account (or, for a profile-driven row, its Profile) *and*
    its Account Slot. Slot is not a detail: a two-account phone has a separate
    encode per account, and handing a `Second` variant to a `Primary` row posts
    one account's video from the other, which is worse than not retrying at all.

    Oldest first, so a retry works through the backlog the same way the planner
    does rather than eating the freshest clip in the pool.
    """
    claimed = claimed_ids or set()
    account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
    profile_id = _first_link(fields, at.F_PQ_TARGET_PROFILE)
    row_slot = at._select_name(fields.get(at.F_PQ_ACCOUNT_SLOT)) or at.SLOT_PRIMARY
    if not account_id and not profile_id:
        return None

    def matches(variant) -> bool:
        if variant.get("id") in claimed or variant.get("id") == current_variant_id:
            return False
        if (variant.get("slot") or at.SLOT_PRIMARY) != row_slot:
            return False
        path = variant.get("file_path")
        if not path or not Path(path).is_file():
            # Retention cleaned it up or the drive isn't mounted. Swapping to a
            # video that isn't there just moves the failure one step later.
            return False
        # An account row matches on the account. A profile-driven row matches on
        # the profile, and must never fall back to the account link -- that is
        # how a profile's video ends up on somebody else's account.
        if account_id:
            return variant.get("account_id") == account_id
        return variant.get("profile_id") == profile_id

    candidates = [v for v in ready_variants or [] if matches(v)]
    if not candidates:
        return None
    # `created` is an Airtable date string; missing sorts first, which is fine --
    # any deterministic order beats whatever order the API happened to return.
    candidates.sort(key=lambda v: str(v.get("created") or ""))
    return candidates[0]


def _iso_at(epoch_seconds: float) -> str:
    """UTC ISO stamp for Scheduled DateTime, matching what the client writes."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).replace(microsecond=0).isoformat()


def retry_failed_posts(airtable, ledger=None, logger=None, now=time.time,
                       max_retries: int = DEFAULT_MAX_RETRIES,
                       dry_run: bool = True) -> dict:
    """Work through every Failed Posting Queue row and re-queue the safe ones.

    A row is re-queued (Post Status=Pending, Scheduled DateTime=now+backoff) when
    all of these hold:
      - Post Status is Failed
      - Issue Type is "Failed - Needs Retry" (never Banned / Blocked or Human
        Verification Required)
      - Retry Count < `max_retries`
      - the target profile and the variant's video both resolve
      - the post ledger holds no record that blocks the repost
      - the clip has not already used up its `MAX_SHARE_ATTEMPTS`
      - and, if the clip was already sent once, a fresh Ready variant exists for
        the row's target to carry instead of it

    That last rule is what keeps a retry from being a duplicate. A clip that has
    been sent is never sent again: the retry swaps in a different video, so if the
    original does publish late the account has posted two different reels rather
    than the same one twice. No replacement available means no retry.

    `dry_run` defaults to True, like every other write pass here: the caller opts
    in to touching the base.

    Returns a tally: {'considered', 'requeued', 'needs_human', 'exhausted',
    'unresolved', 'blocked', 'clip_spent', 'no_variant', 'errors'}.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level, logger.info)(message, *args)

    store = ledger or post_ledger.PostLedger()
    tally = {"considered": 0, "requeued": 0, "needs_human": 0, "exhausted": 0,
             "unresolved": 0, "blocked": 0, "errors": 0,
             "clip_spent": 0, "no_variant": 0}
    # Variants handed out in this pass, so two rows for the same account cannot be
    # pointed at the same replacement clip.
    claimed_variant_ids: set = set()

    try:
        rows = airtable.list_failed_posts()
        accounts_by_id = airtable.accounts_by_id()
        profiles_by_recid = airtable.profile_launch_map()
        variants_by_id = airtable.variants_by_id()
        ready_variants = airtable.list_ready_variants()
    except Exception as exc:
        # An Airtable outage must not look like "nothing to retry" to the caller,
        # but it also must not stop the loop that called us.
        log("warning", "Could not read the failed posts to retry: %s", exc)
        return tally

    for row in rows or []:
        queue_id = row.get("id")
        fields = row.get("fields", {}) or {}
        name = str(fields.get(at.F_PQ_NAME) or queue_id or "")
        tally["considered"] += 1

        # Resolve the ledger key only for rows that survived the row-level rules.
        # Hashing a video is real work, and a banned row is never going out again
        # whatever the ledger says.
        if _row_verdict(fields, max_retries) is None:
            profile_id = resolve_profile_id(fields, accounts_by_id, profiles_by_recid)
            media_path, media_hash = resolve_media(fields, variants_by_id)
            record = store.lookup(profile_id, media_hash) if (profile_id and media_hash) else None
            attempts = (store.share_attempts(profile_id, media_hash)
                        if (profile_id and media_hash) else 0)
        else:
            profile_id, media_path, media_hash, record = "", "", "", None
            attempts = 0

        try:
            retry = int(fields.get(at.F_PQ_RETRY_COUNT) or 0)
        except (TypeError, ValueError):
            retry = 0

        outcome, detail = decide_retry(fields, profile_id, media_hash, record,
                                       max_retries=max_retries, share_attempts=attempts)

        if outcome != OUTCOME_RETRY:
            if outcome == OUTCOME_NOT_FAILED:
                # list_failed_posts filters on Failed, so this only shows up when
                # a caller hands us its own rows. Not worth a tally bucket.
                tally["considered"] -= 1
                continue
            if outcome == OUTCOME_UNRESOLVED and media_path:
                detail = f"{detail} (variant file {media_path})"
            # A blocked or unresolved row is worth a person's attention: the
            # first means a post may have gone out unrecorded, the second that
            # the base and the disk disagree. The other two are routine.
            level = ("warning" if outcome in (OUTCOME_BLOCKED, OUTCOME_UNRESOLVED,
                                              OUTCOME_CLIP_SPENT, OUTCOME_NO_VARIANT)
                     else "info")
            log(level, "Not retrying %s: %s", name, detail)
            tally[outcome] += 1  # the outcome constants are the tally's keys

            # Surface it to a person. Until now "exhausted" and "needs_human"
            # existed only as a number in this pass's log line, so a profile the
            # bot had permanently given up on looked identical in Airtable to one
            # still being retried -- its row even kept Issue Type
            # "Failed - Needs Retry", which says the opposite of what is true.
            reason = _profile_issue_reason(outcome, fields)
            if reason and not dry_run:
                profile_recid = _profile_record_id(fields, accounts_by_id)
                if profile_recid:
                    note = f"{name} -- {detail}" if detail else name
                    try:
                        airtable.flag_profile_for_human(profile_recid, reason, note)
                        # Not "flagged": the write is skipped when this exact
                        # problem is already recorded, which is the common case
                        # once a profile has been sitting flagged for a while.
                        log("info", "Profile needs a human for %s: %s", name, reason)
                    except Exception as exc:
                        log("warning", "Could not flag profile for %s: %s", name, exc)
                else:
                    log("warning", "No profile to flag for %s (%s)", name, reason)
                if outcome in (OUTCOME_EXHAUSTED, OUTCOME_CLIP_SPENT):
                    # Stop the row advertising a retry that will never come.
                    # Without this a spent clip is re-considered, and re-warned
                    # about, on every pass forever.
                    try:
                        airtable.mark_post_retries_exhausted(queue_id)
                    except Exception as exc:
                        log("warning", "Could not mark %s exhausted: %s", name, exc)
            continue

        # If this clip was never actually sent -- no ledger record, so the run died
        # before Share (a failed adb push, a phone that never booted) -- it is
        # still untouched and re-sending it is exactly right. Only a clip that
        # *was* sent needs replacing, and then it needs replacing absolutely: the
        # disproof that let it get this far is evidence, not proof, and a reel that
        # publishes late would land beside its own retry.
        old_variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT) or ""
        replacement = None
        if record is not None:
            replacement = pick_replacement_variant(
                fields, ready_variants, claimed_ids=claimed_variant_ids,
                current_variant_id=old_variant_id)
            if replacement is None:
                # Deliberately a dead end rather than a re-send. Losing one slot
                # is cheap; the same reel twice on a client's account is not.
                log("warning",
                    "Not retrying %s: this clip was already sent to %s and there is no fresh "
                    "Ready variant for its target to replace it with", name, profile_id)
                tally["no_variant"] += 1
                continue

        delay = retry_delay_seconds(retry)
        due = _iso_at(now() + delay)
        if replacement is not None:
            note = (f"auto-retry {retry + 1}/{max_retries} scheduled for {due} with a "
                    f"replacement clip (the original was already sent to {profile_id} and "
                    f"may yet publish)")
        else:
            note = (f"auto-retry {retry + 1}/{max_retries} scheduled for {due} "
                    f"(never sent; ledger clear for {profile_id})")
        log("info", "%sRe-queueing %s in %.0f min (attempt %s/%s)%s",
            "[DRY-RUN] " if dry_run else "", name, delay / 60, retry + 1, max_retries,
            f" with replacement variant {replacement['id']}" if replacement else "")

        if dry_run:
            tally["requeued"] += 1
            continue
        try:
            if airtable.requeue_post(queue_id, due, note=note,
                                     variant_id=(replacement or {}).get("id")):
                tally["requeued"] += 1
                if replacement is not None:
                    claimed_variant_ids.add(replacement["id"])
                    # The clip we just swapped away from has been sent, whatever
                    # Airtable thinks of it. Leaving it Ready would let the planner
                    # hand it to a future slot, which the ledger would then have to
                    # block -- a launch and a boot spent to post nothing.
                    if old_variant_id:
                        try:
                            airtable.mark_variant_used(old_variant_id)
                        except Exception as exc:
                            log("warning", "Could not retire the sent variant %s for %s: %s",
                                old_variant_id, name, exc)
            else:
                tally["errors"] += 1
        except Exception as exc:
            log("warning", "Failed to re-queue %s: %s", name, exc)
            tally["errors"] += 1

    log("info", "Retry pass%s: considered=%s requeued=%s blocked=%s "
        "needs_human=%s exhausted=%s unresolved=%s clip_spent=%s no_variant=%s errors=%s",
        " [DRY-RUN]" if dry_run else "", tally["considered"], tally["requeued"],
        tally["blocked"], tally["needs_human"], tally["exhausted"],
        tally["unresolved"], tally["clip_spent"], tally["no_variant"], tally["errors"])
    return tally
