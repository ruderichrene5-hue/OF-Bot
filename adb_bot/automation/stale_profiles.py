"""Profiles that are still trying to post and no longer landing anything.

The retry pass judges a *row*: this one used its three retries, that one is
banned, park them. It cannot see the shape that only shows up across rows --
a profile whose every post fails, or shares and never confirms, for a day at a
time. Each row looks individually retryable, the loop keeps handing them back,
and the account quietly stops posting while every number on the page says the
fleet is busy.

So this asks a profile-level question instead: **has anything actually landed
on this account lately?** No confirmed post in `STALE_AFTER_SECONDS`, while
attempts are still being made, means whatever is wrong is not something another
retry will fix -- a login wall, a checkpoint, a shadowban, a phone that connects
but cannot upload. All of those need a person to open the phone and try a post
by hand, which is exactly what the flag asks for.

Flagging is the whole intervention. `Needs Human Check` already parks a profile
(the posting planner skips it, `Skipping post X: profile needs a human check`),
already shows up on the dashboard's Profiles tab, and is already cleared by a
person to mean "I looked". `recovery_runner` then hands the dead rows back.
Nothing new is invented here; this only decides *when* to pull that lever.

What it deliberately does not do:

* **It does not flag a quiet profile.** No attempts means nothing is wrong --
  a profile with an empty queue, or one parked by a person, is not failing.
  Without this the first quiet weekend would flag the entire fleet.
* **It does not treat "shared" as success.** An unconfirmed post is the single
  most common symptom of the thing this is looking for: Share is tapped, the
  reel never appears, and the ledger holds `shared` forever. Counting those as
  landed would blind it to its main case.
* **It does not re-flag or un-flag.** A profile already carrying the checkbox
  is left exactly as it is, so a person's notes are never overwritten, and
  clearing it stays the human's signal alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from adb_bot.automation import post_ledger
from adb_bot.clients import airtable as at

#: No confirmed post in this long, while still attempting, means a person has
#: to look. A day: the fleet posts several times a day per account, so 24h of
#: nothing is well past noise, and a profile genuinely mid-outage is not helped
#: by a fourth automated retry.
STALE_AFTER_SECONDS = 24 * 3600

#: How far back an attempt still counts as "this profile is trying". Wider than
#: the staleness window on purpose: a profile whose last attempt was yesterday
#: evening and has had nothing scheduled since is still stuck, and dropping it
#: the moment the window slid past would un-flag it by silence.
ATTEMPT_WINDOW_SECONDS = 3 * 24 * 3600

#: Queue statuses that count as "tried and did not land".
FAILED_STATUSES = (at.POST_STATUS_FAILED, at.POST_STATUS_VERIFYING)


@dataclass
class StaleProfile:
    """One profile that needs a person, and the evidence for saying so."""

    record_id: str
    name: str
    launch_id: str
    last_success: float = 0.0     # epoch seconds; 0.0 = nothing ever confirmed
    failed: int = 0
    uncertain: int = 0

    @property
    def attempts(self) -> int:
        return self.failed + self.uncertain

    def stale_seconds(self, now: float) -> float:
        return max(0.0, now - self.last_success) if self.last_success else 0.0

    def note(self, now: float) -> str:
        """The line written into `Issue Notes` -- what was seen, and what to do.

        The instruction belongs here rather than in a runbook: this text is the
        whole of what the person who opens Airtable at 8 a.m. is given.
        """
        if self.last_success:
            hours = self.stale_seconds(now) / 3600.0
            seen = f"no confirmed post in {hours:.0f}h"
        else:
            seen = "no confirmed post on record"
        tried = []
        if self.failed:
            tried.append(f"{self.failed} failed")
        if self.uncertain:
            tried.append(f"{self.uncertain} unconfirmed")
        return (f"{seen}, with {' and '.join(tried)} attempt(s) since. "
                f"Post one reel from this phone by hand and wait for it to appear "
                f"on the profile. If it posts cleanly, clear Needs Human Check and "
                f"the bot picks the account back up; if it does not, the account "
                f"itself needs fixing before any retry will help.")


def _epoch(value) -> float:
    """An Airtable ISO stamp as epoch seconds; 0.0 when unusable."""
    if not value:
        return 0.0
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def ledger_activity(records, now: float) -> tuple:
    """``(last_success, uncertain)`` per profile id, read from the post ledger.

    The ledger is the honest source for "did a post land": it is written the
    instant Share is tapped and resolved by the deferred recheck, so it knows
    the difference between confirmed, still-unproven and disproved -- which is
    the distinction this whole module turns on. Airtable's queue row only ever
    learns the summary.
    """
    last_success: dict = {}
    uncertain: dict = {}
    for record in records or []:
        pid = str(getattr(record, "profile_id", "") or "")
        if not pid:
            continue
        status = getattr(record, "status", "")
        when = float(getattr(record, "resolved_at", 0.0) or
                     getattr(record, "shared_at", 0.0) or 0.0)
        if status == post_ledger.STATUS_CONFIRMED:
            last_success[pid] = max(last_success.get(pid, 0.0), when)
        elif status in (post_ledger.STATUS_SHARED, post_ledger.STATUS_DISPROVED):
            if now - when <= ATTEMPT_WINDOW_SECONDS:
                uncertain[pid] = uncertain.get(pid, 0) + 1
    return last_success, uncertain


def queue_activity(rows, profiles_by_recid, now: float) -> tuple:
    """``(last_success, failed)`` per launch id, read from the Posting Queue.

    Needed alongside the ledger because a post that never reached Share leaves
    no ledger entry at all -- a profile whose every launch 500s, or whose phone
    never comes up, is invisible there while being exactly the case worth
    flagging.

    A `Posted` row's own timestamp is its *scheduled* time, not when it landed.
    That is a lower bound and it is used as one: it can only ever make a
    profile look more recently successful than it was, so the failure mode is a
    flag not raised rather than a profile parked on bad evidence.
    """
    last_success: dict = {}
    failed: dict = {}
    for record in rows or []:
        fields = record.get("fields", {}) or {}
        links = fields.get(at.F_PQ_TARGET_PROFILE) or []
        launch_id = (profiles_by_recid.get(links[0]) or {}).get("launch_id") if links else None
        if not launch_id:
            continue
        launch_id = str(launch_id)
        status = at._select_name(fields.get(at.F_PQ_POST_STATUS))
        when = _epoch(fields.get(at.F_PQ_SCHEDULED))
        if status == at.POST_STATUS_POSTED:
            last_success[launch_id] = max(last_success.get(launch_id, 0.0), when)
        elif status in FAILED_STATUSES and now - when <= ATTEMPT_WINDOW_SECONDS:
            failed[launch_id] = failed.get(launch_id, 0) + 1
    return last_success, failed


def find_stale_profiles(profiles, queue_rows, ledger_records, now: float,
                        stale_after: float = STALE_AFTER_SECONDS) -> list:
    """Profiles still attempting posts with nothing confirmed in `stale_after`.

    Pure: everything it reads is passed in, so the rule is testable without a
    base, a ledger file or a phone.
    """
    by_recid = {p["record_id"]: p for p in profiles or [] if p.get("record_id")}
    ledger_success, uncertain = ledger_activity(ledger_records, now)
    queue_success, failed = queue_activity(queue_rows, by_recid, now)

    stale: list = []
    for profile in profiles or []:
        launch_id = str(profile.get("launch_id") or "")
        if not launch_id:
            continue                      # nothing can launch it; not its fault
        # Gate order matters and mirrors the planners': a parked profile is not
        # failing, and an already-flagged one is somebody's open worklist item.
        if profile.get("status") != at.STATUS_SELECT_ACTIVE:
            continue
        if profile.get("needs_human"):
            continue

        entry = StaleProfile(
            record_id=profile["record_id"],
            name=profile.get("name") or launch_id,
            launch_id=launch_id,
            last_success=max(ledger_success.get(launch_id, 0.0),
                             queue_success.get(launch_id, 0.0)),
            failed=failed.get(launch_id, 0),
            uncertain=uncertain.get(launch_id, 0),
        )
        if not entry.attempts:
            continue                      # quiet, not broken
        if entry.last_success and (now - entry.last_success) <= stale_after:
            continue                      # something landed recently enough
        stale.append(entry)

    # Longest silent first: "never posted at all" outranks "quiet since
    # yesterday", and that is the order a worklist should be worked in.
    stale.sort(key=lambda e: (e.last_success or 0.0, e.name.lower()))
    return stale


def flag_stale_profiles(airtable, ledger=None, now: float = 0.0,
                        stale_after: float = STALE_AFTER_SECONDS,
                        dry_run: bool = False, logger=None) -> dict:
    """Find them and set `Needs Human Check`. Returns a tally for the log."""
    import time as _time

    now = now or _time.time()
    tally = {"considered": 0, "stale": 0, "flagged": 0, "errors": 0}

    def log(level: str, message: str, *args) -> None:
        if logger is not None:
            getattr(logger, level)(message, *args)

    try:
        profiles = airtable.posting_profiles()
        queue_rows = airtable.list_queue_rows()
    except Exception as exc:
        log("warning", "Could not check for stale profiles: %s", exc)
        tally["errors"] += 1
        return tally

    ledger = ledger if ledger is not None else post_ledger.PostLedger()
    try:
        records = list(ledger.load().values())
    except Exception:
        records = []                      # no ledger is not a reason to skip

    tally["considered"] = len(profiles)
    stale = find_stale_profiles(profiles, queue_rows, records, now, stale_after)
    tally["stale"] = len(stale)

    for entry in stale:
        if dry_run:
            log("info", "[DRY-RUN] would flag %s: %s", entry.name, entry.note(now))
            continue
        try:
            airtable.flag_profile_for_human(
                entry.record_id, at.PROFILE_ISSUE_NO_SUCCESS, entry.note(now))
            tally["flagged"] += 1
            log("warning", "Profile %s needs a human: %s", entry.name, entry.note(now))
        except Exception as exc:
            tally["errors"] += 1
            log("warning", "Could not flag %s: %s", entry.name, exc)
    return tally
