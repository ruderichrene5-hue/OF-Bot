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
from adb_bot.clients.multilogin.launch_stats import CountingLauncherClient, MLX_500, classify_launch
from adb_bot.core.locks import ProfileLocks, live_profile_count, live_profile_slot, max_live_profiles
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
    if status == "already_shared":
        # The ledger stopped a second send of a clip this profile already got.
        # Terminal and NOT retryable: the queue row asks for something that has
        # already happened, so Issue stays Other (the retry pass only re-queues
        # "Failed - Needs Retry") and the counter is not bumped -- a refusal is
        # not an attempt. The Run Log says Skipped rather than Failed, because
        # a guard doing its job should not read as a breakage when someone is
        # scanning for problems.
        #
        # The variant is deliberately left alone rather than marked Used: if a
        # later recheck disproves the original post, the clip becomes sendable
        # again, and consuming it here would throw that away.
        return (at.POST_STATUS_FAILED, at.ISSUE_OTHER, None, at.RESULT_SKIPPED,
                "skipped: this clip was already sent to this profile")

    if status == "heartbeat_lost":
        # The phone stopped being ours mid-post. Retryable: nothing is wrong
        # with the account, the post simply never completed.
        return (at.POST_STATUS_FAILED, at.ISSUE_NEEDS_RETRY, None, at.RESULT_FAILED,
                "profile lost mid-run (heartbeat failed)")
    return None


