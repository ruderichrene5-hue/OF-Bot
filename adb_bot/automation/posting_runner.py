"""Run the Posting Queue: post each due Spoof Variant + Caption on its account's
profile, and write the result back (checklist loop #1).

Mirrors the lifecycle runner but is queue-driven: it plans from the Posting Queue
(via posting_planner), launches each profile, runs the reel-upload flow with the
row's specific video + caption, and records the outcome on the queue row, a Run
Log entry, and the account. Ban / verification / action-block mid-post is routed
through incidents.apply_account_incident (which also stamps the queue row's
Issue Type).

`apply_post_result` -- the write-back half -- is factored out and unit-tested
with a fake client, so the whole mapping is verifiable without a device.
"""

from __future__ import annotations


from adb_bot.clients import airtable as at
from adb_bot.core.locks import ProfileLocks
from adb_bot.core.batching import LaunchGate, resolve_concurrency, run_rolling
from adb_bot.automation import incidents
from adb_bot.automation.posting_planner import plan_posting_queue
from adb_bot.automation.workflow import run_profile_workflow

# The flow that actually uploads a reel. u2 is the reliable path being standardized.
POST_FLOW = "instagram_reel_upload_u2"


def _map_post_status(status: str):
    """Map a run_profile_workflow terminal status to
    (post_status, issue_type, incident_kind, run_result, note). None means an
    intermediate status that produces no write-back."""
    if status == "done":
        return (at.POST_STATUS_POSTED, at.ISSUE_NONE, None, at.RESULT_DONE, "posted")
    if status == "uncertain":
        # Share was tapped but the post could not be proven inside the run's
        # (deliberately short) budget. This is not a failure and not a job for a
        # human -- it is a question the machine can answer better in fifteen
        # minutes, once Instagram has finished processing and the profile has
        # refreshed. The row parks in Verifying with a Recheck After stamp and
        # the deferred pass resolves it.
        #
        # It used to land on Failed + Issue=Other, which was wrong twice over:
        # it reported live posts as failures, and it put them in front of a
        # person who then had no better way to check than we did.
        return (at.POST_STATUS_VERIFYING, at.ISSUE_NONE, None, at.RESULT_UNVERIFIED,
                "sent but not yet confirmed -- queued for automatic recheck")
    if status == "human_verification":
        return (at.POST_STATUS_FAILED, at.ISSUE_HUMAN_VERIFICATION, "human_verification", at.RESULT_FAILED, "human verification requested")
    if status == "banned":
        return (at.POST_STATUS_FAILED, at.ISSUE_BANNED_BLOCKED, "banned", at.RESULT_FAILED, "account banned/suspended")
    if status == "action_block":
        return (at.POST_STATUS_FAILED, at.ISSUE_BANNED_BLOCKED, "action_block", at.RESULT_FAILED, "action blocked (temporary)")
    if status == "adb_connect_failed":
        return (at.POST_STATUS_FAILED, at.ISSUE_NEEDS_RETRY, None, at.RESULT_FAILED, "ADB connect failed")
    if status == "failed":
        return (at.POST_STATUS_FAILED, at.ISSUE_NEEDS_RETRY, None, at.RESULT_FAILED, "flow reported a failure (see app logs)")
    return None


