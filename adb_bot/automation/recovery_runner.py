"""Bring parked profiles back — the other half of `flag_profile_for_human`.

Flagging is one-way: the retry pass parks a phone and nothing ever un-parks it.
That is right for a phone Instagram has challenged, and wrong for everything
else, because most parks are not the phone's fault. On 2026-08-06 seventeen
profiles were parked and the actual cause was one selector bug in the reel flow
(see `_NOT_THE_COMPOSER_IDS`). Making a person clear seventeen checkboxes every
time we ship a bug is backwards, and it hides the bug.

So there are two ways out of a park, and they are deliberately different:

**A person un-ticked the box.** That is their attestation that they looked. This
pass finishes the job -- Status back to Active, Issue Reason cleared -- so that
un-ticking is all they have to do. It touches no phone.

**The bot re-tests its own excuse.** For reasons that mean "our automation gave
up" rather than "this account is in trouble", the pass launches the phone, reads
its post count, and un-parks it if it answers. Verification and ban flags are
never auto-cleared: putting a challenged account back on the air is how a
recoverable challenge becomes a permanent ban, and no probe can tell us a person
has actually dealt with it.

The backoff is what keeps a genuinely dead phone from being probed forever.
Attempts are counted from the notes the pass leaves behind, so there is no new
Airtable field to add and the count survives a restart.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from adb_bot.clients import airtable as at

# Reasons that describe our automation giving up, not the account being in
# trouble. Only these are eligible for an automatic re-test.
AUTOMATION_SIDE_REASONS = frozenset({
    at.PROFILE_ISSUE_EXHAUSTED,
    at.PROFILE_ISSUE_UNREACHABLE,
    at.PROFILE_ISSUE_REPEATED,
})

# Never auto-cleared -- a person has to say they dealt with it.
HUMAN_ONLY_REASONS = frozenset({
    at.PROFILE_ISSUE_VERIFICATION,
    at.PROFILE_ISSUE_BANNED,
})

# How long after the park (or after the last failed attempt) each auto-retry
# waits. A profile that has used all of these is left alone until a person
# looks: four launches spread over four days is a fair try, and past that the
# phone is telling us something a fifth launch will not.
BACKOFF_HOURS = (6, 24, 72)

# Written into Issue Notes so the next pass can count attempts without a new
# field. Both the success and the give-up lines carry it.
ATTEMPT_MARKER = "auto-recovery probe"
CLEARED_BY_PERSON_NOTE = (
    "Un-parked: the flag was cleared by a person, so Status is back to Active "
    "and Issue Reason cleared."
)


@dataclass
class RecoveryReport:
    reactivated: list = field(default_factory=list)   # a person cleared the flag
    recovered: list = field(default_factory=list)     # probe answered, un-parked
    still_down: list = field(default_factory=list)    # probe ran, no answer
    waiting: list = field(default_factory=list)       # inside its backoff window
    needs_person: list = field(default_factory=list)  # out of attempts, or a human-only reason
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        return (f"reactivated={len(self.reactivated)} recovered={len(self.recovered)} "
                f"still_down={len(self.still_down)} waiting={len(self.waiting)} "
                f"needs_person={len(self.needs_person)} errors={len(self.errors)}")

    def as_dict(self) -> dict:
        return {"reactivated": len(self.reactivated), "recovered": len(self.recovered),
                "still_down": len(self.still_down), "waiting": len(self.waiting),
                "needs_person": len(self.needs_person), "errors": len(self.errors)}


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def attempts_so_far(notes: str) -> int:
    """How many times this pass has already probed the profile, counted off the
    notes it left. Cheaper and more durable than a field, and it reads as a
    history to whoever opens the record."""
    return str(notes or "").count(ATTEMPT_MARKER)


def due_for_probe(flagged_at, attempts: int, now: datetime) -> bool:
    """Has this profile's current backoff window elapsed?

    A profile with no `Flagged At` is treated as due: the stamp is written on
    every park, so a missing one means an old record, and refusing to ever
    retry those would quietly strand them.
    """
    if attempts >= len(BACKOFF_HOURS):
        return False
    stamp = _parse_iso(flagged_at)
    if stamp is None:
        return True
    return (now - stamp).total_seconds() >= BACKOFF_HOURS[attempts] * 3600


def plan_recovery(records: list, now: datetime | None = None) -> tuple[list, list, RecoveryReport]:
    """Split parked profiles into (to reactivate, to probe) plus a report of the
    rest. Pure -- no Airtable writes, no phones -- so the decisions are testable
    without either.
    """
    now = now or datetime.now(timezone.utc)
    report = RecoveryReport()
    to_reactivate, to_probe = [], []

    for record in records or []:
        fields = record.get("fields", {}) or {}
        name = str(fields.get(at.F_PROF_NAME) or record.get("id") or "?").strip()
        flagged = bool(fields.get(at.F_PROF_NEEDS_HUMAN))
        reason = at._select_name(fields.get(at.F_PROF_ISSUE_REASON))
        entry = {"record_id": record.get("id"), "name": name, "reason": reason}

        if not flagged:
            # A person cleared the box. Finish the job for them -- but only if
            # the bot is the one that parked it.
            #
            # `Issue Reason` and `Flagged At` are the fingerprint: the bot writes
            # both on every park and `clear_profile_flag` clears both, while a
            # person parking a profile by hand sets neither. Requiring one of
            # them is what stops this pass un-parking every deliberately-parked
            # profile on the base, the 52 Blank staging rows included.
            #
            # Issue *Notes* deliberately do not count, tempting as it is: they
            # survive an un-park on purpose (the history is the useful part), so
            # a profile a person parks months after being flagged still carries
            # them, and treating that as our mark would override the person.
            # The cost is that clearing Reason *and* Flagged At by hand leaves a
            # profile invisible here -- un-ticking the box is meant to be the
            # whole gesture, and this pass does the rest.
            #
            # A profile already Active but still carrying a reason is included
            # on purpose: the write is then only the tidy-up, and leaving the
            # reason set would make it look bot-parked forever.
            if reason or fields.get(at.F_PROF_FLAGGED_AT):
                to_reactivate.append(entry)
            continue

        if reason in HUMAN_ONLY_REASONS:
            report.needs_person.append(entry)
            continue
        if reason not in AUTOMATION_SIDE_REASONS:
            # An unrecognised reason is not something to guess about.
            report.needs_person.append(entry)
            continue

        attempts = attempts_so_far(fields.get(at.F_PROF_ISSUE_NOTES))
        if attempts >= len(BACKOFF_HOURS):
            report.needs_person.append(entry)
            continue
        if not due_for_probe(fields.get(at.F_PROF_FLAGGED_AT), attempts, now):
            report.waiting.append(entry)
            continue
        to_probe.append({**entry, "attempts": attempts,
                         "launch_id": str(fields.get(at.F_PROF_MLX_API_ID) or "").strip()})

    return to_reactivate, to_probe, report


def recover_profiles(airtable, probe=None, logger=None, dry_run: bool = True,
                     now: datetime | None = None) -> RecoveryReport:
    """Run both recovery paths.

    `probe(launch_id, name)` should launch the phone, read its post count and
    shut it down, returning something truthy when the phone answered and None
    when it did not. Leaving it None runs the reactivation pass only, which
    touches no phone -- that is what makes this loop safe to run often.
    """
    now = now or datetime.now(timezone.utc)

    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)(message, *args)

    try:
        records = airtable.list_flagged_profiles()
    except Exception as exc:
        report = RecoveryReport()
        report.errors.append(("<list>", str(exc)))
        log("error", "recovery: could not list profiles: %s", exc)
        return report

    to_reactivate, to_probe, report = plan_recovery(records, now=now)

    for entry in to_reactivate:
        log("info", "recovery: %s was un-flagged by a person -> Active, Issue Reason cleared",
            entry["name"])
        if dry_run:
            report.reactivated.append(entry)
            continue
        try:
            if airtable.clear_profile_flag(entry["record_id"], CLEARED_BY_PERSON_NOTE):
                report.reactivated.append(entry)
            else:
                report.errors.append((entry["name"], "the write failed"))
        except Exception as exc:
            report.errors.append((entry["name"], str(exc)))
            log("warning", "recovery: could not reactivate %s: %s", entry["name"], exc)

    if probe is None:
        if to_probe:
            log("info", "recovery: %s profile(s) are due for a probe; none run (no probe available)",
                len(to_probe))
            report.waiting.extend(to_probe)
        return report

    for entry in to_probe:
        attempt_no = entry["attempts"] + 1
        log("info", "recovery: probing %s (%s, attempt %s/%s)",
            entry["name"], entry["reason"], attempt_no, len(BACKOFF_HOURS))
        if dry_run:
            report.still_down.append(entry)
            continue
        if not entry["launch_id"]:
            report.errors.append((entry["name"], "no MLX API ID"))
            continue
        try:
            answered = probe(entry["launch_id"], entry["name"])
        except Exception as exc:
            answered = None
            log("warning", "recovery: probe of %s raised: %s", entry["name"], exc)

        try:
            if answered:
                note = (f"Un-parked: {ATTEMPT_MARKER} {attempt_no} reached the phone "
                        f"(read {answered}). The park was ours, not the account's.")
                if airtable.clear_profile_flag(entry["record_id"], note):
                    report.recovered.append(entry)
                    log("info", "recovery: %s answered -> back to Active", entry["name"])
                else:
                    report.errors.append((entry["name"], "the write failed"))
            else:
                remaining = len(BACKOFF_HOURS) - attempt_no
                note = (f"{ATTEMPT_MARKER} {attempt_no} could not reach the phone; "
                        + (f"{remaining} attempt(s) left." if remaining
                           else "out of attempts -- a person needs to look."))
                airtable.note_on_profile(entry["record_id"], note, restamp_flagged_at=True)
                report.still_down.append(entry)
                log("info", "recovery: %s did not answer (%s)", entry["name"], note)
        except Exception as exc:
            report.errors.append((entry["name"], str(exc)))
            log("warning", "recovery: could not record the probe of %s: %s", entry["name"], exc)

    return report
