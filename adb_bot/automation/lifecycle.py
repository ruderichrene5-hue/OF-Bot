"""Per-profile lifecycle planner (the "decision engine").

Given a profile's campaign start date and the current date/time, this computes
which flow(s) are due. It is pure logic -- no device, network, or Airtable
access -- so it is trivially testable and safe to build ahead of the scheduler
that will call it.

Campaign rules (from the client's plan):
- Days 1-5: run the warm-up flow once per day.
- Day 6 onward: post reels, 3 per day, at scheduled times.

The warm-up used to also set the profile picture and the bio on one of its days.
It no longer does (2026-08-06, client's call): those two are set up outside the
warm-up now. Both flows still exist and still run when the UI asks for them, or
when the client ticks them on a Warmup Plan row -- the warm-up just stops
scheduling them by itself.

The scheduler is responsible for *when* to call this and for not repeating an
action already done (idempotency lives in the runner/Airtable state, not here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

WARMUP_DAYS = 5
DEFAULT_REEL_TIMES = ("09:00", "14:00", "19:00")  # 3 reels/day once posting begins

# Flow identifiers. These are the uiautomator2 implementations -- the same ones
# the UI offers -- so a scheduled run and a manual run drive the phone the same
# way. The older screen-dump flows (`update_bio`, `instagram_reel_upload`) are
# still registered and still run if an existing Airtable row names them.
FLOW_WARMUP = "warm_up_process"
FLOW_UPDATE_PICTURE = "update_profile_picture"
FLOW_UPDATE_BIO = "update_bio_u2"
FLOW_REEL = "instagram_reel_upload_u2"

STAGE_NOT_STARTED = "not_started"
STAGE_WARMUP = "warmup"
STAGE_POSTING = "posting"


@dataclass(frozen=True)
class PlannedAction:
    flow: str
    label: str
    scheduled_time: str | None = None  # "HH:MM" for reels; None for once-a-day actions


def day_number(start_date: date, today: date) -> int:
    """1-indexed campaign day. Day 1 is the start date; <1 means not started."""
    return (today - start_date).days + 1


def campaign_stage(start_date: date, today: date) -> str:
    day = day_number(start_date, today)
    if day < 1:
        return STAGE_NOT_STARTED
    if day <= WARMUP_DAYS:
        return STAGE_WARMUP
    return STAGE_POSTING


def plan_actions_for_day(
    start_date: date,
    today: date,
    reel_times: tuple[str, ...] = DEFAULT_REEL_TIMES,
) -> list[PlannedAction]:
    """Return every action due on `today` for a profile that started on
    `start_date`. Reel actions carry their scheduled time; daily actions don't."""
    day = day_number(start_date, today)
    if day < 1:
        return []

    actions: list[PlannedAction] = []
    if day <= WARMUP_DAYS:
        # Warm-up only. Profile picture and bio are deliberately not scheduled
        # here any more -- see the module docstring.
        actions.append(PlannedAction(FLOW_WARMUP, f"Warm-up (day {day} of {WARMUP_DAYS})"))
    else:
        for index, when in enumerate(reel_times, start=1):
            actions.append(PlannedAction(FLOW_REEL, f"Post reel {index} of {len(reel_times)}", scheduled_time=when))
    return actions


# Flow used when a plan row asks for scrolling but not following. The warm-up
# flow always does both, so it would over-deliver on a scroll-only day.
FLOW_SCROLL_ONLY = "instagram_scroll"

# Plan-row capabilities with no flow behind them. Surfaced as warnings rather
# than dropped, so a row asking for something the bot cannot do is visible
# instead of silently doing nothing.
UNSUPPORTED_PLAN_KEYS = {"feed_posts": "feed posts"}


def plan_actions_from_row(day: int, row: dict) -> tuple[list[PlannedAction], list[str]]:
    """Translate one Warmup Plan row into actions, plus warnings for anything
    the row asks for that no flow implements.

    Kept pure (no Airtable, no device) so the mapping is testable on its own.
    The checkbox -> flow mapping:

      Scroll + Follow People -> warm_up_process   (that flow does both)
      Scroll only            -> instagram_scroll
      Follow only            -> warm_up_process   (no follow-only flow exists)
      Profile Picture Update -> update_profile_picture
      Bio Update             -> update_bio_u2
      Reel Post              -> instagram_reel_upload_u2
      Feed Posts             -> nothing; warned
    """
    actions: list[PlannedAction] = []
    warnings: list[str] = []

    scroll, follow = bool(row.get("scroll")), bool(row.get("follow"))
    if scroll and follow:
        actions.append(PlannedAction(FLOW_WARMUP, f"Warm-up (day {day}: scroll + follow)"))
    elif follow:
        actions.append(PlannedAction(FLOW_WARMUP, f"Warm-up (day {day}: follow)"))
    elif scroll:
        actions.append(PlannedAction(FLOW_SCROLL_ONLY, f"Scroll feed (day {day})"))

    if row.get("picture"):
        actions.append(PlannedAction(FLOW_UPDATE_PICTURE, f"Update profile picture (day {day})"))
    if row.get("bio"):
        actions.append(PlannedAction(FLOW_UPDATE_BIO, f"Update bio (day {day})"))
    if row.get("reel"):
        actions.append(PlannedAction(FLOW_REEL, f"Post reel (day {day})"))

    for key, label in UNSUPPORTED_PLAN_KEYS.items():
        if row.get(key):
            warnings.append(
                f"day {day} asks for {row[key]} {label}, which no flow implements -- ignored"
            )
    return actions, warnings


def plan_actions_from_table(day: int, plan_by_day: dict) -> tuple[list[PlannedAction], list[str]]:
    """Actions for `day` from the client's Warmup Plan table.

    A day past the end of the table means warm-up is over: no actions. Posting
    from then on is driven by the Posting Queue (the client's Airtable
    automations create those rows), NOT by this planner -- scheduling reels here
    as well would post twice.
    """
    if day < 1:
        return [], []
    row = plan_by_day.get(day)
    if row is None:
        return [], []
    return plan_actions_from_row(day, row)


def describe_campaign(
    start_date: date,
    num_days: int = 7,
    reel_times: tuple[str, ...] = DEFAULT_REEL_TIMES,
) -> list[dict]:
    """A day-by-day preview of the plan, for dashboards/reports. Returns a list
    of {day, stage, actions:[{flow,label,scheduled_time}]}."""
    from datetime import timedelta

    preview: list[dict] = []
    for offset in range(num_days):
        day_date = start_date + timedelta(days=offset)
        actions = plan_actions_for_day(start_date, day_date, reel_times=reel_times)
        preview.append({
            "day": day_number(start_date, day_date),
            "stage": campaign_stage(start_date, day_date),
            "actions": [
                {"flow": a.flow, "label": a.label, "scheduled_time": a.scheduled_time}
                for a in actions
            ],
        })
    return preview
