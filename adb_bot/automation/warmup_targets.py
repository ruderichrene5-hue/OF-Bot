"""Which MLX profiles are due for warm-up today, and on which day of the plan.

The warm-up used to run off the Airtable **Accounts** table: one row per IG
account, with a Creation Date to count campaign days from. That is not where new
accounts live. Fresh accounts exist as MultiLogin profiles long before anyone
writes an Accounts row -- 46 of the 150 profiles in this workspace are tagged
`Created` and named "Blank (N)", with no model and no Accounts row -- so the
account-driven planner could not see a single one of them.

This module drives the warm-up from the MLX inventory instead:

* **Who** -- profiles carrying the `Created` tag in MultiLogin. That tag is the
  client's own signal that an account exists and has been logged in; the bot
  reads it rather than keeping a second list in step with it.
* **Which day** -- counted from `Profiles (Cloning).Warm-up Started`, which the
  bot stamps on the first run and never moves. Day 1 is the day the warm-up
  actually begins, *not* the profile's MLX creation date: profiles are created
  in batches days before anyone starts on them, and counting from `created_at`
  would drop a week-old profile straight past a 5-day plan without warming it up
  at all.

Pure planning -- no MLX calls, no Airtable writes, no phones. It takes the
already-fetched MLX items and the Airtable rows, so the whole selection is
testable without either service.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta

from adb_bot.automation import lifecycle, warmup_completion
from adb_bot.automation.airtable_planner import AccountPlan, AirtablePlan, FlowRun, SkippedAccount
from adb_bot.automation.mlx_sync import normalize_mlx_item

# The MultiLogin tag that means "this account exists and is ready to be warmed
# up". Matched case-insensitively: it is typed by a person in the MLX UI.
WARMUP_TAG = "Created"

# How long past the end of the plan an unfinished profile still gets the days it
# owes re-run. A plan is days of *work*, not days of calendar: the calendar walks
# on at midnight whether or not the night's run landed, so without a catch-up a
# day lost to a bug is lost for good -- the profile falls off the end of the plan
# and is retired "finished" having never done it. That is not hypothetical: on
# 2026-08-11 every one of 155 day-4 runs across 46 profiles had died on a
# watchdog timeout, and 41 profiles were sitting retired at day 3 with nothing
# downstream able to reach them.
#
# The window is what keeps the recovery from being unbounded. A batch abandoned a
# month ago was abandoned for a reason nobody has looked at yet, and a deploy that
# quietly put forty phones back on the feed for it would be a worse surprise than
# the stall. Past this, the profile is reported for a person to decide about.
CATCHUP_DAYS = 14


@dataclass
class WarmupTarget:
    """One profile to warm up, resolved across both systems."""
    serial_no: str
    name: str                    # MLX serial_name, e.g. "Blank (12)"
    launch_id: str               # 18-digit MLX API ID -- the launch/ADB key
    record_id: str               # Airtable Profiles (Cloning) row
    day: int                     # campaign day, 1-based
    started: str | None = None   # Warm-up Started, ISO; None = starts today
    actions: list = dc_field(default_factory=list)
    # The plan day this run is actually doing, when that is not the calendar day
    # -- i.e. a catch-up (see CATCHUP_DAYS). Left None on an ordinary run so the
    # caller can tell the two apart without re-deriving the decision; `day` keeps
    # meaning the calendar day, which is what `Warm-up Day` publishes.
    catchup_day: int | None = None

    @property
    def needs_start_date(self) -> bool:
        """True when this run is day 1 and the date still has to be stamped."""
        return not self.started


def _ran_today(target, completed) -> bool:
    """Has this profile already done a day's warm-up activity today?

    `completed` is keyed `(run key, flow)`, so asking it about one flow answers
    "did this exact run happen", not "has this phone already had its day". A
    catch-up needs the second question -- see the note at the call site.
    """
    keys = (_run_key(target), target.name)
    return any((key, flow) in (completed or set())
               for key in keys
               for flow in warmup_completion.WARMUP_ACTIVITY_FLOWS)


def _run_key(target) -> str:
    """How a profile identifies itself in the Run Log: name *and* serial.

    The name alone is not an identity in this workspace -- see the collision
    note in `plan_profile_warmup`. `warmup_progress` parses this back out, so
    the two must keep agreeing about the shape.
    """
    return f"{target.name} [{target.serial_no}]"


def split_run_key(value) -> tuple:
    """``"Blank (5) [262894]"`` -> ``("Blank (5)", "262894")``.

    A legacy row carrying only the name yields an empty serial rather than a
    guess, which is what lets the reader show it as history it cannot pin to
    one twin.
    """
    text = str(value or "").strip()
    if text.endswith("]") and " [" in text:
        name, _, serial = text[:-1].rpartition(" [")
        if serial.isdigit():
            return name.strip(), serial
    return text, ""


def has_tag(tags, tag: str = WARMUP_TAG) -> bool:
    wanted = str(tag).strip().lower()
    return any(str(t).strip().lower() == wanted for t in (tags or ()))


def _parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def collect_warmup_targets(mlx_items, profiles_by_serial, today: date | None = None,
                           tag: str = WARMUP_TAG) -> tuple[list, list]:
    """Resolve tagged MLX profiles against their Airtable rows.

    Returns ``(targets, skipped)``. A profile is skipped, with the reason a
    person can act on, when:

    - it is not tagged (the normal case -- reported only in the count, not
      row by row, or every run would log 100+ lines);
    - MLX has it but Airtable does not. That is the mlx-sync loop's job and it
      runs nightly, so a batch created today is genuinely not warmable yet;
    - the Airtable row has no MLX API ID, so nothing can launch it;
    - its Airtable Status is Inactive -- the one switch a person has to park a
      profile, honoured here exactly as the queue and pipeline honour it.
    """
    today = today or date.today()
    targets: list = []
    skipped: list = []

    for item in mlx_items or []:
        profile = normalize_mlx_item(item)
        if profile is None:
            continue
        if not has_tag(profile.tags, tag):
            continue

        row = (profiles_by_serial or {}).get(profile.serial_no)
        if not row:
            skipped.append(SkippedAccount(
                profile.name,
                f"tagged {tag} in MultiLogin but has no Profiles (Cloning) row yet "
                "-- run the mlx-sync loop"))
            continue
        if not row.get("api_id"):
            skipped.append(SkippedAccount(profile.name, "no MLX API ID on the Airtable row"))
            continue
        status = row.get("status")
        if status is not None and status == "Inactive":
            skipped.append(SkippedAccount(profile.name, "Airtable Status is Inactive"))
            continue
        if row.get("needs_human"):
            # Honoured the way `posting_planner` honours it. Flagging does not
            # set Status, so without this a phone somebody has flagged keeps
            # being handed seventeen minutes of scrolling every hour while it
            # waits for them -- and, worse, can finish its plan and arrive on
            # the hand-off worklist as ready. On 2026-08-11 fourteen warm-up
            # profiles wore a hand-applied `Issue` tag and one of them did
            # exactly that.
            skipped.append(SkippedAccount(
                profile.name, "flagged in Airtable (Needs Human Check) -- waiting on a person"))
            continue

        started = _parse_date(row.get("warmup_started"))
        # No start date yet -> this run is day 1, and the caller stamps today.
        day = lifecycle.day_number(started, today) if started else 1
        targets.append(WarmupTarget(
            serial_no=profile.serial_no,
            name=row.get("name") or profile.name,
            launch_id=row["api_id"],
            record_id=row["record_id"],
            day=day,
            started=row.get("warmup_started"),
        ))

    targets.sort(key=lambda t: (t.name or "", t.serial_no))
    return targets, skipped


def plan_profile_warmup(mlx_items, profiles_by_serial, today: date | None = None,
                        tag: str = WARMUP_TAG, warmup_plan: dict | None = None,
                        completed: set | None = None,
                        selected_launch_ids=None, limit: int | None = None,
                        attempted: set | None = None,
                        day_done_by_serial: dict | None = None) -> AirtablePlan:
    """The warm-up run plan for tagged profiles, in the shape the runner takes.

    Deliberately returns an :class:`AirtablePlan` of :class:`AccountPlan`s with
    ``account_id=None``: `run_airtable_queue` already handles a plan entry with
    no Accounts row (it writes an unlinked Run Log), so the profile-driven
    warm-up reuses the whole runner rather than growing a second one.

    `warmup_plan` is the client's Warmup Plan table; without it the built-in
    schedule in lifecycle.py applies. `completed` is
    ``{(profile name, flow)}`` from today's Run Log, which is what keeps an
    hourly timer from running the same day's warm-up over and over.

    `limit` caps how many profiles **one tick** takes. A warm-up costs ~17
    minutes of phone, and the phone ceiling (12) is shared with posting -- so an
    uncapped run of 45 profiles at the default concurrency of 10 would hold most
    of the fleet for an hour and a half and leave the posting loop two slots.
    The hourly timer is what gets through the fleet; one tick only has to take a
    bite. Everything past the cap is reported as deferred, never dropped
    silently.

    `attempted` is ``{(key, flow)}`` for every run *logged today whatever its
    result*, and it orders that bite. Without it the cap would take the same
    profiles every tick: a failure does not count as completed, and the order is
    stable, so three profiles failing at the head of the list would consume the
    whole cap all day and the tail would never run at all.

    `day_done_by_serial` is ``{serial: plan days actually completed}``, from
    `warmup_completion.day_done_by_serial`, and it is what turns catch-up on: a
    profile whose calendar has run past the plan while it still owes a gating day
    is planned that owed day instead of the calendar day, until CATCHUP_DAYS runs
    out. Passing None (the default) is not "assume nothing is done" but "do not
    ask": the planner then behaves exactly as it did before catch-up existed --
    calendar day only, and past the end of the plan the profile is retired. That
    is deliberate, because catch-up needs the whole Run Log to decide anything,
    and a caller that cannot read it must not plan as if every day were owed.
    """
    today = today or date.today()
    completed = completed or set()
    plan = AirtablePlan()

    # The day whose completion ends the warm-up -- 0 when the plan is unreadable
    # or was not supplied, which `warmup_completion` treats as "nobody is
    # finished" and which here means "no catch-up", for the same reason: a plan
    # nobody could read is not grounds for re-running days against it.
    finish = warmup_completion.finish_day(warmup_plan) if day_done_by_serial is not None else 0

    targets, skipped = collect_warmup_targets(mlx_items, profiles_by_serial, today=today, tag=tag)
    plan.skipped.extend(skipped)

    for target in targets:
        if selected_launch_ids and target.launch_id not in selected_launch_ids:
            continue

        # Past the end of the plan with days still owed, the day of *work* is
        # what gets planned, not the day of the calendar. The lowest owed day
        # first: the plan is an order, and a profile that missed days 3 and 4
        # does day 3 next, not the day it happens to have arrived at.
        plan_day = target.day
        if finish and target.day > finish:
            done_days = int((day_done_by_serial or {}).get(target.serial_no) or 0)
            if not warmup_completion.is_finished(done_days, finish):
                if target.day - finish > CATCHUP_DAYS:
                    plan.skipped.append(SkippedAccount(
                        target.name,
                        f"day {target.day}: stopped at day {done_days} of {finish} and is more "
                        f"than {CATCHUP_DAYS} days past the plan -- needs a human"))
                    continue
                owed = [d for d in warmup_completion.gating_days(warmup_plan) if d > done_days]
                # One owed day per calendar day, like every other day of the
                # plan. The "already run today" guard further down is keyed on
                # (profile, flow), which is right for an ordinary day but not
                # here: consecutive owed days can want different flows, so a
                # profile that caught up day 3 with `warm_up_process` at 10:00
                # would be handed day 4's `instagram_scroll` at 11:00 and scroll
                # the same warming account twice in an hour. `day_done` credits
                # at most one plan day per date anyway, so the second run's Done
                # row would be discarded and the day re-run tomorrow regardless.
                if owed and not _ran_today(target, completed):
                    plan_day = target.catchup_day = owed[0]
                elif owed:
                    plan.skipped.append(SkippedAccount(
                        target.name,
                        f"catching up day {owed[0]} of the plan: already ran today"))
                    continue

        if warmup_plan:
            actions, _warnings = lifecycle.plan_actions_from_table(plan_day, warmup_plan)
        else:
            # The built-in plan, through lifecycle's own day arithmetic: hand it
            # a start date that puts `today` on this target's day number.
            actions = lifecycle.plan_actions_for_day(
                today - timedelta(days=plan_day - 1), today)

        # Is this day still inside the warm-up at all? Past the end, the built-in
        # plan answers with the posting day's reels rather than with nothing, so
        # "no warm-up actions" alone cannot tell "finished" from "a reel day".
        last_day = max(warmup_plan) if warmup_plan else lifecycle.WARMUP_DAYS
        within_warmup = plan_day <= last_day

        # Every skip below names the day it is talking about, and on a catch-up
        # that is the plan day, not the calendar day -- a note reading "day 6"
        # for a run doing day 4's scroll is how the two got confused in the first
        # place.
        day_note = (f"catching up day {plan_day} of the plan" if target.catchup_day
                    else f"day {plan_day}")

        # Reels are not part of a profile warm-up, whichever plan asked for one.
        # A reel needs a spoofed variant, and a variant needs a model -- which is
        # exactly what a `Created` profile has not got yet ("Blank (12)" belongs
        # to nobody). Posting is the Posting Queue's job once the profile has
        # been named and assigned; scheduling it here would only produce runs
        # that fail for want of media.
        reels = [a for a in actions if a.flow == lifecycle.FLOW_REEL]
        actions = [a for a in actions if a.flow != lifecycle.FLOW_REEL]
        if reels and within_warmup:
            plan.skipped.append(SkippedAccount(
                target.name, f"{day_note}: the plan's reel is left to the Posting Queue"))

        if not actions:
            if not (reels and within_warmup):
                plan.skipped.append(SkippedAccount(
                    target.name,
                    f"{day_note}: warm-up finished -- retag it in MultiLogin"))
            continue

        # MLX names are not unique -- this workspace has three profiles called
        # "Blank (5)", two called "Blank (1)", and 17 profiles sit in a
        # collision group. Both the Run Log's Name and the "already run today"
        # key are built from that name, so on the bare name the first twin to
        # run marks all of them done for the day; and since the order is stable,
        # the same twin wins every day and the others never run at all. The
        # serial is what `warmup_profiles_by_serial` already matches on, so it
        # is what identifies a run here too.
        run_key = _run_key(target)
        # Rows written before this change carry the bare name, and treating one
        # of those as covering its twins is the conservative reading: it costs a
        # twin one day, where the alternative is warming the same profile twice.
        # Self-healing -- every row written from now on is serial-qualified.
        done = {run_key, target.name}
        runs = [FlowRun(a.flow, f"{a.label} [{target.name}]") for a in actions
                if not any((key, a.flow) in completed for key in done)]
        if not runs:
            plan.skipped.append(SkippedAccount(target.name, f"{day_note}: already run today"))
            continue

        entry = AccountPlan(None, run_key, target.launch_id, target.name, runs)
        # Carried so the caller can stamp day 1 without re-resolving anything.
        entry.warmup_target = target
        plan.plans.append(entry)

    if limit is not None and limit > 0 and len(plan.plans) > limit:
        attempted = attempted or set()
        # Anything already tried today goes to the back, so a profile that fails
        # every tick cannot hold the cap against the ones that have had no turn.
        def tried(entry) -> bool:
            target = entry.warmup_target
            return any((key, run.flow) in attempted
                       for key in (_run_key(target), target.name)
                       for run in entry.runs)

        plan.plans.sort(key=tried)
        deferred = plan.plans[limit:]
        plan.plans = plan.plans[:limit]
        for entry in deferred:
            plan.skipped.append(SkippedAccount(
                entry.profile_name,
                f"deferred to the next tick: this run is capped at {limit} profile(s)"))

    return plan


def stamp_started(airtable, plan, today: date | None = None, logger=None) -> int:
    """Write `Warm-up Started` for every day-1 profile in `plan`. Returns how
    many were stamped.

    Called once the run is committed to (under --apply), not at plan time: a
    dry-run must not silently consume a profile's day 1.
    """
    today = today or date.today()
    stamped = 0
    for entry in getattr(plan, "plans", []) or []:
        target = getattr(entry, "warmup_target", None)
        if target is None or not target.needs_start_date:
            continue
        if airtable.set_warmup_started(target.record_id, today.isoformat()):
            stamped += 1
            if logger:
                logger.info("warmup: %s starts its warm-up today (day 1)", target.name)
        elif logger:
            logger.warning("warmup: could not stamp the start date for %s", target.name)
    return stamped
