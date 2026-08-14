"""Publish where each profile stands in its warm-up, into Airtable and MultiLogin.

Until now the only place the campaign was legible was the dashboard, which
recomputes it from the Run Log on every page load. Nothing was written down, so
the two tools people actually work in were blind: Airtable knew only the date a
profile started, and the MultiLogin workspace had a full set of warm-up tags --
`Warming up process not startet`, `Warmup Day 1..4 Done`, `Warmup ready, need
Bio and Pic` -- sitting at ``in_use_count: 0``, made by hand for a bot that
never wrote one.

This closes both. It is a **reconciler**, not a callback on the run: it reads
the same history the dashboard reads and makes the two systems agree with it.
That matters more than it sounds --

* a run that dies between finishing and writing its tag is corrected on the next
  sweep rather than leaving the profile mislabelled until someone notices;
* it is idempotent, so it can run after every warm-up tick *and* on its own
  timer without double-counting anything;
* it fixes up history, which is what lets today's sweep label 46 profiles that
  did their runs before any of this existed.

**The day it publishes is the day *completed*, not the calendar day.** Those
diverge the moment a run fails: a profile advances a day at midnight whether or
not last night's run worked, so a phone that has been failing since day 1 is on
"day 4" and has "day 1 done". Tagging it `Warmup Day 4 Done` would hide exactly
the profile a person opens MultiLogin to find. `Warm-up Day` carries the
calendar day and `Warm-up Stage` carries the completed one, and the pair being
different is the signal.

`Created` is never touched. It is the population selector -- `warmup_targets`
picks profiles by it -- and it is a human's field: dropping it because the bot
believes a profile is finished would remove that profile from the warm-up for
good if the belief were ever wrong. A finished profile gets `Warmup ready, need
Bio and Pic` added alongside, and a person retires `Created` when they act on it.

That last sentence is why this pass can refuse to run. Retiring `Created` is
irreversible from here, and the finished tag is what asks for it, so a sweep
that would move more than `MAX_STAGE_CHANGES_DEFAULT` stages at once writes
nothing and reports the diff instead of relabelling the workspace unattended.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from adb_bot.automation import warmup_completion
from adb_bot.clients import airtable as at

# The tags already in the workspace, with the colours they were given there.
# Matched case-insensitively on assignment, so the spelling here only decides
# what a *newly created* tag would look like -- these all exist already.
TAG_NOT_STARTED = "Warming up process not startet"
TAG_DAY_DONE = "Warmup Day {day} Done"
TAG_FINISHED = "Warmup ready, need Bio and Pic"

# The colours these tags already carry in the workspace. Only consulted when a
# tag has to be *created*, which on this workspace is never -- but a fresh
# workspace should come out looking the same.
TAG_COLORS = {
    TAG_NOT_STARTED: "blue",
    TAG_FINISHED: "green",
    "Warmup Day 1 Done": "green",
    "Warmup Day 2 Done": "blue",
    "Warmup Day 3 Done": "teal",
    "Warmup Day 4 Done": "orange",
}
# Past day 4 the workspace has no per-day tag. Rather than invent `Warmup Day 5
# Done` in someone else's vocabulary, a plan longer than the tags it has falls
# back to the day tags it does have and reports the shortfall.
MAX_DAY_TAG = 4

# How many profiles may have their **stage** moved by one unattended sweep
# before the pass refuses to write at all.
#
# `Warmup ready, need Bio and Pic` is an instruction to a person: do the bio,
# the picture and the first post, then drop `Created`. Once `Created` is gone
# `warmup_targets.collect_warmup_targets` never sees that profile again and no
# loop puts it back -- it is the one step here the bot cannot undo, and this
# runs on a 30-minute timer with nobody watching. A changed definition of
# "finished", or one bad read of the Warmup Plan table, could otherwise relabel
# the whole workspace before anyone opened it.
#
# 10 is measured, not round: the first sweep after the day-4 fix wants to move
# nine stages (seven profiles from `Warmup Day 3 Done` back to day 2, two back
# to day 1 -- profiles that genuinely missed days, which the old calendar
# attribution flattered). So the real correction goes through unattended and
# anything an order of magnitude larger stops and asks.
MAX_STAGE_CHANGES_DEFAULT = 10


def day_tag(day: int) -> str:
    return TAG_DAY_DONE.format(day=day)


def owned_tags(plan_days: int = MAX_DAY_TAG) -> list:
    """Every tag this module writes -- and therefore every tag it may remove.

    A profile's warm-up tag is exclusive: it carries exactly one of these or
    none. Anything outside this set (`Created`, `Issue`, `gmail`, `2 accounts`)
    is somebody else's and is left where it is.
    """
    days = min(max(int(plan_days or 0), 0), MAX_DAY_TAG)
    return [TAG_NOT_STARTED] + [day_tag(d) for d in range(1, days + 1)] + [TAG_FINISHED]


def stage_for(day: int, day_done: int, plan_days: int, finish_day=None) -> str:
    """The one warm-up tag a profile should be carrying.

    Off `day_done`, never `day`. A profile whose calendar day has run past the
    plan without the runs landing is not ready for a bio and a picture -- it is
    the profile someone needs to look at, and labelling it finished is how it
    would stop being found.

    Two different questions hide in the plan's length, and they used to share
    one number. *Which day finishes the warm-up* is `finish_day` -- the last
    plan day that asks for warm-up activity, per `warmup_completion`; it can be
    shorter than the plan when the trailing days ask only for a picture, a bio
    or a reel, none of which the bot can finish on its own. *Which tags the
    workspace has* is `plan_days` against `MAX_DAY_TAG`, and that stays where it
    is: it decides what a day tag may be called, not whether anything is done.
    `finish_day` of None keeps the old reading for callers that have not been
    given the plan's shape, and 0 -- an unreadable Warmup Plan -- calls nobody
    finished rather than retagging a fleet against a plan nobody chose.
    """
    finish = plan_days if finish_day is None else int(finish_day)
    if warmup_completion.is_finished(day_done, finish):
        return TAG_FINISHED
    if day_done <= 0:
        return TAG_NOT_STARTED
    return day_tag(min(day_done, MAX_DAY_TAG))


@dataclass
class ProfileState:
    """What one profile's two systems should say, and what they say now."""
    record_id: str
    launch_id: str
    name: str
    serial: str
    day: int
    day_done: int
    stage: str
    last_run: str = ""
    last_result: str = ""
    runs_done: int = 0
    current_tags: tuple = ()

    def airtable_fields(self) -> dict:
        return {
            at.F_PROF_WARMUP_DAY: self.day,
            at.F_PROF_WARMUP_STAGE: self.stage,
            at.F_PROF_WARMUP_RUNS_DONE: self.runs_done,
            at.F_PROF_WARMUP_LAST_RUN: self.last_run or None,
            at.F_PROF_WARMUP_LAST_RESULT: self.last_result or None,
        }

    def airtable_differs(self, current: dict | None) -> bool:
        """True when Airtable does not already say this.

        Without the comparison an hourly sweep rewrites every row identically
        and stamps a fresh Last Modified on all 46, which is exactly the column
        someone would use to find the profile that actually moved.
        """
        if current is None:
            return True
        for field, value in self.airtable_fields().items():
            if _same(current.get(field), value):
                continue
            return True
        return False

    def tag_changes(self, plan_days: int) -> tuple:
        """``(add, remove)`` tag names, both empty when MLX already agrees."""
        owned = {t.lower() for t in owned_tags(plan_days)}
        held = {str(t).strip() for t in (self.current_tags or ()) if str(t).strip()}
        wanted = self.stage
        remove = [t for t in held if t.lower() in owned and t.lower() != wanted.lower()]
        add = [] if any(t.lower() == wanted.lower() for t in held) else [wanted]
        return add, remove


