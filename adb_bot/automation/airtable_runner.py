"""Process the Airtable "profile queue": read rows marked Ready, run each one's
flow on its Multilogin profile, and write the result back to Airtable.

This is UI-independent on purpose: the manual "Run from Airtable" button calls it
today, and the in-app scheduler (planned) will call the exact same function.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from adb_bot.clients.airtable import (
    AirtableClient,
    FIELD_BIO,
    FIELD_CAPTION,
    FIELD_FLOW,
    FIELD_LAST_RESULT,
    FIELD_LAST_RUN,
    FIELD_NOTES,
    FIELD_PROFILE_ID,
    FIELD_STATUS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
)
from adb_bot.automation.workflow import run_profile_workflow
from adb_bot.automation.airtable_planner import plan_airtable_runs
from adb_bot.automation import incidents
from adb_bot.automation import attachments
from adb_bot.core.locks import ProfileLocks
from adb_bot.core.batching import LaunchGate, resolve_concurrency, run_rolling
from adb_bot.config.settings import REEL_FLOWS
from adb_bot.clients.airtable import (
    RESULT_DONE,
    RESULT_FAILED,
    RESULT_RUNNING,
    RESULT_SKIPPED,
)

# Flow values that are safe to drive from Airtable in this pass (text flows).
VALID_FLOWS = {
    "update_bio",
    "update_bio_u2",
    "update_profile_picture",
    "warm_up_process",
    "instagram_reel_upload",
    "instagram_reel_upload_u2",
    "instagram_story_upload",
    "instagram_scroll",
    "instagram_like_feed",
    "instagram_notifications",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _status_to_airtable_fields(status: str) -> dict:
    """Map a run_profile_workflow terminal status to Airtable field values."""
    mapping = {
        "done": (STATUS_DONE, "success"),
        "already_had_bio": (STATUS_SKIPPED, "already_had_bio"),
        "adb_connect_failed": (STATUS_FAILED, "failed_to_connect_adb"),
        "failed": (STATUS_FAILED, "failed"),
    }
    if status not in mapping:
        return {}
    row_status, result = mapping[status]
    return {FIELD_STATUS: row_status, FIELD_LAST_RESULT: result, FIELD_LAST_RUN: _now_iso()}


def _parse_record(record: dict) -> dict | None:
    """Turn an Airtable record into a run spec, or None if unusable."""
    fields = record.get("fields", {}) or {}
    profile_id = str(fields.get(FIELD_PROFILE_ID, "") or "").strip()
    flow = str(fields.get(FIELD_FLOW, "") or "").strip()
    if not profile_id or not flow:
        return None
    return {
        "record_id": record.get("id"),
        "profile_id": profile_id,
        "flow": flow,
        "bio": (str(fields.get(FIELD_BIO, "") or "").strip() or None),
        "caption": (str(fields.get(FIELD_CAPTION, "") or "").strip() or None),
    }


def _map_terminal_status(status: str):
    """Map a run_profile_workflow terminal status to
    (run_log_result, note, incident_kind). `incident_kind` is a ban_detection
    kind ("banned" / "human_verification" / "action_block") when IG flagged the
    account, else None. Returns None for intermediate statuses
    (starting/connecting/running) so they produce no write-back."""
    if status == "done":
        return (RESULT_DONE, None, None)
    if status == "uncertain":
        return (RESULT_FAILED,
                "UNCERTAIN: Share was tapped but the post could not be confirmed -- "
                "check the account before re-running", None)
    if status == "already_had_bio":
        return (RESULT_SKIPPED, "already had a bio", None)
    if status == "adb_connect_failed":
        return (RESULT_FAILED, "ADB connect failed", None)
    if status == "failed":
        return (RESULT_FAILED, "flow reported a failure (see app logs)", None)
    if status == "human_verification":
        return (RESULT_FAILED, "human verification requested", "human_verification")
    if status == "banned":
        return (RESULT_FAILED, "account banned/suspended", "banned")
    if status == "action_block":
        return (RESULT_FAILED, "action blocked (temporary)", "action_block")
    return None


def _missing_input_reason(flow_run):
    """A clear reason a flow can't run for lack of its required input, or None.
    Checked before launching so we don't spend ~20s launching + connecting only
    to abort inside the flow -- and so the Run Log gets a useful note."""
    flow = flow_run.flow
    if flow in ("update_bio", "update_bio_u2") and not flow_run.bio:
        return "no bio text (set the account's Bio field, or tick 'Use UI flow' and type one)"
    if flow in ("update_profile_picture", "update_profile_picture_u2") and not flow_run.picture:
        return "no profile picture (tick 'Use UI flow' and pick a photo)"
    return None


def run_airtable_queue(
    airtable,
    launcher_client,
    shutdown_client,
    adb_enable_client,
    api_client,
    automation,
    logger,
    readiness_wait_seconds: int = 10,
    readiness_max_attempts: int = 2,
    batch_launch_delay_seconds: int = 1,
    should_stop=None,
    status_callback=None,
    progress_callback=None,
    shutdown_on_success: bool = False,
    today=None,
    run_reels: bool = False,
    selected_launch_ids=None,
    confirm_callback=None,
    override_flow=None,
    override_bio=None,
    override_caption=None,
    override_picture=None,
    media_path_resolver=None,
    max_concurrent_profiles=None,
) -> dict:
    """Lifecycle-driven Airtable run: read Accounts, decide each account's due
    flow(s) via the lifecycle planner, launch its Multilogin profile (by MLX API
    ID), run the flow(s), and write a Run Log row + Last Run/Result back.

    Accounts run in parallel; an account's own due flows run sequentially on its
    (single) profile so they never collide on one device.

    `selected_launch_ids` restricts the run to those profiles (None = all).
    `confirm_callback(num_to_run, num_skipped) -> bool` is called after planning
    and before any launch; returning False cancels the run.
    `media_path_resolver(launch_id) -> str | None` supplies the reel media folder
    mapped to that profile's Multilogin folder; None (the default, or for an
    unmapped folder) leaves media resolution to the flow's global setting."""

    def aborted() -> bool:
        return callable(should_stop) and should_stop()

    # 1) Plan -----------------------------------------------------------------
    try:
        plan = plan_airtable_runs(
            airtable, today=today, logger=logger, run_reels=run_reels,
            selected_launch_ids=selected_launch_ids,
            override_flow=override_flow, override_bio=override_bio,
            override_caption=override_caption, override_picture=override_picture,
        )
    except Exception as exc:
        logger.error("Failed to build the Airtable run plan: %s", exc)
        return {"processed": 0, "error": str(exc)}

    for skip in plan.skipped:
        logger.info("Skipping account %s: %s", skip.account_name, skip.reason)

    total_flows = sum(len(p.runs) for p in plan.plans)
    logger.info(
        "Airtable plan: %s account(s), %s flow-run(s) due; %s skipped",
        len(plan.plans), total_flows, len(plan.skipped),
    )

    # Let the caller confirm (and see how many will run) before we launch.
    if callable(confirm_callback):
        try:
            proceed = confirm_callback(len(plan.plans), len(plan.skipped))
        except Exception as exc:
            logger.warning("Airtable run confirm callback failed: %s", exc)
            proceed = True
        if not proceed:
            logger.info("Airtable run cancelled before launch")
            return {"processed": 0, "cancelled": True, "skipped": len(plan.skipped)}

    if not plan.plans:
        logger.info("Airtable plan: nothing due to run (%s account(s) skipped)", len(plan.skipped))
        return {"processed": 0, "skipped": len(plan.skipped)}

    # 2) Lock the profiles, then launch them ----------------------------------
    # A profile already being driven by another loop (posting) is skipped this
    # round rather than fought over; the next run picks it up.
    all_launch_ids: list[str] = []
    seen: set = set()
    for account_plan in plan.plans:
        if account_plan.launch_id not in seen:
            seen.add(account_plan.launch_id)
            all_launch_ids.append(account_plan.launch_id)

    with ProfileLocks(owner="warmup") as locks:
        launch_ids = locks.acquire_all(all_launch_ids)
        if locks.busy:
            logger.info("Skipping %s profile(s) busy in another loop: %s",
                        len(locks.busy), ", ".join(locks.busy))
        usable = set(launch_ids)
        plan.plans = [p for p in plan.plans if p.launch_id in usable]
        if not plan.plans:
            logger.info("All due profiles are busy in another loop; nothing to do this round")
            return {"processed": 0, "skipped": len(plan.skipped), "busy": len(locks.busy)}
        return _launch_and_run_flows(
            plan, launch_ids, airtable, launcher_client, shutdown_client, adb_enable_client,
            api_client, automation, logger, readiness_wait_seconds, readiness_max_attempts,
            batch_launch_delay_seconds, should_stop, status_callback, progress_callback,
            shutdown_on_success, busy_count=len(locks.busy),
            media_path_resolver=media_path_resolver,
            max_concurrent_profiles=max_concurrent_profiles,
        )