def apply_post_result(airtable, item, status, flow=POST_FLOW, logger=None, detail="") -> bool:
    """Write one post's outcome back to Airtable. Returns True if it was a
    terminal status that produced a write-back, False for intermediate statuses.

    - Posted: Post Status=Posted (+ clear Issue), mark the Spoof Variant Used.
    - Retryable failure: Post Status=Failed, Issue=Failed - Needs Retry, +1 retry.
    - Account flag (ban/verify/action-block): record the incident (which also
      stamps the queue row's Issue Type + Post Status) -- retry is NOT bumped.
    Always writes a Run Log row and the account's Last Run/Result.
    """
    mapped = _map_post_status(status)
    if mapped is None:
        return False
    post_status, issue_type, incident, run_result, note = mapped

    # Record which signal decided this. Without it the Run Log says "failed"
    # with no way to tell a post that never happened from one we simply could
    # not see -- which is the difference between "retry" and "go look".
    if detail:
        note = f"{note} ({detail})" if note else detail

    airtable.create_run_log(item.account_id, item.account_name, flow, run_result, note)
    airtable.set_account_result(item.account_id, f"{run_result}: {flow} ({note})")

    if post_status == at.POST_STATUS_POSTED:
        airtable.mark_post_result(item.queue_id, at.POST_STATUS_POSTED, issue_type)
        if item.variant_id:
            airtable.mark_variant_used(item.variant_id)
    elif post_status == at.POST_STATUS_VERIFYING:
        # No retry bump and no Issue: this is neither a "try again" nor a
        # "something is wrong" outcome, it is an open question with a scheduled
        # answer. The variant stays unused until the recheck decides -- marking
        # it Used now would lose it if the post turns out never to have landed.
        airtable.mark_post_pending_verification(item.queue_id, note=note)
    elif incident:
        # incidents also sets the queue row's Issue Type + Post Status=Failed and
        # flags the account so the loops skip it. Don't bump retry -- not retryable.
        incidents.apply_account_incident(
            airtable, item.account_id, flow, incident, note, logger,
            queue_record_id=item.queue_id,
        )
    else:
        airtable.mark_post_result(
            item.queue_id, at.POST_STATUS_FAILED, issue_type, retry_count=item.retry_count + 1,
        )
    return True


def run_posting_queue(
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
    now=None,
    selected_launch_ids=None,
    confirm_callback=None,
    flow: str = POST_FLOW,
    max_concurrent_profiles=None,
) -> dict:
    """Plan the due posts, launch their profiles, run the reel-upload flow with
    each post's video + caption, and write results back."""

    def aborted() -> bool:
        return callable(should_stop) and should_stop()

    # 1) Plan from the queue ---------------------------------------------------
    try:
        rows = airtable.list_pending_posts()
        plan = plan_posting_queue(
            rows,
            accounts_by_id=airtable.accounts_by_id(),
            profiles_by_recid=airtable.profile_launch_map(),
            variants_by_id=airtable.variants_by_id(),
            captions_by_id=airtable.captions_by_id(),
            now=now,
            selected_launch_ids=selected_launch_ids,
        )
    except Exception as exc:
        logger.error("Failed to build the posting-queue plan: %s", exc)
        return {"processed": 0, "error": str(exc)}

    for skip in plan.skipped:
        logger.info("Skipping post %s: %s", skip.name, skip.reason)
    logger.info("Posting-queue plan: %s due, %s skipped", len(plan.to_post), len(plan.skipped))

    if callable(confirm_callback):
        try:
            if not confirm_callback(len(plan.to_post), len(plan.skipped)):
                logger.info("Posting run cancelled before launch")
                return {"processed": 0, "cancelled": True, "skipped": len(plan.skipped)}
        except Exception as exc:
            logger.warning("Posting run confirm callback failed: %s", exc)

    if not plan.to_post:
        return {"processed": 0, "skipped": len(plan.skipped)}

    # 2) Lock the profiles, then launch them ----------------------------------
    # A profile already being driven by another loop (warmup) is skipped this
    # round rather than fought over; the next cycle picks it up.
    all_launch_ids = list(dict.fromkeys(item.launch_id for item in plan.to_post))
    with ProfileLocks(owner="posting") as locks:
        launch_ids = locks.acquire_all(all_launch_ids)
        if locks.busy:
            logger.info("Skipping %s profile(s) busy in another loop: %s",
                        len(locks.busy), ", ".join(locks.busy))
        plan.to_post = [item for item in plan.to_post if item.launch_id in set(launch_ids)]
        if not plan.to_post:
            logger.info("All due profiles are busy in another loop; nothing to do this round")
            return {"processed": 0, "skipped": len(plan.skipped), "busy": len(locks.busy)}
        return _launch_and_post(
            plan, launch_ids, airtable, launcher_client, shutdown_client, adb_enable_client,
            api_client, automation, logger, readiness_wait_seconds, readiness_max_attempts,
            batch_launch_delay_seconds, should_stop, status_callback, progress_callback, flow,
            busy_count=len(locks.busy), max_concurrent_profiles=max_concurrent_profiles,
        )


