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

# Stop the pass after this many failures in a row that another phone will not
# fix. Every profile costs a launch and about four minutes, so grinding through
# the whole limit against an empty wallet or a MultiLogin outage spends half an
# hour to learn the same thing twice.
MAX_CONSECUTIVE_FLEET_FAILURES = 2

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


@dataclass
class VerificationPlan:
    to_run: list = field(default_factory=list)      # PlannedProfile
    cooling_off: list = field(default_factory=list)  # (name, hours remaining)
    over_limit: int = 0
    flagged: int = 0

    def summary(self) -> str:
        return (f"{self.flagged} flagged, {len(self.to_run)} to run, "
                f"{len(self.cooling_off)} cooling off, {self.over_limit} over the limit")


def plan_verification(mlx_items, attempts=None, now=None,
                      limit: int = DEFAULT_LIMIT,
                      cooloff_hours: float = DEFAULT_COOLOFF_HOURS) -> VerificationPlan:
    """Which flagged profiles this pass should work, and which it should not.

    Ordering matters more than it looks: profiles whose MultiLogin remark
    already mentions verification go first. The remarks badly understate the
    problem -- only 10 of 70 tagged profiles say "human verification" and 34 say
    nothing at all -- so this is a *priority*, never a filter. A pass that
    filtered on the remark would skip most of the real work.
    """
    now = now or _now()
    attempts = attempts or {}
    plan = VerificationPlan()

    candidates = []
    for item in mlx_items or []:
        if ISSUE_TAG not in (item.get("tags") or []):
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

        last = _parse((attempts.get(launch_id) or {}).get("at"))
        if last is not None:
            ready_at = last + timedelta(hours=cooloff_hours)
            if now < ready_at:
                plan.cooling_off.append(
                    (name, (ready_at - now).total_seconds() / 3600.0))
                continue
        candidates.append(PlannedProfile(launch_id=launch_id, name=name, remark=remark))

    candidates.sort(key=lambda p: (0 if "verif" in p.remark.lower() else 1,
                                   p.name.lower()))
    plan.to_run = candidates[:limit]
    plan.over_limit = max(0, len(candidates) - limit)
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
    error: str = ""


@dataclass
class VerificationReport:
    outcomes: list = field(default_factory=list)    # ProfileOutcome
    plan: VerificationPlan | None = None
    dry_run: bool = True
    aborted: str = ""      # why the pass stopped early, if it did

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
        return f"{summary} -- ABORTED: {self.aborted}" if self.aborted else summary


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
                          max_seconds: float = verification.MAX_RUN_SECONDS
                          ) -> VerificationReport:
    """Work up to `limit` flagged profiles. Returns what happened to each.

    `dry_run` names the profiles it would work and rents nothing -- the money is
    behind the flag, not behind a config file.
    """
    from adb_bot.core import locks

    attempts = load_attempts(app_dir)
    plan = plan_verification(mlx_items, attempts=attempts, limit=limit,
                             cooloff_hours=cooloff_hours)
    report = VerificationReport(plan=plan, dry_run=dry_run)
    logger.info("verification pass: %s", plan.summary())

    if dry_run:
        for planned in plan.to_run:
            logger.info("verification pass: would work %s [%s]",
                        planned.name, planned.launch_id)
            report.outcomes.append(ProfileOutcome(
                name=planned.name, launch_id=planned.launch_id,
                status="would-run", detail="dry run"))
        return report

    by_launch = {str(r.get("launch_id") or ""): r for r in (profile_records or [])}

    consecutive_fleet_failures = 0
    for planned in plan.to_run:
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
                          max_seconds=max_seconds)
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
              max_seconds: float = verification.MAX_RUN_SECONDS) -> None:
    """Launch one phone and run the chain on it. Fills `outcome` in place."""
    from adb_bot.automation.flows.verification_driver import (
        AdbChallengeDriver, VerificationRecorder,
    )
    from adb_bot.automation.verification_probe import _open_instagram
    from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
    from adb_bot.clients.sms.base import DEFAULT_COUNTRY
    from adb_bot.clients.sms.router import build_router

    logger.info("verification pass: launching %s", planned.name)
    clients.launcher.start_profiles([planned.launch_id])

    profile = prepare_profile_for_adb(
        planned.launch_id, clients.api, clients.adb_enable, logger,
        launcher_client=clients.launcher)
    if not profile:
        outcome.error = "never became ADB-ready"
        return

    target = connect_with_retries(adb_client, profile, logger, planned.launch_id)
    if not target:
        outcome.error = "could not reach it over ADB"
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
    logger.info("verification pass: %s -> %s (%s)",
                planned.name, result.status, result.detail)


def _write_back(airtable, tag_client, logger, planned, outcome, record) -> None:
    """Act on one outcome. Never raises -- a write failure is not worth the pass."""
    if outcome.error or not outcome.status:
        return

    untag, flag_banned = route_result(outcome.status)

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

    if flag_banned and record and record.get("record_id"):
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
                        help=f"profiles per pass (default {DEFAULT_LIMIT}).")
    parser.add_argument("--cooloff-hours", type=float, default=DEFAULT_COOLOFF_HOURS,
                        help=f"leave a worked profile alone this long "
                             f"(default {DEFAULT_COOLOFF_HOURS}).")
    parser.add_argument("--country", default=None,
                        help="override the country numbers are rented from.")
    parser.add_argument("--mlx-token", default=None)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)

    airtable = None
    profile_records = None
    if args.apply:
        # Only needed to flag a banned profile, so a dry run does not read
        # Airtable at all -- and an Airtable outage cannot stop the pass that
        # matters, only the write-back for the one result that needs it.
        try:
            airtable = _airtable()
            profile_records = airtable.profile_overview()
        except Exception as exc:
            logger.warning("verification pass: no Airtable (%s); a banned profile "
                           "will be reported but not flagged", exc)

    report = run_verification_pass(
        build_mlx_clients(token), ADBClient(), airtable, logger, items,
        tag_client=MultiloginTagClient(token) if args.apply else None,
        dry_run=not args.apply, limit=args.limit,
        cooloff_hours=args.cooloff_hours, country=args.country,
        profile_records=profile_records)

    plan = report.plan
    print(f"\n{'=' * 70}")
    print(f"{'APPLY (spends money, taps phones)' if args.apply else 'DRY RUN'}")
    print(f"flagged     : {plan.flagged}")
    print(f"cooling off : {len(plan.cooling_off)}")
    print(f"over limit  : {plan.over_limit}")
    print(f"{'=' * 70}")
    for outcome in report.outcomes:
        note = outcome.error or f"{outcome.status} -- {outcome.detail}"
        marks = []
        if outcome.untagged:
            marks.append("untagged")
        if outcome.flagged_banned:
            marks.append("flagged banned")
        if outcome.numbers_used:
            marks.append(f"{outcome.numbers_used} number(s)")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {outcome.name:24} {note}{suffix}")
    print(f"{'=' * 70}")
    if report.aborted:
        print(f"STOPPED EARLY: {report.aborted}")
    print(report.summary())
    # Non-zero when the pass gave up on a fleet-level problem, so a timer or a
    # watchdog can tell "worked through five profiles" from "could not rent a
    # number and stopped". An ordinary needs_human is not an error.
    return 1 if report.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