def _same(current, wanted) -> bool:
    """Airtable's reading of a value against ours. Blank in any spelling is one
    value; a number that came back as 3.0 is still 3."""
    if current in (None, "") and wanted in (None, ""):
        return True
    if isinstance(current, (int, float)) and isinstance(wanted, (int, float)):
        return float(current) == float(wanted)
    return str(current).strip() == str(wanted).strip()


@dataclass
class SyncReport:
    checked: int = 0
    airtable_written: int = 0
    tags_written: int = 0
    unchanged: int = 0
    errors: list = dc_field(default_factory=list)
    changes: list = dc_field(default_factory=list)

    def summary(self) -> str:
        return (f"checked={self.checked} airtable={self.airtable_written} "
                f"tags={self.tags_written} unchanged={self.unchanged} "
                f"errors={len(self.errors)}")


def build_states(progress: dict, tags_by_launch_id: dict | None = None) -> list:
    """`report.warmup_progress` output -> one :class:`ProfileState` per profile.

    `finish_day` is carried separately from `plan_days` and passed straight
    through, so the tag a profile gets and the number the dashboard shows come
    from one reading of the plan. A progress dict from before that key existed
    leaves it None, which is the old behaviour rather than "nobody is finished".
    """
    plan_days = int(progress.get("plan_days") or 0)
    finish_day = progress.get("finish_day")
    tags_by_launch_id = tags_by_launch_id or {}
    states = []
    for row in progress.get("profiles") or []:
        if not row.get("record_id"):
            continue
        day = int(row.get("day") or 0)
        day_done = int(row.get("day_done") or 0)
        states.append(ProfileState(
            record_id=row["record_id"],
            launch_id=str(row.get("launch_id") or ""),
            name=str(row.get("name") or ""),
            serial=str(row.get("serial") or ""),
            day=day,
            day_done=day_done,
            stage=stage_for(day, day_done, plan_days, finish_day),
            last_run=str(row.get("last_at_iso") or ""),
            last_result=str(row.get("last_result") or ""),
            runs_done=int(row.get("runs_done") or 0),
            current_tags=tuple(tags_by_launch_id.get(str(row.get("launch_id") or "")) or ()),
        ))
    return states


