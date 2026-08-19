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

`decide_retry` is pure and takes the already-resolved facts, so the whole ruling
is unit tested without Airtable, without a device and without a real video.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.clients import airtable as at
from adb_bot.automation import post_ledger
from adb_bot.automation.recheck_runner import disprove_from_later_share

# How many attempts a row gets before it stops being retried automatically.
# The posting runner bumps Retry Count on each failed attempt, so this counts
# real attempts: 5 means the row is tried up to five times and then waits for a
# person. A row that has failed five times is not failing by chance.
#
# Raised from 3 to 5 on 2026-08-11: three attempts was sending rows to the
# needs-human worklist that a fourth or fifth try would have cleared on its own,
# because the failures that repeat here are usually the phone (MLX slow to
# launch, ADB dropping, a device mid-reboot) rather than the account. A person
# should only be asked once the phone has genuinely refused five times.
DEFAULT_MAX_RETRIES = 5

# Backoff: 15min * 2**retry_count, capped. 15 minutes is the same settling time
# the deferred recheck uses -- long enough that whatever transient broke the run
# (phone rebooting, MLX restarting, Instagram throttling a device) has had a
# chance to clear, short enough to still land inside the same posting window.
# The cap is what keeps the tail of the ladder from drifting into the middle of
# the night: with five attempts the waits are 15m/30m/1h/2h, so the last try
# lands under 4h after the first failure and stays inside a normal day.
RETRY_BACKOFF_BASE_SECONDS = 15 * 60
RETRY_BACKOFF_MAX_SECONDS = 4 * 3600

OUTCOME_RETRY = "retry"              # eligible; goes back to Pending
OUTCOME_NOT_FAILED = "not_failed"    # not a Failed row at all -- nothing to do
OUTCOME_NEEDS_HUMAN = "needs_human"  # banned / verification: retrying is harmful
OUTCOME_EXHAUSTED = "exhausted"      # out of retries; a person should look
OUTCOME_UNRESOLVED = "unresolved"    # can't identify the clip or the account
OUTCOME_BLOCKED = "blocked"          # the ledger says this clip may be live

