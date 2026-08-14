"""One definition of "this profile has finished its warm-up", for everything that asks.

Four places used to answer that question and they did not agree. The dashboard's
warm-up tab called a profile finished when the *calendar* ran past the plan; the
hand-off worklist wanted the plan's last day *completed*; `warmup_state` wrote
the MultiLogin tag off a third reading; and `warmup_targets` retired a profile
from the campaign off a fourth. On 2026-08-11 that cost 41 phones: the warm-up
tab counted them `finished: 41` while the worklist that tells a person to give
them a bio and a picture showed nobody, and the planner had already stopped
running them. Every one of those answers came from the same Run Log; only the
rule differed.

The rule, stated once:

    A profile has finished its warm-up when every plan day that asks for
    warm-up *activity* has been completed -- one day per calendar date, in
    order, from Done rows in the Run Log.

Two words in that sentence carry the weight.

**Activity.** A Warmup Plan row can ask for four things, and only two of them
are the warm-up: scrolling the feed (`instagram_scroll`) and scrolling plus
following (`warm_up_process`). The other two -- a profile picture and a bio --
are what a *person* does at the end, which is what the finished tag has always
said ("Warmup ready, need Bio and Pic"). The bot schedules them because the plan
table lists them, but it cannot do either without an input somebody has to
supply: as of 2026-08-11 the base holds 52 `update_profile_picture` rows and not
one is Done, every one skipped `no profile picture`. Gating completion on them
would mean no profile ever finishes, and the hand-off tab -- the very thing that
asks a person for the picture -- would stay empty waiting for the picture. So
they are reported, never required. `FLOW_REEL` is excluded for a different
reason: `warmup_targets` strips it because a reel belongs to the Posting Queue,
so requiring it would gate on a run the warm-up never makes.

**Completed, not elapsed.** A profile advances a calendar day at midnight
whether or not the night's run worked. Counting days completed is what separates
a profile that did its four days from one that did one day and then sat broken
for three -- and the second is the one somebody needs to find. `day` and
`day_done` diverging *is* the signal; see `report.warmup_progress`.

Nothing here touches Airtable or a phone: it takes the plan and the Run Log
rows, and returns numbers. That is deliberate -- it is imported by the report,
the tag writer, the planner and the runner, and a shared definition that needed
a client would end up restated in whichever of them could not have one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from adb_bot.automation import lifecycle

# Every flow the warm-up runner can be asked to run, which is what the Run Log
# has to be read for. Picture and bio are here because they are warm-up history
# worth showing -- `required_flows_by_day` is what decides they do not gate.
WARMUP_RUN_FLOWS = (
    lifecycle.FLOW_WARMUP,
    lifecycle.FLOW_SCROLL_ONLY,
    lifecycle.FLOW_UPDATE_PICTURE,
    lifecycle.FLOW_UPDATE_BIO,
)

# The subset that *is* the warm-up: time on the phone, in the feed. A plan day
# is complete when its activity is done, and a day that asks for no activity at
# all (a picture-only day, or the trailing reel day of some future plan) cannot
# hold the campaign open.
WARMUP_ACTIVITY_FLOWS = (lifecycle.FLOW_WARMUP, lifecycle.FLOW_SCROLL_ONLY)


def parse_run_at(value):
    """Airtable's ISO dateTime (UTC 'Z') as an aware datetime; None if unusable."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def required_flows_by_day(plan_by_day: dict | None) -> dict:
    """`{day: {flow, ...}}` -- the activity each plan day must complete.

    Runs each row through the same `lifecycle` mapping the planner uses, so a
    plan row can never mean one thing to the runner and another to the count of
    what it owes. Days asking for no activity map to an empty set and are
    skipped by everything below rather than dropped here, so `plan_days` (how
    long the plan is) stays a separate question from which days gate.
    """
    out: dict = {}
    for day, row in (plan_by_day or {}).items():
        try:
            number = int(day)
        except (TypeError, ValueError):
            continue
        if number < 1:
            continue
        actions, _warnings = lifecycle.plan_actions_from_row(number, row or {})
        out[number] = {a.flow for a in actions if a.flow in WARMUP_ACTIVITY_FLOWS}
    return out


def gating_days(plan_by_day: dict | None) -> list:
    """The plan days that must be completed, in order."""
    required = required_flows_by_day(plan_by_day)
    return [day for day in sorted(required) if required[day]]