def tags_by_launch_id(mlx_items) -> dict:
    """``{18-digit MLX API ID: (tag, ...)}`` from an already-fetched inventory.

    Keyed on the API ID and not the serial because that is what the tag
    endpoints take as `profile_id`.
    """
    from adb_bot.automation.mlx_sync import normalize_mlx_item

    out: dict = {}
    for item in mlx_items or []:
        profile = normalize_mlx_item(item)
        if profile is None:
            continue
        out[str(profile.api_id)] = tuple(profile.tags or ())
    return out


def stage_moves(states, snapshot: dict | None) -> list:
    """`[(state, current_stage), ...]` -- the profiles whose stage would *move*.

    Deliberately narrower than `airtable_differs`. The run metadata (Runs Done,
    Last Run, Last Result) changes on nearly every row of the first sweep after
    the Run Log filter widens, and that is the sweep that fixes things: counting
    it would trip the breaker on exactly the pass it exists to let through. Only
    `Warm-up Stage` says "this profile is somewhere else in its warm-up now",
    and only that is counted.

    A row Airtable cannot state a stage for -- no snapshot at all, no row in it,
    or the field still blank because this is the first write -- is not a move.
    "No current stage" is not "the stage changed": the first sweep on a fresh
    column would otherwise read as the whole fleet moving and refuse to write
    the column it is there to fill.
    """
    moves = []
    for state in states:
        current = (snapshot or {}).get(state.record_id) or {}
        was = str(current.get(at.F_PROF_WARMUP_STAGE) or "").strip()
        if not was or was.lower() == str(state.stage).strip().lower():
            continue
        moves.append((state, was))
    return moves


