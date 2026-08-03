"""Turn the normalized Airtable base into a per-account run plan.

Reads Accounts (+ their linked Profile for the launch key), applies the skip
guards (paused / banned / needs-verification / missing data), and asks the
lifecycle engine which flow(s) are due for each account today. Pure planning: it
does no launching or device work, so it is easy to reason about and test.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time as dtime

from adb_bot.automation import lifecycle
from adb_bot.automation import attachments
from adb_bot.clients import airtable as at


@dataclass
class FlowRun:
    flow: str
    label: str
    scheduled_time: str | None = None
    bio: str | None = None
    caption: str | None = None
    picture: str | None = None


@dataclass
class AccountPlan:
    account_id: str
    account_name: str
    launch_id: str            # 18-digit MLX API ID -- the launch/ADB key
    profile_name: str | None
    runs: list = dc_field(default_factory=list)


@dataclass
class SkippedAccount:
    account_name: str
    reason: str


@dataclass
class AirtablePlan:
    plans: list = dc_field(default_factory=list)
    skipped: list = dc_field(default_factory=list)


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _parse_hhmm(value):
    try:
        hh, mm = str(value).split(":")[:2]
        return dtime(int(hh), int(mm))
    except Exception:
        return None


def plan_airtable_runs(
    airtable,
    today=None,
    now_time=None,
    logger=None,
    run_reels: bool = True,
    selected_launch_ids=None,
    override_flow: str | None = None,
    override_bio: str | None = None,
    override_caption: str | None = None,
    override_picture: str | None = None,
) -> AirtablePlan:
    """Build the run plan. `run_reels` defaults to True now that reel media is
    wired (Drive -> spoof pipeline -> Spoof Variants); pass False to skip reel
    actions. Reels carrying a scheduled time are also time-gated to that slot,
    so running in the morning doesn't fire the whole day at once.

    `selected_launch_ids` (a set of 18-digit MLX API IDs) restricts the run to
    accounts whose profile is in that set; None/empty means run every account.

    `override_flow`: when set, every in-scope account runs that single UI-chosen
    flow (with the UI-supplied bio/caption/picture) instead of its lifecycle
    flow -- the creation-date requirement, idempotency and reel gating are all
    bypassed, but the health guards (paused / banned / needs-verification) and
    the launch-key requirement still apply."""
    today = today or date.today()
    now_time = now_time if now_time is not None else datetime.now().time()

    accounts = airtable.list_accounts()
    profiles = airtable.profile_launch_map()
    try:
        completed = airtable.todays_completed_runs()
    except Exception:
        completed = set()

    # The client edits the Warmup Plan table to control the warm-up. Read it
    # fresh each run; an empty/missing table falls back to the built-in
    # schedule so a base without the table behaves exactly as before.
    warmup_plan = {}
    if not override_flow:
        try:
            warmup_plan = airtable.warmup_plan_by_day() or {}
        except Exception as exc:
            if logger:
                logger.warning("Could not read the Warmup Plan table (%s); using the built-in schedule", exc)
    if logger:
        logger.info(
            "Warm-up schedule: %s",
            f"Warmup Plan table, day(s) {sorted(warmup_plan)}" if warmup_plan
            else "built-in (lifecycle.py) -- Warmup Plan table empty or unavailable",
        )
    reported_plan_warnings: set = set()

    # Reverse map so we can report selected profiles that match no Account.
    launch_id_to_name = {}
    for info in profiles.values():
        lid = info.get("launch_id")
        if lid:
            launch_id_to_name.setdefault(lid, info.get("name"))
    matched_launch_ids = set()

    plan = AirtablePlan()

    for record in accounts:
        fields = record.get("fields", {}) or {}
        account_id = record.get("id")
        name = str(fields.get(at.F_ACC_NAME) or "").strip()

        # Skip fully-empty placeholder rows silently.
        if not name and not fields.get(at.F_ACC_PROFILE):
            continue
        display_name = name or account_id

        # Resolve the launch key (linked Profile -> MLX API ID) up front so the
        # selection filter below can use it.
        profile_links = fields.get(at.F_ACC_PROFILE) or []
        launch_id = None
        profile_name = None
        if profile_links:
            info = profiles.get(profile_links[0])
            if info:
                launch_id = info.get("launch_id")
                profile_name = info.get("name")

        # Selection filter: when a set of selected launch ids is given, silently
        # exclude every account whose profile isn't in it (None/empty = run all).
        if selected_launch_ids:
            if not launch_id or launch_id not in selected_launch_ids:
                continue
            matched_launch_ids.add(launch_id)

        # --- skip guards ---
        if bool(fields.get(at.F_ACC_NEEDS_VERIFICATION)):
            plan.skipped.append(SkippedAccount(display_name, "needs human verification"))
            continue
        if at._select_name(fields.get(at.F_ACC_AUTOMATION_MODE)) == at.MODE_PAUSED:
            plan.skipped.append(SkippedAccount(display_name, "automation mode paused"))
            continue
        stage = at._select_name(fields.get(at.F_ACC_LIFECYCLE_STAGE))
        if stage in (at.STAGE_PAUSED, at.STAGE_BANNED):
            plan.skipped.append(SkippedAccount(display_name, f"lifecycle stage {stage}"))
            continue

        if not launch_id:
            plan.skipped.append(SkippedAccount(display_name, "no MLX API ID on linked profile"))
            continue

        # --- override: run the UI-selected flow instead of the lifecycle one ---
        if override_flow:
            plan.plans.append(AccountPlan(
                account_id, display_name, launch_id, profile_name,
                [FlowRun(
                    override_flow, f"UI flow: {override_flow}",
                    bio=override_bio, caption=override_caption, picture=override_picture,
                )],
            ))
            continue

        # --- lifecycle: which flows are due today ---
        start_date = _parse_iso_date(fields.get(at.F_ACC_CREATION_DATE))
        if start_date is None:
            plan.skipped.append(SkippedAccount(display_name, "no creation date"))
            continue

        day = lifecycle.day_number(start_date, today)
        if warmup_plan:
            actions, warnings = lifecycle.plan_actions_from_table(day, warmup_plan)
            for warning in warnings:
                if warning not in reported_plan_warnings:
                    reported_plan_warnings.add(warning)   # once per run, not per account
                    if logger:
                        logger.warning("Warmup Plan: %s", warning)
        else:
            actions = lifecycle.plan_actions_for_day(start_date, today)
        runs = []
        for action in actions:
            if action.flow == lifecycle.FLOW_REEL:
                if not run_reels:
                    continue  # caller explicitly disabled reels (--no-reels)
                sched = _parse_hhmm(action.scheduled_time)
                if sched is not None and now_time < sched:
                    continue  # not time for this reel slot yet
            if (account_id, action.flow) in completed:
                continue  # already ran to Done/Running today (idempotent)
            bio = None
            if action.flow == lifecycle.FLOW_UPDATE_BIO:
                bio = (str(fields.get(at.F_ACC_BIO) or "").strip() or None)
            picture = None
            if action.flow == lifecycle.FLOW_UPDATE_PICTURE:
                # Carry the attachment URL; the runner downloads it to a local
                # file right before the flow runs (Airtable URLs are temporary).
                picture = attachments.first_attachment_url(fields.get(at.F_ACC_PROFILE_PICTURE))
            runs.append(FlowRun(action.flow, action.label, action.scheduled_time, bio=bio, picture=picture))

        if runs:
            plan.plans.append(AccountPlan(account_id, display_name, launch_id, profile_name, runs))

    # Tell the user about selected profiles that aren't linked to any Account.
    if selected_launch_ids:
        for lid in set(selected_launch_ids) - matched_launch_ids:
            plan.skipped.append(SkippedAccount(
                launch_id_to_name.get(lid, lid), "selected profile has no linked Account"
            ))

    return plan