# How long a blocked row is given to acquire the evidence that would settle it
# before its clip is written off. Matches the deferred recheck's own write-off
# window, because it is the same question being given up on.
#
# The evidence a blocked row needs is the *next* share on the same account --
# and for a profile whose every row is blocked, that share can never happen.
# Emely 4, 5, 7 and 12 were each sitting on one Failed row apiece, holding the
# only Ready variant their profile had, with nothing Pending behind it: no
# further post could ever be made, so no later count could ever be read, so the
# row could never be settled and the profile could never post again. Waiting
# longer does not break that circle; releasing the clip does.
WRITE_OFF_SECONDS = 24 * 3600


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
                 max_retries: int = DEFAULT_MAX_RETRIES) -> tuple:
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
    if outcome == OUTCOME_EXHAUSTED:
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

    `dry_run` defaults to True, like every other write pass here: the caller opts
    in to touching the base.

    Returns a tally: {'considered', 'requeued', 'needs_human', 'exhausted',
    'unresolved', 'blocked', 'errors'}.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level, logger.info)(message, *args)

    store = ledger or post_ledger.PostLedger()
    # Read once, not per row: the counter evidence below compares a blocked
    # row's share against every other share on the same account, and the ledger
    # is a file on disk.
    all_shares = list(store.load().values())
    tally = {"considered": 0, "requeued": 0, "needs_human": 0, "exhausted": 0,
             "unresolved": 0, "blocked": 0, "unblocked": 0,
             "written_off": 0, "closed_as_posted": 0, "errors": 0}

    try:
        rows = airtable.list_failed_posts()
        accounts_by_id = airtable.accounts_by_id()
        profiles_by_recid = airtable.profile_launch_map()
        variants_by_id = airtable.variants_by_id()
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
        else:
            profile_id, media_path, media_hash, record = "", "", "", None

        try:
            retry = int(fields.get(at.F_PQ_RETRY_COUNT) or 0)
        except (TypeError, ValueError):
            retry = 0

        outcome, detail = decide_retry(fields, profile_id, media_hash, record,
                                       max_retries=max_retries)

        # A blocked row is not necessarily a stuck one. `blocked` means the
        # ledger still calls this clip `shared`: Share was tapped and nobody
        # ever proved the outcome. The deferred recheck is what normally
        # settles that, but it only ever looks at rows Airtable has in
        # `Verifying` -- so a run that died *after* Share left the row `Failed`
        # and put it somewhere nothing would ever read it again. The ledger
        # kept blocking, this pass kept refusing, the row kept holding its
        # Ready variant, and the queue kept reporting the profile as having
        # nothing to post. That is the `No Recent Success` deadlock: on
        # 2026-08-19, 119 unresolved shares across 49 profiles, and profiles
        # refused here 720 times each without one of them ever being asked the
        # one question that could free it.
        #
        # The proof is usually already on disk and needs no phone: every share
        # records the post count read moments before Share, so the next share
        # on the same account is a second reading of the same counter. If it
        # is unchanged, nothing landed in between and the clip is safe to send
        # again.
        #
        # No device is launched and nothing new is spent. When the evidence
        # does not reach, `disprove_from_later_share` returns None and the row
        # stays blocked exactly as before.
        if outcome == OUTCOME_BLOCKED and profile_id and media_hash and record is not None:
            settled = disprove_from_later_share(record, all_shares)
            if settled is not None:
                _, why = settled
                log("info", "Unblocking %s: %s", name, why)
                if not dry_run:
                    store.resolve(profile_id, media_hash,
                                  post_ledger.STATUS_DISPROVED, why)
                outcome, detail = OUTCOME_RETRY, f"disproved with no device -- {why}"
                tally["unblocked"] += 1
            elif record.status == post_ledger.STATUS_CONFIRMED:
                # The ledger already proved this one landed; the row just never
                # heard. Marking it Failed would be false, and it matters beyond
                # tidiness: `stale_profiles` flags a profile on its count of
                # *confirmed* posts in Airtable, so a post that went out and was
                # recorded only on disk still reads as "No Recent Success" and
                # parks the profile. Close the row honestly instead.
                log("info", "Closing %s as posted: the ledger confirmed this clip "
                            "on %s and the row never heard", name, profile_id)
                if not dry_run:
                    airtable.mark_post_result(queue_id, at.POST_STATUS_POSTED,
                                              at.ISSUE_NONE)
                    variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT)
                    if variant_id:
                        airtable.mark_variant_used(variant_id)
                outcome = OUTCOME_BLOCKED
                tally["closed_as_posted"] += 1
            elif (now() - (record.shared_at or 0.0)) > WRITE_OFF_SECONDS:
                # No evidence has arrived in a day and none can now: this row is
                # holding the profile's only Ready variant, and a variant only
                # becomes `Used` on a *successful* post, so a Failed row holds
                # its clip for ever. `queue_runner` then reports the profile as
                # having Ready content it may not touch and queues nothing, and
                # the pipeline keeps spoofing clips nobody can post.
                #
                # Burning the clip is the one move that frees the profile
                # without re-sending anything. The reel may well be live -- that
                # is exactly why it is marked `Used` rather than retried -- but
                # the *next* one no longer has to wait behind it.
                #
                # The deferred recheck already writes rows off after ~26h and
                # leaves the variant linked, which is why `Viktoria 10` was
                # abandoned on 2026-08-18 and still had not posted a day later.
                variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT)
                log("warning",
                    "Writing off %s: %s -- unproven for %.1fh and no later post can "
                    "settle it; burning the clip so the profile can post again",
                    name, detail, (now() - (record.shared_at or 0.0)) / 3600.0)
                if not dry_run:
                    airtable.mark_post_result(queue_id, at.POST_STATUS_FAILED,
                                              at.ISSUE_OTHER)
                    if variant_id:
                        airtable.mark_variant_used(variant_id)
                outcome = OUTCOME_BLOCKED
                tally["written_off"] += 1

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
            level = "warning" if outcome in (OUTCOME_BLOCKED, OUTCOME_UNRESOLVED) else "info"
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
                if outcome == OUTCOME_EXHAUSTED:
                    # Stop the row advertising a retry that will never come.
                    try:
                        airtable.mark_post_retries_exhausted(queue_id)
                    except Exception as exc:
                        log("warning", "Could not mark %s exhausted: %s", name, exc)
            continue

        delay = retry_delay_seconds(retry)
        due = _iso_at(now() + delay)
        note = (f"auto-retry {retry + 1}/{max_retries} scheduled for {due} "
                f"(ledger clear for {profile_id})")
        log("info", "%sRe-queueing %s in %.0f min (attempt %s/%s)",
            "[DRY-RUN] " if dry_run else "", name, delay / 60, retry + 1, max_retries)

        if dry_run:
            tally["requeued"] += 1
            continue
        try:
            if airtable.requeue_post(queue_id, due, note=note):
                tally["requeued"] += 1
            else:
                tally["errors"] += 1
        except Exception as exc:
            log("warning", "Failed to re-queue %s: %s", name, exc)
            tally["errors"] += 1

    log("info", "Retry pass%s: considered=%s requeued=%s blocked=%s "
        "needs_human=%s exhausted=%s unresolved=%s errors=%s",
        " [DRY-RUN]" if dry_run else "", tally["considered"], tally["requeued"],
        tally["blocked"], tally["needs_human"], tally["exhausted"],
        tally["unresolved"], tally["errors"])
    return tally
