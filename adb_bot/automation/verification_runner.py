"""Work the flagged fleet through Instagram's verification chain, unattended.

`flows/verification.py` solves *one* account when somebody points it at one.
This is what points it at them: pick the flagged profiles, take the locks the
other loops respect, drive each one, and write the answer back.

**Route on the result, not on the tag.** The `Issue` tag says "somebody or
something thinks this phone is stuck"; it does not say what is wrong, and the
five results this flow can return want five different things done:

- `solved` -> take the `Issue` tag off in MultiLogin, and nothing else. That
  one write is the whole hand-back: `issue_tags` sees the tag gone, reads it as
  a person having looked, and clears `Needs Human Check`; `recovery_runner`
  then sets Status back to Active and re-queues the rows that were stuck. Doing
  any of that here as well would be two systems writing the same state, which
  is what stranded profiles the last time (see `adbbot-unpark-stranded`).
- `banned` -> flag the profile `Banned / Blocked` and **leave the tag on**. The
  account is gone; handing it back to the posting loop would spend launches on
  a phone that can never post.
- `signed_out` -> change nothing at all. Nobody is logged in, so there was no
  challenge to answer and none was answered. This is the result that most needs
  its own name: before it existed the flow reported these as *solved*, which
  would untag a logged-out profile and hand it straight back to posting.
- `needs_human`, `stuck`, `failed` -> change nothing. The tag stays, which is
  exactly where the profile already was.

**The cool-off.** A profile this pass touches is not reconsidered for
`DEFAULT_COOLOFF_HOURS`, whatever happened to it. Two different reasons, both
about money: a *cleared* profile is eligible to post within minutes and may
re-flag itself immediately if the account is still shaky, and a profile that
failed will usually fail the same way an hour later. Without this the pass
spends three numbers per profile per tick, forever, on the same phones.

Note what the cool-off does **not** cover: it stops this pass rerunning, not
the posting loop from posting. A cleared profile still becomes postable as soon
as `recovery_runner` reactivates it (TODO_2026-08-12 §4.3 wanted a delay there
too; that belongs in the posting planner, not here).

The planning half (`plan_verification`) is pure and unit-tested. The apply half
needs MultiLogin, a phone, and real money, and is deliberately opt-in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adb_bot.automation.flows import verification
from adb_bot.clients import airtable as at

ISSUE_TAG = "Issue"

# Tags a person applies in MultiLogin that already say what is wrong -- and say
# it is not something this flow can fix. Read them: a VA who has looked at a
# phone knows more than a run that has not started yet, and launching one costs
# two minutes and possibly a rented number to rediscover what the tag says.
#
# On the live workspace these cover **14 of the 69 flagged profiles**:
#   'logged out'       x10  -- needs credentials, not verification
#   'unable to verify' x3   -- somebody already tried this and failed
#   'Banned / Dead'    x1   -- the account is gone
#
# Matched lowercased, because they are typed by hand and the casing varies.
DIAGNOSED_ELSEWHERE_TAGS = {
    "logged out",
    "unable to verify",
    "banned / dead",
    "banned/dead",
}

# How long a profile is left alone after this pass has worked it. Long enough
# that a re-flag has to be a fresh problem rather than the same one still
# settling, short enough that a genuinely fixable phone is retried the same day.
DEFAULT_COOLOFF_HOURS = 6.0

# How many profiles one pass will work. Each costs up to three rented numbers
# and about four minutes on a phone, so an unbounded pass could empty the
# wallet in a single tick -- and the wallet, unlike the breaker, stops
# everything (see `clients/sms/breaker.py` on why an empty wallet deliberately
# does not trip it).
DEFAULT_LIMIT = 5

LEDGER_FILENAME = "verification_attempts.json"

# Airtable `Issue Reason` values this pass must never work, whatever the screen
# says. Both are somebody else's finding, and the failure mode is the same: a
# suspended account goes on rendering a **cached feed**, which reads as healthy,
# which routes to `solved`, which takes the `Issue` tag off and hands a dead
# account back to the posting loop. That is exactly how `Laila 5`'s ban was
# overwritten on 2026-08-13, and it is the one mistake an unattended pass can
# make that costs more than money.
#
# `flag_review` has always filtered its population before launching anything;
# this pass never did, because it read MultiLogin's tags and MultiLogin has no
# field for "a person already decided this". Airtable does.
#
# Matched lowercased -- `Held For Supervised Run` is written with typecast on,
# so its casing is whatever first created it.
PROTECTED_REASONS = {
    at.PROFILE_ISSUE_BANNED.lower(),        # "banned / blocked"
    "held for supervised run",
}

# The Airtable reason that means a detector -- not a person, not a guess --
# saw Instagram ask for verification on this profile. Those go first: they are
# the ones this flow exists to answer, and the MultiLogin remark that used to
# order the queue says nothing at all on 34 of 70 tagged profiles.
DETECTED_VERIFICATION_REASON = at.PROFILE_ISSUE_VERIFICATION.lower()

# Hard ceiling on rented numbers per pass, independent of `--limit`. The limit
# bounds *profiles*; this bounds *money*, and they are not the same bound: one
# profile can spend three numbers, so `--limit 5` is a 15-number worst case.
# Checked between profiles, so a pass stops at the first profile boundary after
# the ceiling rather than mid-chain -- abandoning a half-answered challenge
# would waste the numbers already spent on it.
DEFAULT_MAX_NUMBERS_PER_PASS = 6

# ...and the ceiling that actually matters once a timer is driving this. The
# per-pass ceiling bounds one tick; nothing bounds *the day* except how often
# the timer fires, and a 30-minute timer at 6 numbers a tick is a 288-number
# day against a wallet that has never held more than ten dollars.
#
# Counted from the attempt ledger over a rolling 24 hours, not from a counter
# that resets at midnight: a run at 23:50 and another at 00:10 are twenty
# minutes apart and would otherwise both get a full day's budget.
#
# 12 numbers is roughly $2.40 -- about four profiles' worth of a real challenge
# chain, which is more than a healthy day ever needs, and survivable when a bad
# pool eats three numbers for nothing.
DEFAULT_MAX_NUMBERS_PER_DAY = 12

# Refuse to start a paid pass under this much credit with the active provider.
# One number is ~$0.20 and a profile can want three, so starting a pass under
# this cannot finish even one profile -- it can only burn launches discovering
# that the wallet is empty.
MIN_BALANCE_TO_START = 1.00

# Stop the pass after this many failures in a row that another phone will not
# fix. Every profile costs a launch and about four minutes, so grinding through
# the whole limit against an empty wallet or a MultiLogin outage spends half an
# hour to learn the same thing twice.
MAX_CONSECUTIVE_FLEET_FAILURES = 2

# A profile whose result cannot change without a person is not worth retrying
# every six hours. `Blank (10)` is the case that pays for this: it rented a
# number, received the code, and *then* hit a video-selfie request -- so every
# retry costs a launch and a number to reach the same wall. A week is long
# enough to stop paying for it and short enough that the profile comes back on
# its own if Instagram changes its mind.
TERMINAL_COOLOFF_HOURS = 24.0 * 7

# Result details that will read the same way tomorrow. Matched on the detail
# text because that is where the reason lives -- `needs_human` covers both "no
# code arrived" (worth another go when the pool is kinder) and "Instagram wants
# a video selfie" (never worth another go).
# Note what is NOT here any more: the consent gate. It was terminal while it
# needed a person; since 2026-08-12 the flow taps through it, so a run that
# still could not get past one is a mechanical failure worth retrying, not a
# settled answer.
_TERMINAL_MARKERS = (
    "photo challenge could not be completed",   # the video-selfie request
    "number the bot does not control",          # IG texts an owner's own phone
    "nobody is logged into instagram",          # needs credentials
    "instagram is not installed",               # needs provisioning, not a run
    "account is disabled",                      # banned
)


# What to write back into MultiLogin when a run settles something a person will
# have to act on. Deliberately the tags the VAs *already* use -- `logged out`
# (11 profiles), `unable to verify` (3), `Banned / Dead` (4) -- rather than a
# private vocabulary. Three things follow from reusing theirs:
#
#   * the finding lands where the work actually happens, instead of in a log
#     file and a local JSON nobody opens;
#   * `DIAGNOSED_ELSEWHERE_TAGS` already skips these, so the next pass costs
#     nothing on a profile this one has settled;
#   * a VA reading the workspace cannot tell a bot's `logged out` from their
#     own, and does not need to -- it means the same thing either way.
#
# Only outcomes with a screen behind them get one. "No code arrived" is a bad
# hour, not a diagnosis, and must never become a tag that hides a profile.
_DIAGNOSIS_TAGS = (
    # (status, detail marker or None, tag)
    (verification.RESULT_SIGNED_OUT, None, "logged out"),
    (verification.RESULT_BANNED, None, "Banned / Dead"),
    (None, "photo challenge could not be completed", "unable to verify"),
    (None, "number the bot does not control", "unable to verify"),
)


def diagnosis_tag_for(outcome) -> str | None:
    """The MultiLogin tag that records what this run found, or None."""
    detail = str(outcome.detail or "").lower()
    for status, marker, tag in _DIAGNOSIS_TAGS:
        if status is not None and outcome.status == status:
            return tag
        if marker is not None and marker in detail:
            return tag
    return None


def is_terminal_outcome(outcome) -> bool:
    """Whether re-running this profile could plausibly give a different answer.

    Transient by default. Getting this wrong in the cautious direction costs
    one wasted retry; getting it wrong the other way hides a profile for a
    week, so only reasons that are demonstrably about the *account* count.
    """
    if outcome.status in (verification.RESULT_BANNED, verification.RESULT_SIGNED_OUT):
        return True
    detail = str(outcome.detail or "").lower()
    return any(marker in detail for marker in _TERMINAL_MARKERS)

# How long to wait for a launched phone to answer over ADB. NOT the shipped
# defaults of `prepare_profile_for_adb` (2 attempts, 10s), which are far too
# short for these MultiLogin cloud phones: a cold launch takes 50-65 seconds
# and MultiLogin 500s on the first try often enough to matter. With the
# defaults this pass gave up on `Blank (10)` after 49 seconds and called it
# "never became ADB-ready" -- which is a *fleet-level* failure, so two of them
# in a row would abort the whole run over phones that were merely still
# booting. The probe has used 8 x 15s all along; this is the same.
READINESS_ATTEMPTS = 8
READINESS_WAIT_SECONDS = 15

# ...and how many times to retry the ADB connection itself, which is a separate
# failure from readiness: MultiLogin can report a phone ready while adb sits in
# `error: device offline`. `Luisa 3` (2026-08-12) did exactly that and was lost
# after the default 3 tries. The probe has always used 5; matched here for the
# same reason the readiness settings were.
CONNECT_ATTEMPTS = 5
CONNECT_RETRY_SECONDS = 5

# Failure texts that are about the fleet or the wallet rather than this account.
# Matched on the message because that is where the reason actually is: the
# router raises one exception type for an empty wallet, a refusing provider and
# a tripped breaker alike, and all three mean "the next profile will fail too".
_FLEET_LEVEL_MARKERS = (
    "could not rent a number",
    "never became adb-ready",
    "could not reach it over adb",
    "instagram would not open",
)


def is_fleet_level_failure(outcome) -> bool:
    """Whether this failure says something about the fleet, not the account.

    A challenge the flow cannot answer is this profile's problem and the next
    one deserves its turn. An empty wallet, a provider refusing, MultiLogin not
    starting phones -- those are the same answer for everybody, and the only
    useful response is to stop and say so.
    """
    text = f"{outcome.error} {outcome.detail}".lower()
    return any(marker in text for marker in _FLEET_LEVEL_MARKERS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- the attempt ledger -------------------------------------------------------
def ledger_path(app_dir=None) -> Path:
    if app_dir is None:
        from adb_bot.config.settings import get_app_data_dir
        app_dir = get_app_data_dir()
    return Path(app_dir) / LEDGER_FILENAME


def load_attempts(app_dir=None) -> dict:
    """`{launch_id: {'at': iso, 'result': str}}`, or {} if unreadable.

    Unreadable is deliberately the same as empty: a corrupt ledger must not
    stop the pass, it must only cost the cool-off it was remembering.
    """
    try:
        data = json.loads(ledger_path(app_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def numbers_spent_since(attempts: dict, since: datetime) -> int:
    """Rented numbers recorded in the ledger at or after `since`.

    The ledger is keyed by launch id and holds one entry per profile, so this
    counts *the last attempt on each profile*, not every attempt ever made --
    which is the right number anyway: an entry is overwritten only when that
    profile is worked again, and the cool-off means that cannot happen twice
    inside six hours.

    An entry with no `numbers` key predates this being recorded and counts as
    zero. That undercounts for one cool-off period after an upgrade and then
    never again, which is the cheap direction to be wrong in for a ceiling that
    is about not spending tomorrow's money today.
    """
    total = 0
    for entry in (attempts or {}).values():
        stamp = _parse((entry or {}).get("at"))
        if stamp is None or stamp < since:
            continue
        try:
            total += int((entry or {}).get("numbers") or 0)
        except (TypeError, ValueError):
            continue
    return total


def save_attempts(attempts: dict, app_dir=None) -> bool:
    try:
        path = ledger_path(app_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(attempts, indent=2, sort_keys=True), encoding="utf-8")
        return True
    except Exception:
        return False


# --- planning (pure) ----------------------------------------------------------
@dataclass
class PlannedProfile:
    launch_id: str
    name: str
    remark: str = ""
    reason: str = ""   # lowercased Airtable `Issue Reason`, "" if unknown


@dataclass
class VerificationPlan:
    to_run: list = field(default_factory=list)      # PlannedProfile
    cooling_off: list = field(default_factory=list)  # (name, hours remaining)
    diagnosed: list = field(default_factory=list)    # (name, the tag that says so)
    protected: list = field(default_factory=list)    # (name, the Airtable reason)
    not_found: list = field(default_factory=list)    # names asked for that do not exist
    over_limit: int = 0
    flagged: int = 0

    def summary(self) -> str:
        parts = [f"{self.flagged} flagged", f"{len(self.to_run)} to run",
                 f"{len(self.cooling_off)} cooling off",
                 f"{len(self.diagnosed)} already diagnosed by a person",
                 f"{len(self.protected)} protected", f"{self.over_limit} over the limit"]
        if self.not_found:
            parts.append(f"{len(self.not_found)} not found")
        return ", ".join(parts)


def _wanted_key(name: str) -> str:
    """Normalise a profile name for matching: hand-typed, so casing and spacing
    both vary ('Luisa 2', 'luisa  2')."""
    return " ".join(str(name or "").split()).lower()


def reasons_by_launch(profile_records) -> dict:
    """`{launch_id: lowercased Airtable Issue Reason}` from `profile_overview()`.

    Its own function because both halves of this module need it and neither
    should have to know the shape of an Airtable row to ask "what did somebody
    already decide about this profile?".
    """
    out: dict = {}
    for record in profile_records or []:
        launch_id = str(record.get("launch_id") or "").strip()
        if launch_id:
            out[launch_id] = str(record.get("reason") or "").strip().lower()
    return out


def plan_verification(mlx_items, attempts=None, now=None,
                      limit: int = DEFAULT_LIMIT,
                      cooloff_hours: float = DEFAULT_COOLOFF_HOURS,
                      only=None, respect_diagnosis: bool = True,
                      match: str | None = None,
                      reasons=None) -> VerificationPlan:
    """Which flagged profiles this pass should work, and which it should not.

    Ordering matters more than it looks. Profiles Airtable has recorded as
    `Human Verification Required` go first -- that is a detector saying it *saw*
    Instagram ask, which is the signal this whole flow exists to answer.
    MultiLogin's remark is the weaker second key: it understates the problem
    badly (only 10 of 70 tagged profiles say "human verification" and 34 say
    nothing at all), so it is a *priority*, never a filter. A pass that filtered
    on either would skip most of the real work -- the tag is what selects, and
    the reason only decides who goes first.

    `reasons` is `{launch_id: issue reason}` from `reasons_by_launch()`. Passing
    it also switches on the `PROTECTED_REASONS` guard; without it this plans
    exactly as it did before, which is what the pure unit tests still assert.
    """
    now = now or _now()
    reasons = reasons or {}
    attempts = attempts or {}
    plan = VerificationPlan()
    wanted = {_wanted_key(n) for n in only} if only else None
    needle = _wanted_key(match) if match else None
    seen_names: dict = {}

    candidates = []
    for item in mlx_items or []:
        tags = item.get("tags") or []
        name_key = _wanted_key(item.get("serial_name") or "")

        # An explicit list overrides the tag filter -- somebody asking for a
        # named profile has a reason, and refusing because the tag is missing
        # would just be unhelpful. It does NOT override the diagnosis check
        # below: that one is another person's finding, not a filter.
        #
        # An entry may be a launch id instead of a name, because names on this
        # workspace are not unique and the ambiguity warning below has always
        # said "use the id" without there being any way to do so: three
        # profiles are called `Blank (13)`, and asking for that name works all
        # three, two of which nobody asked about.
        if wanted is not None:
            # Normalised the same way names are: ids are digits on this
            # workspace, so this only ever costs surrounding whitespace, and it
            # keeps one rule for what "the same string" means.
            item_key = _wanted_key(item.get("id") or "")
            if item_key and item_key in wanted:
                seen_names.setdefault(item_key, 0)
                seen_names[item_key] += 1
            elif name_key in wanted:
                seen_names.setdefault(name_key, 0)
                seen_names[name_key] += 1
            else:
                continue
        elif ISSUE_TAG not in tags:
            continue

        # A substring of the name, for working a family of profiles at once
        # ("blank"). Applied on top of the tag filter, not instead of it, and
        # deliberately a *substring* rather than an exact name: the families on
        # this workspace share a prefix and differ by a number, and several of
        # those numbers are duplicated across two profiles -- so an exact-name
        # selection could not address them at all.
        if needle and needle not in name_key:
            continue
        launch_id = str(item.get("id") or "").strip()
        if not launch_id:
            # No id means no way to launch, tag or shut it down. Counted as
            # flagged so the numbers still reconcile against MultiLogin.
            plan.flagged += 1
            continue
        plan.flagged += 1
        name = str(item.get("serial_name") or launch_id)
        remark = str(item.get("remark") or "")

        # Somebody -- or an earlier pass -- has already settled this profile in
        # Airtable, and the answer is one this flow must not overturn. Checked
        # BEFORE the cool-off and before `--only`, because unlike every other
        # skip here this one is not about saving a launch: working the profile
        # at all risks reading a banned account's cached feed as healthy and
        # untagging it. `--ignore-diagnosis` deliberately does not reach it.
        reason = reasons.get(launch_id, "")
        if reason in PROTECTED_REASONS:
            plan.protected.append((name, reason))
            continue

        # A person has already looked and said what is wrong. Believe them:
        # `logged out` needs credentials and `unable to verify` means somebody
        # tried. Launching either costs two minutes to rediscover the tag.
        if respect_diagnosis:
            diagnosis = next((tag for tag in tags
                              if _wanted_key(tag) in DIAGNOSED_ELSEWHERE_TAGS), None)
            if diagnosis:
                plan.diagnosed.append((name, diagnosis))
                continue

        entry = attempts.get(launch_id) or {}
        last = _parse(entry.get("at"))
        if last is not None:
            # A result that cannot change without a person waits far longer --
            # see `is_terminal_outcome`.
            hours = TERMINAL_COOLOFF_HOURS if entry.get("terminal") else cooloff_hours
            ready_at = last + timedelta(hours=hours)
            if now < ready_at:
                plan.cooling_off.append(
                    (name, (ready_at - now).total_seconds() / 3600.0))
                continue
        candidates.append(PlannedProfile(launch_id=launch_id, name=name, remark=remark,
                                        reason=reason))

    candidates.sort(key=lambda p: (0 if p.reason == DETECTED_VERIFICATION_REASON else 1,
                                   0 if "verif" in p.remark.lower() else 1,
                                   p.name.lower()))
    plan.to_run = candidates[:limit]
    plan.over_limit = max(0, len(candidates) - limit)

    if wanted is not None:
        # Names that matched nothing, and names that matched more than one
        # profile -- both are reported rather than guessed at. Profile names on
        # this workspace are NOT unique.
        for key in sorted(wanted):
            count = seen_names.get(key, 0)
            if count == 0:
                plan.not_found.append(f"{key} (no such profile)")
            elif count > 1:
                plan.not_found.append(f"{key} (matches {count} profiles -- use the id)")
    return plan


# --- what one result means ----------------------------------------------------
@dataclass
class ProfileOutcome:
    name: str
    launch_id: str
    status: str = ""
    detail: str = ""
    numbers_used: int = 0
    untagged: bool = False
    flagged_banned: bool = False
    diagnosis_tag: str = ""   # what this run wrote back into MultiLogin
    error: str = ""


@dataclass
class VerificationReport:
    outcomes: list = field(default_factory=list)    # ProfileOutcome
    plan: VerificationPlan | None = None
    dry_run: bool = True
    aborted: str = ""      # a fleet-level problem stopped the pass -- an error
    stopped: str = ""      # the pass hit its own ceiling -- not an error

    def counts(self) -> dict:
        out: dict = {}
        for outcome in self.outcomes:
            key = outcome.error and "error" or outcome.status
            out[key] = out.get(key, 0) + 1
        return out

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        counts = ", ".join(f"{k}={v}" for k, v in sorted(self.counts().items())) or "nothing"
        spent = sum(o.numbers_used for o in self.outcomes)
        summary = (f"[{mode}] verification: {counts}; {spent} number(s) rented, "
                   f"{sum(1 for o in self.outcomes if o.untagged)} profile(s) handed back")
        if self.aborted:
            return f"{summary} -- ABORTED: {self.aborted}"
        return f"{summary} -- {self.stopped}" if self.stopped else summary


def route_result(status: str) -> tuple:
    """`(untag, flag_banned)` for one verification result status.

    Split out from the loop below because it is the decision worth testing on
    its own: everything else in the apply half needs a phone, and this is the
    part where getting it wrong hands a broken account back to the posting loop.
    """
    if status == verification.RESULT_SOLVED:
        return (True, False)
    if status == verification.RESULT_BANNED:
        return (False, True)
    # needs_human / stuck / failed / signed_out: the profile stays exactly where
    # it was, which is flagged and tagged. `signed_out` is in this list on
    # purpose -- see the module docstring.
    return (False, False)


# --- apply --------------------------------------------------------------------
def run_verification_pass(clients, adb_client, airtable, logger, mlx_items,
                          tag_client=None, dry_run: bool = True,
                          limit: int = DEFAULT_LIMIT,
                          cooloff_hours: float = DEFAULT_COOLOFF_HOURS,
                          app_dir=None, country: str | None = None,
                          profile_records=None,
                          max_seconds: float = verification.MAX_RUN_SECONDS,
                          readiness_attempts: int = READINESS_ATTEMPTS,
                          readiness_wait: int = READINESS_WAIT_SECONDS,
                          only=None, respect_diagnosis: bool = True,
                          match: str | None = None,
                          max_numbers: int = DEFAULT_MAX_NUMBERS_PER_PASS,
                          max_per_day: int = DEFAULT_MAX_NUMBERS_PER_DAY,
                          ) -> VerificationReport:
    """Work up to `limit` flagged profiles. Returns what happened to each.

    `dry_run` names the profiles it would work and rents nothing -- the money is
    behind the flag, not behind a config file.
    """
    from adb_bot.core import locks

    attempts = load_attempts(app_dir)
    reasons = reasons_by_launch(profile_records)
    plan = plan_verification(mlx_items, attempts=attempts, limit=limit,
                             cooloff_hours=cooloff_hours, only=only,
                             respect_diagnosis=respect_diagnosis, match=match,
                             reasons=reasons)
    report = VerificationReport(plan=plan, dry_run=dry_run)
    logger.info("verification pass: %s", plan.summary())
    for name, reason in plan.protected:
        logger.info("verification pass: skipping %s -- Airtable records it as %r, "
                    "which this pass must not overturn", name, reason)
    for name, tag in plan.diagnosed:
        logger.info("verification pass: skipping %s -- somebody tagged it %r, "
                    "which verification cannot fix", name, tag)
    for missing in plan.not_found:
        logger.warning("verification pass: asked for %s", missing)

    if dry_run:
        for planned in plan.to_run:
            logger.info("verification pass: would work %s [%s]",
                        planned.name, planned.launch_id)
            report.outcomes.append(ProfileOutcome(
                name=planned.name, launch_id=planned.launch_id,
                status="would-run", detail="dry run"))
        return report

    by_launch = {str(r.get("launch_id") or ""): r for r in (profile_records or [])}

    # What the last 24 hours already cost, before this pass adds to it.
    spent_today = numbers_spent_since(attempts, _now() - timedelta(hours=24))
    if max_per_day and spent_today:
        logger.info("verification pass: %d number(s) rented in the last 24 hours, "
                    "ceiling %d", spent_today, max_per_day)

    consecutive_fleet_failures = 0
    for planned in plan.to_run:
        # Money, checked at the profile boundary. `--limit` bounds phones and
        # this bounds spend; a five-profile pass is a fifteen-number worst case,
        # which is more than the wallet has ever held. Checked here rather than
        # inside the chain on purpose: stopping mid-challenge would throw away
        # the numbers already spent answering it.
        spent = sum(o.numbers_used for o in report.outcomes)
        if max_per_day and (spent_today + spent) >= max_per_day:
            report.stopped = (
                f"stopped on the daily ceiling: {spent_today + spent} number(s) "
                f"rented in the last 24 hours, limit {max_per_day}. The flagged "
                f"profiles are still flagged and tomorrow's ticks take them.")
            logger.info("verification pass: %s", report.stopped)
            break
        if max_numbers and spent >= max_numbers:
            # `stopped`, not `aborted`: reaching the ceiling is the ceiling
            # working, and a timer that painted itself red for it would train
            # everyone to ignore a red verification unit. Only a fleet-level
            # abort is an error -- see `main`'s exit code.
            report.stopped = (f"stopped after {spent} rented number(s), the "
                              f"per-pass ceiling of {max_numbers}. The profiles "
                              f"left are still flagged and the next tick takes "
                              f"them.")
            logger.info("verification pass: %s", report.stopped)
            break

        outcome = ProfileOutcome(name=planned.name, launch_id=planned.launch_id)
        report.outcomes.append(outcome)

        # The same lock the posting and warm-up loops take. Without it this can
        # launch a profile another loop is mid-post on, which loses that post
        # and reads afterwards as a random Instagram failure.
        with locks.ProfileLocks(owner="verification") as held:
            if not held.acquire_all([planned.launch_id]):
                outcome.error = "busy in another loop"
                logger.info("verification pass: %s is busy; leaving it for next tick",
                            planned.name)
                continue
            try:
                _work_one(clients, adb_client, logger, planned, outcome, country,
                          max_seconds=max_seconds,
                          readiness_attempts=readiness_attempts,
                          readiness_wait=readiness_wait)
                # Exactly one line per profile, whatever happened. `_work_one`
                # logs nothing itself: it settles some profiles before the
                # chain ever runs -- no Instagram installed, no usable device --
                # and each of those returns from a different place. Leaving the
                # line to each return is how `Blank (9)` produced `launching`
                # and then silence on 2026-08-13, with a status, a detail and a
                # seven-day bench that nothing in the log ever mentioned.
                if outcome.error:
                    logger.warning("verification pass: %s -> could not be worked "
                                   "(%s)", planned.name, outcome.error)
                elif outcome.status:
                    logger.info("verification pass: %s -> %s (%s)", planned.name,
                                outcome.status, outcome.detail)
                else:
                    # Not reachable by any current path, and logged rather than
                    # asserted because a pass that stops mid-fleet over a
                    # bookkeeping slip is worse than one that says so.
                    logger.warning("verification pass: %s -> finished with no "
                                   "outcome recorded", planned.name)
            except Exception as exc:
                # One phone's failure must not end the pass: the next profile is
                # a different phone with a different problem.
                outcome.error = f"{type(exc).__name__}: {exc}"
                logger.warning("verification pass: %s raised (%s)", planned.name, exc)
            finally:
                try:
                    clients.shutdown.shutdown_profiles([planned.launch_id])
                except Exception as exc:
                    logger.warning("verification pass: shutdown failed for %s (%s)",
                                   planned.name, exc)

        # Recorded whatever happened, including an error: a phone that raises
        # every time must not be retried every tick.
        attempts[planned.launch_id] = {
            "at": _now().isoformat(),
            "result": outcome.error or outcome.status or "unknown",
            "name": planned.name,
            # The detail, not just the status: "needs_human" alone cannot tell
            # anyone later whether this was a bad SMS pool or a video selfie.
            "detail": outcome.detail or "",
            "terminal": is_terminal_outcome(outcome),
            # What this profile cost, so the rolling daily ceiling has something
            # to count. Kept here rather than in a counter of its own: the
            # ledger is already the file that survives a restart, and a second
            # spend file could disagree with it.
            "numbers": int(outcome.numbers_used or 0),
        }
        _write_back(airtable, tag_client, logger, planned, outcome,
                    by_launch.get(planned.launch_id))

        # Stop while it still means something. The cool-off above is already
        # written for every profile touched, so nothing is retried in a tight
        # loop either way -- this only saves the launches.
        if is_fleet_level_failure(outcome):
            consecutive_fleet_failures += 1
            if consecutive_fleet_failures >= MAX_CONSECUTIVE_FLEET_FAILURES:
                report.aborted = (
                    f"stopped after {consecutive_fleet_failures} failures in a row "
                    f"that another phone will not fix (last: "
                    f"{outcome.error or outcome.detail}). Check the provider "
                    f"balances and that MultiLogin is starting phones.")
                logger.warning("verification pass: %s", report.aborted)
                break
        else:
            consecutive_fleet_failures = 0

    save_attempts(attempts, app_dir)
    logger.info("verification pass: %s", report.summary())
    return report


def _work_one(clients, adb_client, logger, planned, outcome, country,
              max_seconds: float = verification.MAX_RUN_SECONDS,
              readiness_attempts: int = READINESS_ATTEMPTS,
              readiness_wait: int = READINESS_WAIT_SECONDS) -> None:
    """Launch one phone and run the chain on it. Fills `outcome` in place."""
    from adb_bot.automation.flows.verification_driver import (
        AdbChallengeDriver, VerificationRecorder,
    )
    from adb_bot.automation.verification_probe import (
        _open_instagram, instagram_installed,
    )
    from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
    from adb_bot.clients.sms.base import DEFAULT_COUNTRY
    from adb_bot.clients.sms.router import build_router

    logger.info("verification pass: launching %s", planned.name)
    clients.launcher.start_profiles([planned.launch_id])

    profile = prepare_profile_for_adb(
        planned.launch_id, clients.api, clients.adb_enable, logger,
        max_attempts=readiness_attempts, wait_seconds=readiness_wait,
        launcher_client=clients.launcher)
    if not profile:
        outcome.error = "never became ADB-ready"
        return

    target = connect_with_retries(adb_client, profile, logger, planned.launch_id,
                                  max_attempts=CONNECT_ATTEMPTS,
                                  retry_delay_seconds=CONNECT_RETRY_SECONDS)
    if not target:
        # Distinguished from "never became ADB-ready" on purpose: this one means
        # MultiLogin said the phone was ready and adb still could not use it,
        # which is the stale-`offline` case rather than a slow boot.
        outcome.error = ("could not reach it over ADB (MultiLogin reported it "
                         "ready, but adb never saw a usable device)")
        return

    # Asked before trying to start it, because the two failures want different
    # answers. A phone with no Instagram on it is not evidence about any other
    # phone, and `Blank (9)` (2026-08-12) reported as "would not open" -- a
    # fleet-level failure -- when the app simply was not there. Two of those in
    # a row would have aborted a pass over one unprovisioned phone.
    if not instagram_installed(target, adb_client, logger=logger):
        outcome.status = verification.RESULT_NEEDS_HUMAN
        outcome.detail = ("Instagram is not installed on this phone, so there is "
                          "nothing to verify -- it needs provisioning, not a "
                          "verification run")
        return

    if not _open_instagram(target, adb_client, logger):
        # Reading the screen now would describe the Android launcher, and a
        # launcher screen classifies as a clean "none" -- which this flow would
        # then call solved. Refusing is the only safe answer.
        outcome.error = "Instagram would not open"
        return

    recorder = VerificationRecorder(planned.name, logger=logger)
    driver = AdbChallengeDriver(target, adb_client, logger=logger,
                                recorder=recorder, act=True)
    result = verification.run_verification(
        driver, build_router(logger=logger), logger=logger,
        country=country or DEFAULT_COUNTRY, max_seconds=max_seconds)

    outcome.status = result.status
    outcome.detail = result.detail
    outcome.numbers_used = result.numbers_used
    # The per-profile line is the caller's, so that the profiles settled above
    # -- before this chain runs at all -- get one too.


def _record_diagnosis_tag(tag_client, logger, planned, outcome) -> None:
    """Write what this run found back into MultiLogin, as a tag a VA already uses.

    Never raises and never removes anything -- `assign` only adds. An existing
    tag is left alone rather than re-applied, and a tag the workspace has not
    got is skipped rather than invented: this borrows the VAs' vocabulary, it
    does not get to extend it.
    """
    tag = diagnosis_tag_for(outcome)
    if not tag or tag_client is None:
        return
    try:
        tag_id = tag_client.tag_ids_by_name().get(tag.lower())
        if not tag_id:
            logger.info("verification pass: would tag %s %r, but the workspace "
                        "has no such tag", planned.name, tag)
            return
        if tag_client.assign(planned.launch_id, [tag_id]):
            outcome.diagnosis_tag = tag
            logger.info("verification pass: tagged %s %r -- so the next pass "
                        "skips it and a VA can see why without reading a log",
                        planned.name, tag)
    except Exception as exc:
        logger.warning("verification pass: tagging %s %r failed (%s)",
                       planned.name, tag, exc)


def _write_back(airtable, tag_client, logger, planned, outcome, record) -> None:
    """Act on one outcome. Never raises -- a write failure is not worth the pass."""
    if outcome.error or not outcome.status:
        return

    untag, flag_banned = route_result(outcome.status)

    # The same guard as the planner's, applied to the write instead of the
    # selection. Deliberately duplicated rather than trusted once: the planner
    # only sees the reasons it was given, and `--only` plus a stale Airtable
    # read is enough to put a protected profile in front of this line. Refusing
    # here costs a wasted launch; the alternative costs a banned account handed
    # back to the posting loop, which is what happened to `Laila 5`.
    reason = str((record or {}).get("reason") or "").strip().lower()
    if untag and reason in PROTECTED_REASONS:
        logger.warning(
            "verification pass: %s read as %s, but Airtable records it as %r -- "
            "NOT untagging. A suspended account renders a cached feed, so a "
            "clean screen is not evidence the ban is gone.",
            planned.name, outcome.status, reason)
        return
    _record_diagnosis_tag(tag_client, logger, planned, outcome)

    if untag and tag_client is not None:
        try:
            from adb_bot.automation.issue_tags import resolve_issue_tag_id
            tag_id = resolve_issue_tag_id(tag_client)
            if tag_id and tag_client.unassign(planned.launch_id, [tag_id]):
                outcome.untagged = True
                logger.info("verification pass: took the %s tag off %s -- issue_tags "
                            "will clear the flag and recovery_runner will re-queue it",
                            ISSUE_TAG, planned.name)
            else:
                logger.warning("verification pass: could not take the %s tag off %s; "
                               "it stays flagged", ISSUE_TAG, planned.name)
        except Exception as exc:
            logger.warning("verification pass: untagging %s failed (%s)",
                           planned.name, exc)

    if not flag_banned:
        return

    # Not every MultiLogin profile has an Airtable row. The staging ones
    # ("Default profile name (NN)") are tagged and launchable but unmanaged --
    # `632451306307322212` is exactly this -- so there is nothing to flag and no
    # record of the ban anywhere except the log. Said out loud rather than
    # skipped quietly: a banned account nobody hears about is the whole reason
    # the incident write-back exists.
    if not (record and record.get("record_id")):
        logger.warning(
            "verification pass: %s [%s] is BANNED, but has no Profiles (Cloning) "
            "row, so the ban could not be recorded in Airtable. The MultiLogin "
            "'%s' tag has been left on, which is the only thing now marking it.",
            planned.name, planned.launch_id, ISSUE_TAG)
        return

    try:
        airtable.flag_profile_for_human(
            record["record_id"], at.PROFILE_ISSUE_BANNED,
            f"verification pass: {outcome.detail}")
        outcome.flagged_banned = True
        logger.warning("verification pass: %s is banned; flagged and left tagged",
                       planned.name)
    except Exception as exc:
        logger.warning("verification pass: flagging %s as banned failed (%s)",
                       planned.name, exc)


# --- CLI ----------------------------------------------------------------------
def main(argv=None) -> int:
    """Dry-run by default. `--apply` is what rents numbers and taps phones."""
    import argparse

    from adb_bot.automation.bootstrap import build_mlx_clients
    from adb_bot.automation.run_loop import _airtable
    from adb_bot.automation.verification_probe import _profiles, _resolve_token
    from adb_bot.clients.adb import ADBClient
    from adb_bot.clients.multilogin.tags import MultiloginTagClient
    from adb_bot.core.logger import get_logger

    parser = argparse.ArgumentParser(
        prog="python -m adb_bot.automation.verification_runner",
        description="Work the flagged fleet through Instagram's verification chain.")
    parser.add_argument("--apply", action="store_true",
                        help="actually rent numbers and drive the phones "
                             "(without this, only says what it would work).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"profiles per pass (default {DEFAULT_LIMIT}). "
                             f"With --only, defaults to the length of that list.")
    parser.add_argument("--cooloff-hours", type=float, default=DEFAULT_COOLOFF_HOURS,
                        help=f"leave a worked profile alone this long "
                             f"(default {DEFAULT_COOLOFF_HOURS}).")
    parser.add_argument("--country", default=None,
                        help="override the country numbers are rented from.")
    parser.add_argument("--only", default=None,
                        help="comma-separated profile names or launch ids to "
                             "work instead of the whole flagged population "
                             "(e.g. 'luisa 2,luisa 3'). Names on this workspace "
                             "are not unique -- three profiles are called "
                             "'Blank (13)' -- so use the id to mean one of "
                             "them. Overrides the Issue-tag filter, but not the "
                             "diagnosis check.")
    parser.add_argument("--match", default=None,
                        help="only work flagged profiles whose name contains "
                             "this (e.g. 'blank'). Combines with --limit.")
    parser.add_argument("--ignore-diagnosis", action="store_true",
                        help=f"work profiles even when a person has tagged them "
                             f"{sorted(DIAGNOSED_ELSEWHERE_TAGS)}. Use when a "
                             f"tag is stale.")
    parser.add_argument("--readiness-attempts", type=int, default=READINESS_ATTEMPTS,
                        help=f"tries for a phone to answer over ADB "
                             f"(default {READINESS_ATTEMPTS}).")
    parser.add_argument("--readiness-wait", type=int, default=READINESS_WAIT_SECONDS,
                        help=f"seconds per readiness attempt "
                             f"(default {READINESS_WAIT_SECONDS}).")
    parser.add_argument("--max-numbers", type=int, default=DEFAULT_MAX_NUMBERS_PER_PASS,
                        help=f"stop the pass once this many numbers have been "
                             f"rented (default {DEFAULT_MAX_NUMBERS_PER_PASS}). "
                             f"Bounds money the way --limit bounds phones; 0 "
                             f"means no ceiling.")
    parser.add_argument("--max-numbers-per-day", type=int,
                        default=DEFAULT_MAX_NUMBERS_PER_DAY,
                        help=f"stop once this many numbers have been rented in "
                             f"the last 24 hours, across every pass (default "
                             f"{DEFAULT_MAX_NUMBERS_PER_DAY}). This is the one "
                             f"that bounds a timer; 0 means no ceiling.")
    parser.add_argument("--min-balance", type=float, default=MIN_BALANCE_TO_START,
                        help=f"refuse to start a paid pass under this much "
                             f"credit (default {MIN_BALANCE_TO_START}). 0 skips "
                             f"the check.")
    parser.add_argument("--mlx-token", default=None)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)

    airtable = None
    profile_records = None
    # Read in BOTH modes now. It used to be apply-only, on the reasoning that a
    # dry run needs nothing from Airtable -- but the `PROTECTED_REASONS` guard
    # lives there, so without it a dry run lists profiles the real pass would
    # refuse, which is the one thing a dry run must not do. An Airtable outage
    # still cannot stop the pass: it degrades to the old behaviour, loudly.
    try:
        airtable = _airtable()
        profile_records = airtable.profile_overview()
    except Exception as exc:
        logger.warning("verification pass: no Airtable (%s); running without the "
                       "protected-reason guard and without banned write-back", exc)
    # Ask the wallet before launching anything. A pass that starts broke cannot
    # do its job and does not fail cleanly either: it launches a phone, reads a
    # challenge, fails to rent, and books that as a *fleet-level* failure -- so
    # two profiles in, it aborts having spent four minutes and two launches to
    # discover a balance one HTTP call would have told it. Free to ask, and the
    # only preflight that can save a whole tick.
    if args.apply and args.min_balance > 0:
        try:
            from adb_bot.clients.sms.router import build_router
            # The best of them, not the active one: the breaker will switch to
            # whichever still has credit, so a flat 5sim does not mean a pass
            # cannot run when SMSPool is funded.
            best = 0.0
            for provider in build_router(logger=logger).providers:
                try:
                    best = max(best, float(provider.balance()))
                except Exception as exc:
                    logger.warning("verification pass: %s balance unreadable (%s)",
                                   provider.name, exc)
            if best < args.min_balance:
                print(f"REFUSED: best provider balance is {best:.2f}, under the "
                      f"{args.min_balance:.2f} floor. Top up before running; "
                      f"nothing was launched.")
                logger.warning("verification pass: refusing to start -- best balance "
                               "%.2f is under the %.2f floor", best, args.min_balance)
                return 1
        except Exception as exc:
            # Never a reason to refuse: the balance check is an optimisation,
            # and a provider whose API is down may still rent.
            logger.warning("verification pass: could not read balances (%s); "
                           "starting anyway", exc)

    only = [n for n in (args.only or "").split(",") if n.strip()] or None
    # An explicit list is a request for those profiles, so the default limit
    # must not quietly drop the tail of it.
    limit = args.limit
    if only and limit == DEFAULT_LIMIT:
        limit = len(only)

    report = run_verification_pass(
        build_mlx_clients(token), ADBClient(), airtable, logger, items,
        tag_client=MultiloginTagClient(token) if args.apply else None,
        dry_run=not args.apply, limit=limit,
        cooloff_hours=args.cooloff_hours, country=args.country,
        profile_records=profile_records,
        readiness_attempts=args.readiness_attempts,
        readiness_wait=args.readiness_wait,
        only=only, respect_diagnosis=not args.ignore_diagnosis,
        match=args.match, max_numbers=args.max_numbers,
        max_per_day=args.max_numbers_per_day)

    plan = report.plan
    print(f"\n{'=' * 70}")
    print(f"{'APPLY (spends money, taps phones)' if args.apply else 'DRY RUN'}")
    print(f"flagged     : {plan.flagged}")
    print(f"cooling off : {len(plan.cooling_off)}")
    print(f"diagnosed   : {len(plan.diagnosed)}  (a person already said what is wrong)")
    print(f"protected   : {len(plan.protected)}  (Airtable says do not touch)")
    print(f"over limit  : {plan.over_limit}")
    print(f"{'=' * 70}")
    for name, reason in plan.protected:
        print(f"  {name:24} PROTECTED -- Airtable reason {reason!r}")
    for name, tag in plan.diagnosed:
        print(f"  {name:24} skipped -- tagged {tag!r}")
    for missing in plan.not_found:
        print(f"  {missing:24} NOT RUN")
    for outcome in report.outcomes:
        note = outcome.error or f"{outcome.status} -- {outcome.detail}"
        marks = []
        if outcome.untagged:
            marks.append("untagged")
        if outcome.flagged_banned:
            marks.append("flagged banned")
        if outcome.diagnosis_tag:
            marks.append(f"tagged {outcome.diagnosis_tag!r}")
        if outcome.numbers_used:
            marks.append(f"{outcome.numbers_used} number(s)")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {outcome.name:24} {note}{suffix}")
    print(f"{'=' * 70}")
    if report.aborted:
        print(f"STOPPED EARLY: {report.aborted}")
    if report.stopped:
        print(f"CEILING: {report.stopped}")
    print(report.summary())
    # Non-zero when the pass gave up on a fleet-level problem, so a timer or a
    # watchdog can tell "worked through five profiles" from "could not rent a
    # number and stopped". An ordinary needs_human is not an error.
    return 1 if report.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