def _launch_and_run_flows(plan, launch_ids, airtable, launcher_client, shutdown_client,
                          adb_enable_client, api_client, automation, logger,
                          readiness_wait_seconds, readiness_max_attempts, batch_launch_delay_seconds,
                          should_stop, status_callback, progress_callback, shutdown_on_success,
                          busy_count=0, media_path_resolver=None,
                          max_concurrent_profiles=None) -> dict:
    """Launch the locked profiles and run each account's due flows. Split out so
    the lock in run_airtable_queue spans the whole launch->flow->shutdown life."""

    def aborted() -> bool:
        return callable(should_stop) and should_stop()

    # 3) Run each account's due flows (accounts parallel, flows sequential) ----
    def run_account(account_plan) -> None:
        if aborted():
            return
        # Only a clean account closes its profile at the end: any failed flow
        # leaves it open so it can be inspected (see `shutdown_on_success`).
        had_failure = False

        for flow_run in account_plan.runs:
            if aborted():
                return

            # Pre-flight: skip a flow whose required input is missing, with a
            # clear note, instead of launching + connecting only to abort.
            reason = _missing_input_reason(flow_run)
            if reason:
                logger.info("Skipping %s for %s: %s", flow_run.flow, account_plan.account_name, reason)
                airtable.create_run_log(
                    account_plan.account_id, account_plan.account_name, flow_run.flow, RESULT_SKIPPED, reason,
                )
                airtable.set_account_result(
                    account_plan.account_id, f"Skipped: {flow_run.flow} ({reason})",
                )
                continue

            run_log_id = airtable.create_run_log(
                account_plan.account_id, account_plan.account_name, flow_run.flow, RESULT_RUNNING,
            )

            def make_cb(fr, rid, acc_id):
                def _cb(pid: str, status: str, detail: str = "") -> None:
                    nonlocal had_failure
                    if callable(status_callback):
                        try:
                            status_callback(pid, status, detail)
                        except TypeError:
                            status_callback(pid, status)
                        except Exception:
                            pass
                    mapped = _map_terminal_status(status)
                    if mapped is None:
                        return
                    result, note, incident = mapped
                    # Which signal decided this -- see the note in posting_runner.
                    if detail:
                        note = f"{note} ({detail})" if note else detail
                    if result == RESULT_FAILED:
                        had_failure = True
                    last_result = f"{result}: {fr.flow}" + (f" ({note})" if note else "")
                    if rid:
                        airtable.update_run_log(rid, result, note)
                    airtable.set_account_result(acc_id, last_result)
                    # IG flagged the account mid-flow: record the incident so it
                    # drops out of the loops and shows on the Control Tower.
                    if incident:
                        incidents.apply_account_incident(airtable, acc_id, fr.flow, incident, note, logger)
                return _cb

            # A picture supplied as an Airtable attachment URL is downloaded to a
            # local temp file here (URLs are temporary); a local path passes through.
            picture = flow_run.picture
            temp_picture = None
            if attachments.is_url(picture):
                temp_picture = attachments.download_to_temp(picture, logger=logger)
                picture = temp_picture

            # Reels only, and only ever from the folder mapped to this profile's
            # own Multilogin folder -- an unmapped folder resolves to None and
            # keeps the flow's existing global media behaviour.
            folder_media = None
            if flow_run.flow in REEL_FLOWS and callable(media_path_resolver):
                try:
                    folder_media = media_path_resolver(account_plan.launch_id)
                except Exception as exc:
                    logger.warning("Could not resolve the reel media folder for %s: %s",
                                   account_plan.launch_id, exc)
                if folder_media:
                    logger.info("Profile %s will take its reel media from %s",
                                account_plan.launch_id, folder_media)

            try:
                run_profile_workflow(
                    account_plan.launch_id,
                    api_client.bearer_token,
                    api_client,
                    adb_enable_client,
                    shutdown_client,
                    automation,
                    logger,
                    readiness_wait_seconds=readiness_wait_seconds,
                    readiness_max_attempts=readiness_max_attempts,
                    flow_name=flow_run.flow,
                    should_stop=should_stop,
                    status_callback=make_cb(flow_run, run_log_id, account_plan.account_id),
                    progress_callback=progress_callback,
                    shutdown_on_abort=True,
                    shutdown_on_success=False,
                    caption=flow_run.caption,
                    bio=flow_run.bio,
                    picture=picture,
                    media_path=folder_media,
                    # Lets readiness relaunch a profile whose launch didn't
                    # take, instead of re-enabling ADB on something stopped.
                    launcher_client=launcher_client,
                )
            finally:
                if temp_picture:
                    try:
                        os.remove(temp_picture)
                    except OSError:
                        pass

        if shutdown_on_success and not aborted():
            if had_failure:
                logger.info(
                    "Leaving profile %s open: at least one flow failed for %s",
                    account_plan.launch_id, account_plan.account_name,
                )
            else:
                try:
                    shutdown_client.shutdown_profiles([account_plan.launch_id])
                except Exception as exc:
                    logger.warning("Failed to shut down profile %s after its flows: %s", account_plan.launch_id, exc)

    # A rolling window of at most `concurrency` phones. Fixed batches ran only as
    # fast as their slowest account and left finished phones idle until the whole
    # group was done; here a finished profile is replaced immediately.
    concurrency = resolve_concurrency(max_concurrent_profiles)
    plans_by_launch: dict = {}
    for account_plan in plan.plans:
        plans_by_launch.setdefault(account_plan.launch_id, []).append(account_plan)

    gate = LaunchGate(batch_launch_delay_seconds)

    def run_profile(launch_id) -> None:
        """Launch one profile, then run everything due on it, sequentially.

        The account plans for a launch id share one phone, and `run_account`
        shuts the profile down when its flows succeed -- so overlapping them
        would have one plan's shutdown cut another off mid-flow.
        """
        launch_response = gate.launch(lambda: launcher_client.start_profiles([launch_id]))
        if isinstance(launch_response, dict) and launch_response.get("status") == "error":
            logger.error("Failed to launch profile %s on Multilogin: %s", launch_id, launch_response)
        else:
            logger.info("Launched profile %s", launch_id)
        # No batch-wide readiness sleep: run_account waits for *this* profile.
        for account_plan in plans_by_launch.get(launch_id, []):
            if aborted():
                return
            run_account(account_plan)

    logger.info("Running %s profile(s), up to %s at a time (rolling)",
                len(launch_ids), concurrency)
    outcome = run_rolling(launch_ids, run_profile, concurrency=concurrency,
                          should_stop=should_stop, logger=logger)
    if outcome["aborted"]:
        return {"processed": 0, "aborted": True}

    total_flows = sum(len(p.runs) for p in plan.plans)
    logger.info("Airtable run complete (%s account(s), %s flow-run(s))", len(plan.plans), total_flows)
    return {"processed": len(plan.plans), "flows": total_flows,
            "skipped": len(plan.skipped), "busy": busy_count}