def _launch_and_post(plan, launch_ids, airtable, launcher_client, shutdown_client,
                     adb_enable_client, api_client, automation, logger,
                     readiness_wait_seconds, readiness_max_attempts, batch_launch_delay_seconds,
                     should_stop, status_callback, progress_callback, flow, busy_count=0,
                     max_concurrent_profiles=None) -> dict:
    """Launch the locked profiles and post on each. Split out so the lock in
    run_posting_queue wraps the whole launch->post->shutdown lifetime."""

    def aborted() -> bool:
        return callable(should_stop) and should_stop()

    # 3) Post each item (parallel across profiles) ----------------------------
    def run_post(item) -> None:
        if aborted():
            return

        def _cb(pid: str, status: str, detail: str = "") -> None:
            if callable(status_callback):
                try:
                    status_callback(pid, status, detail)
                except TypeError:
                    status_callback(pid, status)
                except Exception:
                    pass
            try:
                apply_post_result(airtable, item, status, flow=flow, logger=logger, detail=detail)
            except Exception as exc:
                logger.warning("Failed to write post result for %s: %s", item.account_name, exc)

        run_profile_workflow(
            item.launch_id,
            api_client.bearer_token,
            api_client,
            adb_enable_client,
            shutdown_client,
            automation,
            logger,
            readiness_wait_seconds=readiness_wait_seconds,
            readiness_max_attempts=readiness_max_attempts,
            flow_name=flow,
            should_stop=should_stop,
            status_callback=_cb,
            progress_callback=progress_callback,
            shutdown_on_abort=True,
            shutdown_on_success=True,
            caption=item.caption,
            media_path=item.video_path,
            # Lets readiness relaunch a profile whose launch didn't take,
            # instead of re-enabling ADB on something that isn't running.
            launcher_client=launcher_client,
        )

    # A rolling window of at most `concurrency` phones. The previous fixed
    # batches ran only as fast as their slowest profile: with a 3-minute post
    # verification floor, one laggard held every finished phone in its batch
    # idle. Here a finished profile is replaced immediately.
    concurrency = resolve_concurrency(max_concurrent_profiles)
    items_by_launch: dict = {}
    for item in plan.to_post:
        items_by_launch.setdefault(item.launch_id, []).append(item)

    gate = LaunchGate(batch_launch_delay_seconds)

    def run_profile(launch_id) -> None:
        """Launch one profile, then work through everything due on it.

        Its items run **sequentially**. They share one phone, and
        `run_profile_workflow` shuts the profile down when it succeeds -- so
        running two of them at once would have the first one's shutdown pull the
        device out from under the second.
        """
        response = gate.launch(lambda: launcher_client.start_profiles([launch_id]))
        if isinstance(response, dict) and response.get("status") == "error":
            logger.error("Failed to launch profile %s: %s", launch_id, response)
        else:
            logger.info("Launched profile %s", launch_id)
        # No batch-wide readiness sleep any more: run_profile_workflow waits for
        # *this* profile to be ready, which is the same wait applied where it
        # belongs instead of once for a whole group.
        for item in items_by_launch.get(launch_id, []):
            if aborted():
                return
            run_post(item)

    logger.info("Posting %s profile(s), up to %s at a time (rolling)",
                len(launch_ids), concurrency)
    outcome = run_rolling(launch_ids, run_profile, concurrency=concurrency,
                          should_stop=should_stop, logger=logger)
    if outcome["aborted"]:
        return {"processed": 0, "aborted": True}

    logger.info("Posting run complete (%s post(s))", len(plan.to_post))
    return {"processed": len(plan.to_post), "skipped": len(plan.skipped), "busy": busy_count}