def sync_warmup_state(airtable, progress: dict, tag_client=None, mlx_items=None,
                      dry_run: bool = False, logger=None,
                      max_stage_changes: int = MAX_STAGE_CHANGES_DEFAULT) -> SyncReport:
    """Make Airtable and MultiLogin agree with the Run Log. Idempotent.

    `tag_client` is optional: without one the Airtable half still runs, which is
    what keeps a MultiLogin outage from also costing the day numbers.

    `max_stage_changes` is the circuit breaker described at
    :data:`MAX_STAGE_CHANGES_DEFAULT`: past it this pass writes nothing at all
    -- no Airtable patch and no retag -- and reports what it would have done, so
    a person reads the diff before the workspace is relabelled. A dry run never
    trips, because a dry run *is* how you read the diff.
    """
    report = SyncReport()
    plan_days = int(progress.get("plan_days") or 0)
    if progress.get("error"):
        report.errors.append(str(progress["error"]))
        return report

    states = build_states(progress, tags_by_launch_id(mlx_items) if mlx_items else {})
    wanted_tag_ids: dict = {}

    # One list call for what Airtable already says. `None` means the fields are
    # missing, and then every row "differs" -- which is the right answer: the
    # first patch is what creates them via typecast.
    snapshot_failed = False
    try:
        snapshot = airtable.warmup_state_snapshot()
    except Exception as exc:
        report.errors.append(f"warm-up state snapshot: {type(exc).__name__}: {exc}")
        snapshot = None
        snapshot_failed = True

    # A snapshot that could not be *read* is not the same as a base that has no
    # stage to read, and the difference decides whether this pass may write.
    # Without it, every row "differs" (which is correct when the fields are
    # merely missing -- the first patch creates them) while `stage_moves` sees
    # no current stage anywhere and reports no moves: the breaker would go
    # quiet on precisely the pass it cannot measure, and one Airtable 429 would
    # relabel the whole fleet and strip its MultiLogin day tags unattended.
    if snapshot_failed and not dry_run:
        report.checked = len(states)
        report.errors.append(
            "refused to write: could not read the current warm-up state from "
            "Airtable, so there is no way to tell how many profiles this pass "
            "would move. Nothing was written; the next sweep will retry.")
        if logger:
            logger.error("warmup state: %s", report.errors[-1])
        return report

    # The breaker, before the first write of either kind: a refusal that had
    # already patched half the fleet would not be a refusal.
    moves = stage_moves(states, snapshot)
    limit = int(max_stage_changes)
    if not dry_run and len(moves) > limit:
        report.checked = len(states)
        for state, was in moves:
            report.changes.append(
                f"{state.name} [{state.serial}] {was} -> {state.stage}")
        report.errors.append(
            f"refused to write: {len(moves)} profiles would change warm-up stage "
            f"(limit {limit}). Nothing was written. Read the list with "
            f"`python -m adb_bot.automation.run_loop warmup-state`, then let it "
            f"through with `python -m adb_bot.automation.run_loop warmup-state "
            f"--apply --max-stage-changes {len(moves)}`")
        if logger:
            logger.error("warmup state: %s", report.errors[-1])
            # Every one of them, not the usual first 20: the list is the thing
            # the person has to read before deciding.
            for line in report.changes:
                logger.info("  would set %s", line)
        return report

    for state in states:
        report.checked += 1
        touched = False

        current = snapshot.get(state.record_id) if snapshot else None
        if state.airtable_differs(current):
            report.changes.append(
                f"{state.name} [{state.serial}] day {state.day}, {state.stage}")
            touched = True
            if not dry_run:
                try:
                    if airtable.set_warmup_state(state.record_id, state.airtable_fields()):
                        report.airtable_written += 1
                    else:
                        report.errors.append(f"{state.name}: Airtable patch refused")
                except Exception as exc:
                    report.errors.append(f"{state.name}: Airtable {type(exc).__name__}: {exc}")

        if tag_client is not None and state.launch_id:
            add, remove = state.tag_changes(plan_days)
            if add or remove:
                touched = True
                if not dry_run:
                    try:
                        # Resolved once per distinct tag name, not once per
                        # profile: 46 profiles share five tags between them.
                        for name in add + remove:
                            if name not in wanted_tag_ids:
                                wanted_tag_ids[name] = tag_client.ensure_tag(
                                    name, TAG_COLORS.get(name, "gray"))
                        tag_client.retag(
                            state.launch_id,
                            add=[wanted_tag_ids[n] for n in add if wanted_tag_ids.get(n)],
                            remove=[wanted_tag_ids[n] for n in remove if wanted_tag_ids.get(n)])
                        report.tags_written += 1
                    except Exception as exc:
                        report.errors.append(f"{state.name}: MLX {type(exc).__name__}: {exc}")

        if not touched:
            report.unchanged += 1

    if logger:
        logger.info("warmup state: %s", report.summary())
        for line in report.changes[:20]:
            logger.info("  tag %s", line)
        for line in report.errors[:10]:
            logger.warning("  %s", line)
    return report