def consumes_retry_budget(status: str) -> bool:
    """Whether this terminal status spends one of the queue row's retries.

    Mirrors the last branch of `apply_post_result` -- the only one that writes
    `retry_count + 1`. Kept as its own predicate so the MLX-500 accounting can
    ask "did this failure cost the row a retry?" without duplicating the mapping
    table or guessing from the status name.
    """
    mapped = _map_post_status(status)
    if mapped is None:
        return False
    post_status, _issue, incident, _result, _note = mapped
    if post_status != at.POST_STATUS_FAILED:
        return False
    # Incidents (ban / verification / action block) and `already_shared` are
    # terminal but deliberately do NOT bump the counter.
    return not incident and status != "already_shared"


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
    elif incident and item.account_id:
        # incidents also sets the queue row's Issue Type + Post Status=Failed and
        # flags the account so the loops skip it. Don't bump retry -- not retryable.
        incidents.apply_account_incident(
            airtable, item.account_id, flow, incident, note, logger,
            queue_record_id=item.queue_id,
        )
    elif incident:
        # Profile-driven run: the flag belongs on an Accounts row that doesn't
        # exist. Still stamp the queue row so the incident is visible and the row
        # is not retried blindly -- but no retry bump, same as the account path.
        airtable.mark_post_result(item.queue_id, at.POST_STATUS_FAILED, issue_type)
    elif status == "already_shared":
        # Terminal, but not an attempt: the send never happened because the clip
        # was already out. Bumping the counter here would spend a retry the row
        # never used -- and the row is not retryable anyway, so the only effect
        # would be a misleading number in front of whoever reads it.
        airtable.mark_post_result(item.queue_id, at.POST_STATUS_FAILED, issue_type)
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

    # Count every launch this run makes. The wrapper is handed to readiness too
    # (as `launcher_client`), so its relaunches are counted on the same tally --
    # and MultiLogin's own 500s stay separated from our failures. `stats` rides
    # along on every return so even a run that launches nothing reports 0/0.0%
    # instead of a missing number.
    launcher_client = CountingLauncherClient(launcher_client)
    stats = launcher_client.stats

    def result(payload: dict) -> dict:
        payload.update(stats.as_dict())
        return payload

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
        return result({"processed": 0, "error": str(exc)})

    for skip in plan.skipped:
        logger.info("Skipping post %s: %s", skip.name, skip.reason)
    logger.info("Posting-queue plan: %s due, %s skipped", len(plan.to_post), len(plan.skipped))

    if callable(confirm_callback):
        try:
            if not confirm_callback(len(plan.to_post), len(plan.skipped)):
                logger.info("Posting run cancelled before launch")
                return result({"processed": 0, "cancelled": True, "skipped": len(plan.skipped)})
        except Exception as exc:
            logger.warning("Posting run confirm callback failed: %s", exc)

    if not plan.to_post:
        return result({"processed": 0, "skipped": len(plan.skipped)})

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
            return result({"processed": 0, "skipped": len(plan.skipped), "busy": len(locks.busy)})
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

    # Normally already wrapped by run_posting_queue; wrapping again here (it is
    # idempotent) keeps this function honest when it is called directly, so no
    # launch path can report a run without its MLX numbers.
    if not isinstance(launcher_client, CountingLauncherClient):
        launcher_client = CountingLauncherClient(launcher_client)
    stats = launcher_client.stats

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
            # One named line per finished post, before the Airtable write can
            # fail. Everything else in this log knows a profile only by its MLX
            # id, so without this the daily report can say how many posts a run
            # made but not *which* accounts they were -- and the id is no use to
            # the person reading it. Only terminal statuses: the intermediate
            # ones are progress, not results.
            if _map_post_status(status) is not None:
                logger.info("Post result for %s (profile %s): %s%s",
                            item.account_name, item.launch_id, status,
                            f" -- {detail}" if detail else "")
            try:
                apply_post_result(airtable, item, status, flow=flow, logger=logger, detail=detail)
                # If this row just spent a retry and its profile's launch 500ed
                # on MultiLogin's side, that retry was burned by their cloud,
                # not by anything the bot did. That is the number which explains
                # a row reaching "Retries Exhausted" with nothing wrong here.
                if consumes_retry_budget(status):
                    stats.note_retry_consumed(item.launch_id)
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
            # Stamps the local post ledger, which is how the deferred recheck
            # matches a ledger entry back to its Verifying row. Without it every
            # entry is written with an empty queue_id and the recheck can never
            # resolve anything -- proven 2026-08-03, two Verifying rows returned
            # "no local ledger entry" against a ledger that held both posts.
            queue_id=item.queue_id,
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

    no_slot: list = []

    def run_profile(launch_id) -> None:
        """Launch one profile, then work through everything due on it.

        Its items run **sequentially**. They share one phone, and
        `run_profile_workflow` shuts the profile down when it succeeds -- so
        running two of them at once would have the first one's shutdown pull the
        device out from under the second.
        """
        # The cross-loop ceiling. `concurrency` above only bounds *this* loop;
        # warmup and recheck apply their own, so the three of them could have 21
        # phones open between them. No slot means the box is already at its
        # global limit: don't launch, and let the next tick pick this profile up
        # (its queue row is untouched, exactly as when another loop holds it).
        with live_profile_slot(owner="posting") as slot:
            if slot is None:
                no_slot.append(launch_id)
                logger.warning(
                    "Skipping profile %s this round: %s phone(s) already open across all "
                    "loops (global ceiling). It will be retried next tick.",
                    launch_id, live_profile_count())
                return

            response = gate.launch(lambda: launcher_client.start_profiles([launch_id]))
            if isinstance(response, dict) and response.get("status") == "error":
                if classify_launch(response) == MLX_500:
                    # Say whose failure it is where it happens, not only in the
                    # end-of-run tally: this one is MultiLogin's cloud and will
                    # self-heal, so it is not a reason to go looking at the box.
                    logger.error("Failed to launch profile %s -- MultiLogin-side 500 (their "
                                 "cloud; self-heals, but it still spends this row's retry "
                                 "budget): %s", launch_id, response)
                else:
                    logger.error("Failed to launch profile %s: %s", launch_id, response)
            else:
                logger.info("Launched profile %s", launch_id)
            # No batch-wide readiness sleep any more: run_profile_workflow waits
            # for *this* profile to be ready, which is the same wait applied
            # where it belongs instead of once for a whole group.
            for item in items_by_launch.get(launch_id, []):
                if aborted():
                    return
                run_post(item)

    logger.info("Posting %s profile(s), up to %s at a time (rolling, global ceiling %s)",
                len(launch_ids), concurrency, max_live_profiles())
    outcome = run_rolling(launch_ids, run_profile, concurrency=concurrency,
                          should_stop=should_stop, logger=logger)
    if outcome["aborted"]:
        logger.info("Posting run aborted; %s", stats.summary())
        return {"processed": 0, "aborted": True, **stats.as_dict()}

    deferred = set(no_slot)
    processed = len([item for item in plan.to_post if item.launch_id not in deferred])
    if deferred:
        logger.warning("%s profile(s) deferred to the next run by the global phone ceiling: %s",
                       len(deferred), ", ".join(sorted(deferred)))
    # One run summary, not two: the MLX launch tally rides on the line that
    # already closes the run (and in the dict the loop logs and returns), so a
    # bad night on their side is readable without correlating anything.
    logger.info("Posting run complete (%s post(s)); %s", processed, stats.summary())
    return {"processed": processed, "skipped": len(plan.skipped), "busy": busy_count,
            "no_slot": len(deferred), **stats.as_dict()}
