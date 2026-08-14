"""Resume a profile once a person has un-flagged it.

The bot flags a profile it has given up on (`Profiles (Cloning).Needs Human
Check`) and never clears it -- clearing is how somebody says "I looked, it is
fixed". Until now that said nothing to the bot: no loop read the checkbox, so
clearing it changed nothing at all. The profile's posts stayed dead, because
what actually stopped them is on the *queue rows*, not the profile:

    Post Status = Failed, Issue Type = Retries Exhausted, Retry Count = 3

and `retry_runner` only re-queues a row whose Issue Type is exactly
`Failed - Needs Retry` with a count below the limit. Those rows are also what
holds the clips: `Failed` is in the queue loop's `VARIANT_HELD_BY` set, on
purpose, so one clip has one owner. Leave them and the variants stay locked to a
row nobody will ever act on, and the profile is quietly finished even though a
person believes they have fixed it.

This pass closes that loop. It does not decide that a post may go out -- it
hands the row back to `retry_runner`, which applies the ledger check that keeps
a reel from going out twice. All this does is make a dead row eligible to be
*considered* again.

Two things it deliberately does not do:

* **It does not set Status back to Active.** Status is the park switch a person
  owns; a profile that is both flagged and parked was parked by somebody, and
  un-parking it because a different checkbox changed would override them. A
  recovered profile that is still Inactive is reported, not resurrected.
* **It does not re-tick the checkbox.** The person cleared it; writing it back
  would be the bot arguing with them.

The planning half is pure, so which rows count as dead is testable without
Airtable, a device or a real video.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adb_bot.automation.posting_planner import _first_link
from adb_bot.automation.retry_runner import DEFAULT_MAX_RETRIES
from adb_bot.clients import airtable as at


@dataclass
class RecoveredProfile:
    """One un-flagged profile and the rows this pass hands back to retry."""
    record_id: str
    name: str
    status: str | None = None
    reason: str | None = None
    row_ids: list = field(default_factory=list)

    @property
    def parked(self) -> bool:
        """Un-flagged, but still switched off by hand -- it will not run."""
        return self.status is not None and self.status != at.STATUS_SELECT_ACTIVE


@dataclass
class RecoveryReport:
    recovered: list = field(default_factory=list)   # RecoveredProfile
    rows_reset: int = 0
    profiles_cleared: int = 0
    errors: list = field(default_factory=list)      # (name, message)
    dry_run: bool = True

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        parked = sum(1 for p in self.recovered if p.parked)
        return (f"[{mode}] profiles={len(self.recovered)} rows={self.rows_reset} "
                f"cleared={self.profiles_cleared} still_parked={parked} "
                f"errors={len(self.errors)}")


def is_dead_row(fields: dict, max_retries: int = DEFAULT_MAX_RETRIES) -> bool:
    """True for a Failed row the retry pass will never pick up again.

    The mirror image of `retry_runner._row_verdict`: a row is dead when it has
    failed and either its Issue Type is not the retryable one, or it has used up
    its attempts. Anything the retry pass would still act on is left alone --
    resetting a row mid-ladder would hand it a fresh set of attempts it has not
    earned.

    Rows whose Issue Type says a *person* owns the outcome are still included:
    `Banned / Blocked` and `Human Verification Required` are exactly what gets
    fixed by hand, and clearing the flag is the person saying they fixed it.
    """
    if at._select_name(fields.get(at.F_PQ_POST_STATUS)) != at.POST_STATUS_FAILED:
        return False
    issue = at._select_name(fields.get(at.F_PQ_ISSUE_TYPE))
    try:
        retry = int(fields.get(at.F_PQ_RETRY_COUNT) or 0)
    except (TypeError, ValueError):
        retry = 0
    return issue != at.ISSUE_NEEDS_RETRY or retry >= max_retries


def rows_for_profile(queue_rows, profile_id: str, accounts_by_profile=None) -> list:
    """The queue rows belonging to one profile, by either targeting path.

    A row points at a profile directly (`Target Profile`) or at an Accounts row
    whose `Profile` link is this profile -- the posting planner accepts both, so
    recovery has to see both or an account-driven profile recovers nothing.
    """
    accounts = (accounts_by_profile or {}).get(profile_id) or set()
    out = []
    for row in queue_rows or []:
        fields = row.get("fields", {}) or {}
        if _first_link(fields, at.F_PQ_TARGET_PROFILE) == profile_id:
            out.append(row)
            continue
        account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
        if account_id and account_id in accounts:
            out.append(row)
    return out


def plan_recovery(profiles, queue_rows, accounts_by_profile=None,
                  max_retries: int = DEFAULT_MAX_RETRIES) -> RecoveryReport:
    """Which profiles to resume and which rows to hand back. Pure: no writes.

    - `profiles`: `AirtableClient.profiles_awaiting_recovery()` output
    - `queue_rows`: every Posting Queue row (all statuses)
    - `accounts_by_profile`: profile record_id -> {account record_ids}

    A profile with no dead rows is still "recovered": there was nothing stuck,
    so the pass just closes the issue out so it stops being reconsidered.
    """
    report = RecoveryReport()
    for profile in profiles or []:
        record_id = profile.get("record_id")
        if not record_id:
            continue
        dead = [row.get("id") for row in rows_for_profile(queue_rows, record_id, accounts_by_profile)
                if is_dead_row(row.get("fields", {}) or {}, max_retries)]
        report.recovered.append(RecoveredProfile(
            record_id=record_id,
            name=profile.get("name") or record_id,
            status=profile.get("status"),
            reason=profile.get("reason"),
            row_ids=[rid for rid in dead if rid],
        ))
    return report


def run_recovery(airtable, logger, dry_run: bool = True,
                 max_retries: int = DEFAULT_MAX_RETRIES) -> RecoveryReport:
    """Find un-flagged profiles, hand their dead rows back to the retry pass.

    Entry point for the loop. Dry-run is the default and writes nothing; it
    reports exactly what an --apply run would do.
    """
    try:
        profiles = airtable.profiles_awaiting_recovery()
        if not profiles:
            logger.info("recovery: no un-flagged profiles waiting")
            report = RecoveryReport(dry_run=dry_run)
            return report
        queue_rows = airtable.list_queue_rows()
        accounts_by_profile: dict = {}
        for account_id, fields in (airtable.accounts_by_id() or {}).items():
            profile_id = _first_link(fields, at.F_ACC_PROFILE)
            if profile_id:
                accounts_by_profile.setdefault(profile_id, set()).add(account_id)
    except Exception as exc:
        logger.error("recovery: could not read Airtable: %s", exc)
        report = RecoveryReport(dry_run=dry_run)
        report.errors.append(("<airtable>", str(exc)))
        return report

    report = plan_recovery(profiles, queue_rows, accounts_by_profile, max_retries=max_retries)
    report.dry_run = dry_run

    for profile in report.recovered:
        if profile.parked:
            # Worth saying out loud: from a person's side the profile looks
            # fixed, and it still will not run until the park switch goes back.
            logger.warning("recovery: %s is un-flagged but Status is %s -- it stays out of "
                           "every loop until that is set to %s",
                           profile.name, profile.status, at.STATUS_SELECT_ACTIVE)

        note = (f"{len(profile.row_ids)} queue row(s) handed back to the retry pass"
                if profile.row_ids else "nothing was stuck; issue closed")
        if dry_run:
            logger.info("[DRY-RUN] would recover %s (was: %s) -- %s",
                        profile.name, profile.reason or "no reason recorded", note)
            report.rows_reset += len(profile.row_ids)
            continue

        reset = 0
        for row_id in profile.row_ids:
            if airtable.reset_row_for_retry(row_id):
                reset += 1
            else:
                report.errors.append((profile.name, f"could not reset queue row {row_id}"))
        report.rows_reset += reset

        # Clear the issue last. If a row reset failed, leaving Flagged At in
        # place means the next tick tries again rather than declaring a
        # half-recovered profile finished.
        if reset == len(profile.row_ids):
            if airtable.clear_profile_issue(profile.record_id, note):
                report.profiles_cleared += 1
                logger.info("recovery: %s resumed -- %s", profile.name, note)
            else:
                report.errors.append((profile.name, "could not clear the profile's issue fields"))
        else:
            logger.warning("recovery: %s only partly reset (%d/%d rows); leaving it flagged "
                           "for the next tick", profile.name, reset, len(profile.row_ids))

    logger.info("recovery: %s", report.summary())
    return report