def finish_day(plan_by_day: dict | None) -> int:
    """The plan day whose completion finishes the warm-up. 0 when unknowable.

    Zero is not "finished immediately" -- `is_finished` refuses everything on a
    zero, so a Warmup Plan table that fails to read declares nobody finished
    rather than quietly falling back to a built-in length and retagging a fleet
    against a plan nobody chose.
    """
    days = gating_days(plan_by_day)
    return days[-1] if days else 0


def is_finished(day_done_value: int, finish: int) -> bool:
    """The one test. `finish` of 0 means the plan is unknown: nothing is done."""
    return int(finish or 0) > 0 and int(day_done_value or 0) >= int(finish)


def history_by_key(rows) -> tuple[dict, dict]:
    """Run Log rows split into `(by_serial, by_name)`, newest first.

    `create_run_log` writes the Name as "<key> / <flow> / <when>", where the key
    is "<profile name> [<serial>]" for anything logged since 2026-08-07 and the
    bare profile name before that. MultiLogin names are not unique -- this
    workspace has three "Blank (5)" -- so a legacy row cannot be pinned to one
    twin and is kept under the name for the caller to fall back on.
    """
    from collections import defaultdict

    from adb_bot.automation import warmup_targets
    from adb_bot.clients import airtable as at

    by_serial: dict = defaultdict(list)
    by_name: dict = defaultdict(list)
    for record in rows or []:
        fields = record.get("fields", {}) or {}
        head = str(fields.get(at.F_RUN_NAME) or "").split(" / ")[0]
        name, serial = warmup_targets.split_run_key(head)
        entry = {
            "at": str(fields.get(at.F_RUN_AT) or ""),
            "result": at._select_name(fields.get(at.F_RUN_RESULT)) or "",
            "notes": str(fields.get(at.F_RUN_NOTES) or ""),
            # Carried because completion is per-flow now: two rows on one date
            # can be a retry of one day or two different days' work.
            "flow": at._select_name(fields.get(at.F_RUN_FLOW)) or "",
        }
        (by_serial[serial] if serial else by_name[name]).append(entry)
    return by_serial, by_name


def day_done(history, started, plan_by_day: dict | None) -> int:
    """How many of the plan's gating days this profile has actually completed.

    Walks the dates it ran, in order, and credits each date to the *next* day
    the plan still owes -- not to the calendar day the run landed on. The two
    only agree for a profile that never missed a day, and the difference is the
    whole point: a run on calendar day 6 that finally does the day-4 scroll has
    completed day 4, and crediting it "day 6" would call a profile finished for
    a day it skipped. One date can settle at most one plan day, so a retry after
    a partial run does not push a profile a day ahead of the plan.

    Rows dated before `Warm-up Started` belong to a previous life of the profile
    and are ignored; a missing start date means nothing can be attributed.
    """
    from collections import defaultdict

    from adb_bot.clients import airtable as at

    days = gating_days(plan_by_day)
    if not days or not started:
        return 0
    required = required_flows_by_day(plan_by_day)

    flows_by_date: dict = defaultdict(set)
    for entry in history or []:
        if entry.get("result") != at.RESULT_DONE:
            continue
        # Local, not UTC: `Warm-up Started` is stamped from the server's own
        # `date.today()`, so a run logged at 00:30 local would otherwise land on
        # the previous campaign day.
        stamp = parse_run_at(entry.get("at"))
        if not stamp:
            continue
        ran = stamp.astimezone().date()
        if ran < started:
            continue
        flows_by_date[ran].add(entry.get("flow") or "")

    progress = 0
    pending: set = set()
    for ran in sorted(flows_by_date):
        pending |= flows_by_date[ran]
        if progress < len(days) and required[days[progress]] <= pending:
            progress += 1
            pending = set()
    return days[progress - 1] if progress else 0


def day_done_by_serial(profiles_by_serial: dict | None, log_rows,
                       plan_by_day: dict | None) -> dict:
    """`{serial: day_done}` for every profile with an Airtable row.

    The runner needs the same number the dashboard shows, over the same rows.
    Deriving it here rather than in either of them is what stops the planner and
    the report drifting apart again.
    """
    from adb_bot.automation import warmup_targets

    by_serial, by_name = history_by_key(log_rows)
    out: dict = {}
    for serial, row in (profiles_by_serial or {}).items():
        history = by_serial.get(serial) or by_name.get(row.get("name") or "") or []
        started = warmup_targets._parse_date(row.get("warmup_started"))
        out[serial] = day_done(history, started, plan_by_day)
    return out
