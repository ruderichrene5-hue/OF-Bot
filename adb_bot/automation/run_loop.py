"""Headless entrypoint for the scheduled loops (Windows Task Scheduler).

One command runs one loop once and exits, so Task Scheduler can fire it on the
checklist's cadence:

    python -m adb_bot.automation.run_loop posting     # every 5-15 min
    python -m adb_bot.automation.run_loop warmup       # 2-3x/day
    python -m adb_bot.automation.run_loop pipeline      # every 15-30 min
    python -m adb_bot.automation.run_loop mlx-sync      # once a day

Dry-run is the default for every loop: it plans and prints but launches nothing
and writes nothing device-side. Add --apply to actually run. Credentials come
from saved dev settings or env (MULTILOGIN_TOKEN / AIRTABLE_TOKEN /
AIRTABLE_BASE_ID), the same as the other entrypoints.

The device loops (posting / warmup) only *do* device work under --apply; their
dry-run prints the plan, which is exactly what's testable off the server.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from adb_bot.automation import loop_watchdog, schedule_spec
from adb_bot.automation.flows import reel_verify
from adb_bot.clients import airtable as at
from adb_bot.clients.airtable import AirtableClient
from adb_bot.config import settings
from adb_bot.core import locks, shutdown
from adb_bot.core.logger import get_logger

LOOPS = ("pipeline", "queue", "posting", "recheck", "retry", "recovery", "warmup",
         "mlx-sync", "cleanup")
# `doctor` isn't a loop -- it's the preflight check, runnable the same way.
# `report` renders the operational page; like `doctor` it is a command rather
# than a loop, and unlike `doctor` it is not in the recommended set, so it never
# gets a timer -- the live view is the always-on report server.
COMMANDS = LOOPS + ("doctor", "report", "reap-phones")


def _mlx_token(cli_value=None) -> str:
    return (
        (cli_value or "").strip()
        or (os.environ.get("MULTILOGIN_TOKEN", "") or "").strip()
        or settings.get_saved_bearer_token()
    )


def _airtable(base_id=None, token=None) -> AirtableClient:
    token = (token or "").strip() or settings.get_saved_airtable_token()
    base_id = (base_id or "").strip() or settings.get_saved_airtable_base_id()
    if not token:
        raise SystemExit("[fatal] No Airtable token (AIRTABLE_TOKEN / dev settings / --airtable-token).")
    return AirtableClient(token, base_id, at.TABLE_PROFILES)


def _watch(logger, airtable, observe) -> None:
    """Record one loop tick with the production watchdog.

    Wrapped in its own try/except and always last: a loop's real work is already
    done by the time this runs, and a monitor that can fail the thing it watches
    is worse than no monitor. `observe` takes the watchdog so each loop can pass
    whatever its own report already counted.
    """
    try:
        observe(loop_watchdog.build_watchdog(logger=logger, airtable=airtable))
    except Exception as exc:
        logger.warning("watchdog: could not record this tick: %s", exc)


# --- individual loops --------------------------------------------------------

def _run_mlx_sync(args, logger) -> int:
    from adb_bot.automation import sync_cli
    token = _mlx_token(args.mlx_token)
    if not token:
        raise SystemExit("[fatal] No MultiLogin token (MULTILOGIN_TOKEN / dev settings / --mlx-token).")
    airtable_token = (args.airtable_token or "").strip() or settings.get_saved_airtable_token()
    base_id = (args.base_id or "").strip() or settings.get_saved_airtable_base_id()
    report = sync_cli.run_sync(token, airtable_token, base_id, dry_run=not args.apply,
                               skip_staging=args.skip_staging)
    return 1 if report.errors else 0


def _run_posting(args, logger) -> int:
    from adb_bot.automation.posting_planner import plan_posting_queue
    airtable = _airtable(args.base_id, args.airtable_token)

    if not args.apply:
        # Plan-only: pure, no device. This is the off-server-testable path.
        rows = airtable.list_pending_posts()
        plan = plan_posting_queue(
            rows, airtable.accounts_by_id(), airtable.profile_launch_map(),
            airtable.variants_by_id(), airtable.captions_by_id(), now=datetime.now(),
        )
        logger.info("[DRY-RUN] posting plan: %s", plan.summary())
        for item in plan.to_post:
            logger.info("  POST %s -> %s | video=%s | caption=%r",
                        item.account_name, item.launch_id, item.video_path, (item.caption or "")[:40])
        for skip in plan.skipped:
            logger.info("  skip %s: %s", skip.name, skip.reason)
        return 0

    from adb_bot.automation.bootstrap import build_automation, build_mlx_clients
    from adb_bot.automation.posting_runner import run_posting_queue
    token = _mlx_token(args.mlx_token)
    clients = build_mlx_clients(token)
    result = run_posting_queue(
        airtable, clients.launcher, clients.shutdown, clients.adb_enable, clients.api,
        build_automation(), logger,
        max_concurrent_profiles=args.max_concurrent,
        readiness_wait_seconds=settings.get_saved_readiness_wait(),
        readiness_max_attempts=settings.get_saved_readiness_attempts(),
        batch_launch_delay_seconds=settings.get_saved_batch_launch_delay(),
    )
    logger.info("posting result: %s", result)
    # Did this tick actually produce anything, given what was owed? A dead MLX
    # agent, stale locks and an empty queue all end here with the same quiet
    # "posting result"; only the watchdog tells them apart. See loop_watchdog.
    _watch(logger, airtable, lambda wd: loop_watchdog.observe_posting(wd, airtable, logger=logger))
    return 0


def _profile_warmup_plan(args, airtable, logger):
    """The warm-up plan for MLX profiles tagged `Created` (--targets profiles).

    New accounts live in MultiLogin long before anyone writes an Accounts row,
    so the account-driven planner cannot see them. This reads the tag straight
    off the MLX inventory and matches it to Airtable by serial.
    """
    from adb_bot.automation import warmup_targets
    from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient

    token = _mlx_token(args.mlx_token)
    if not token:
        raise SystemExit("[fatal] No MultiLogin token (MULTILOGIN_TOKEN / dev settings / --mlx-token).")
    mlx_items = MultiloginMobileListClient(token).list_mobile_profiles()

    profiles = airtable.warmup_profiles_by_serial()
    if profiles is None:
        raise SystemExit(
            f"[fatal] Profiles (Cloning) has no '{at.F_PROF_WARMUP_STARTED}' date field. "
            "Add it (the profile warm-up counts its days from there) and re-run.")

    warmup_plan = {}
    try:
        warmup_plan = airtable.warmup_plan_by_day() or {}
    except Exception as exc:
        logger.warning("Could not read the Warmup Plan table (%s); using the built-in schedule", exc)

    tag = args.warmup_tag or warmup_targets.WARMUP_TAG
    plan = warmup_targets.plan_profile_warmup(
        mlx_items, profiles,
        tag=tag,
        warmup_plan=warmup_plan,
        completed=airtable.todays_completed_profile_runs(),
    )
    logger.info("warmup: %d MLX profile(s), %d tagged '%s', %d due today",
                len(mlx_items), len(plan.plans) + len(plan.skipped), tag, len(plan.plans))
    return plan


def _run_warmup(args, logger) -> int:
    from adb_bot.automation.airtable_planner import plan_airtable_runs
    airtable = _airtable(args.base_id, args.airtable_token)

    on_profiles = args.targets == "profiles"
    plan = None
    if on_profiles:
        plan = _profile_warmup_plan(args, airtable, logger)

    if not args.apply:
        if plan is None:
            plan = plan_airtable_runs(airtable, logger=logger, run_reels=args.reels)
        total = sum(len(p.runs) for p in plan.plans)
        logger.info("[DRY-RUN] warmup plan: %s %s, %s flow-run(s), %s skipped",
                    len(plan.plans), "profile(s)" if on_profiles else "account(s)",
                    total, len(plan.skipped))
        for p in plan.plans:
            day = getattr(getattr(p, "warmup_target", None), "day", None)
            logger.info("  %s%s -> %s", p.account_name,
                        f" (day {day})" if day else "", [r.flow for r in p.runs])
        for skip in plan.skipped:
            logger.info("  skip %s: %s", skip.account_name, skip.reason)
        return 0

    from adb_bot.automation.bootstrap import build_automation, build_mlx_clients
    from adb_bot.automation.airtable_runner import run_airtable_queue
    token = _mlx_token(args.mlx_token)
    clients = build_mlx_clients(token)
    if on_profiles:
        # Stamp day 1 before launching: the campaign day counts calendar days
        # from the day the warm-up began, and a run that starts and then fails
        # still began. Doing it here (not at plan time) keeps a dry-run from
        # silently consuming a profile's day 1.
        from adb_bot.automation import warmup_targets
        warmup_targets.stamp_started(airtable, plan, logger=logger)
    result = run_airtable_queue(
        airtable, clients.launcher, clients.shutdown, clients.adb_enable, clients.api,
        build_automation(), logger, run_reels=args.reels,
        max_concurrent_profiles=args.max_concurrent,
        readiness_wait_seconds=settings.get_saved_readiness_wait(),
        readiness_max_attempts=settings.get_saved_readiness_attempts(),
        batch_launch_delay_seconds=settings.get_saved_batch_launch_delay(),
        plan=plan,
    )
    logger.info("warmup result: %s", result)
    return 0


def _run_pipeline(args, logger) -> int:
    from adb_bot.automation import spoof_pipeline
    airtable = _airtable(args.base_id, args.airtable_token)
    spoof_fn = None
    if args.apply:
        spoofer_python = args.spoofer_python or settings.get_saved_spoofer_python()
        spoofer_root = args.spoofer_root or settings.get_saved_spoofer_root()
        if spoofer_python and spoofer_root:
            spoof_fn = spoof_pipeline.build_cli_spoofer(spoofer_python, spoofer_root)
        else:
            logger.warning("pipeline: no spoofer configured (SPOOFER_PYTHON / SPOOFER_ROOT); "
                           "variants cannot be produced")
    report = spoof_pipeline.run_pipeline(
        airtable, logger=logger,
        raw_root=args.raw_root or settings.get_saved_raw_videos_dir(),
        out_root=args.out_root or settings.get_saved_spoofed_videos_dir(),
        spoof_fn=spoof_fn,
        drive_folder_id=args.drive_folder or settings.get_saved_drive_folder_id(),
        service_account_json=settings.get_saved_google_service_account_json(),
        dry_run=not args.apply,
        # `--max-variants` unset means "the default cap", not "no cap". Passing
        # args.max_variants straight through sent None, which *disables* the cap
        # -- so every CLI and systemd run was uncapped, which is exactly what the
        # constant exists to prevent. Use 0 to genuinely disable it.
        max_variants=(spoof_pipeline.MAX_VARIANTS_PER_RUN if args.max_variants is None
                      else (args.max_variants or None)),
        targets=args.targets,
        only_handles=[h for h in (args.profile or "").split(",") if h.strip()] or None,
    )
    logger.info("pipeline result: %s", report.summary())
    if args.apply:
        _watch(logger, airtable, lambda wd: loop_watchdog.observe_pipeline(wd, report))
    return 1 if report.errors else 0


def _run_cleanup(args, logger) -> int:
    """Delete media the bot has finished with (default: older than 2 days)."""
    from adb_bot.automation import retention

    days = args.max_age_days if args.max_age_days is not None else retention.DEFAULT_MAX_AGE_DAYS
    dry_run = not args.apply

    roots = [r for r in (args.raw_root or settings.get_saved_raw_videos_dir(),
                         settings.get_saved_story_media_path()) if r]
    inputs = retention.purge_used_inputs(roots, days, dry_run=dry_run, logger=logger)

    variants = retention.PurgeReport(dry_run=dry_run)
    try:
        airtable = _airtable(args.base_id, args.airtable_token)
        variants = retention.purge_used_variants(airtable, days, dry_run=dry_run, logger=logger)
    except SystemExit:
        logger.warning("cleanup: no Airtable token; skipping the spoofed-variant sweep")

    out_root = args.out_root or settings.get_saved_spoofed_videos_dir()
    retention.prune_empty_dirs(out_root, logger=logger, dry_run=dry_run)

    total = len(inputs.deleted) + len(variants.deleted)
    freed = (inputs.freed_bytes + variants.freed_bytes) / 1_048_576
    logger.info("cleanup: %s file(s) %s, %.1f MB freed (older than %s day(s))",
                total, "would be removed" if dry_run else "removed", freed, days)
    return 1 if (inputs.errors or variants.errors) else 0


def _run_queue(args, logger) -> int:
    """Create the Posting Queue rows for today's slots.

    The step between spoofing and posting: `pipeline` makes variants, `posting`
    consumes queue rows, and until this loop existed nothing made the rows. Five
    Airtable automations were meant to, but they are undeployed and write no
    Spoof Variant link, so every row they create is rejected by the planner.
    Here it is code instead -- Airtable cannot see the profile-driven targeting
    path, and cannot be tested.
    """
    from adb_bot.automation import queue_runner
    airtable = _airtable(args.base_id, args.airtable_token)
    report = queue_runner.run_queue_slots(
        airtable, logger=logger,
        slot_times=queue_runner.parse_slot_times(args.slots) if args.slots else queue_runner.DEFAULT_SLOT_TIMES,
        dry_run=not args.apply,
        include_profiles=args.targets != "accounts",
        # Each model's own Reel Post Times win over `--slots`; a model that has
        # picked none posts whenever it has a spoofed video, inside these bounds.
        use_model_times=args.model_times,
        anytime_gap_minutes=(queue_runner.DEFAULT_ANYTIME_GAP_MINUTES
                             if args.anytime_gap is None else args.anytime_gap),
        anytime_max_per_day=(queue_runner.DEFAULT_ANYTIME_MAX_PER_DAY
                             if args.anytime_max is None else args.anytime_max),
    )
    for name, reason in report.skipped:
        logger.info("queue: skipped %s: %s", name, reason)
    logger.info("queue result: %s", report.summary())
    if args.apply:
        _watch(logger, airtable, lambda wd: loop_watchdog.observe_queue(wd, report))
    return 1 if report.errors else 0


def _run_retry(args, logger) -> int:
    """Put retryable Failed rows back in the queue.

    Without this a failure is terminal: `list_pending_posts` only reads Pending,
    so nothing ever picks a Failed row up again. The ledger is what makes it safe
    -- a row whose clip may already be live is left alone, so a transient device
    failure retries and a post that actually landed never goes out twice.
    """
    from adb_bot.automation import retry_runner
    airtable = _airtable(args.base_id, args.airtable_token)
    tally = retry_runner.retry_failed_posts(
        airtable, logger=logger,
        max_retries=args.max_retries,
        dry_run=not args.apply,
    )
    logger.info("retry result: %s", tally)
    return 1 if tally.get("errors") else 0


def _run_recovery(args, logger) -> int:
    """Resume the profiles a person has un-flagged.

    Clearing `Needs Human Check` used to tell the bot nothing -- no loop read it
    -- so a profile somebody had fixed stayed dead, because what stopped it is on
    its queue rows (Retries Exhausted, count at the limit) and those rows also
    hold its clips. This hands them back to the retry pass, which still applies
    the ledger check before anything is posted again.
    """
    from adb_bot.automation import recovery_runner
    airtable = _airtable(args.base_id, args.airtable_token)
    report = recovery_runner.run_recovery(
        airtable, logger=logger,
        dry_run=not args.apply,
        max_retries=args.max_retries,
    )
    logger.info("recovery result: %s", report.summary())
    return 1 if report.errors else 0


def _run_recheck(args, logger) -> int:
    """Resolve posts that were sent but could not be confirmed in-run.

    The posting loop now stops verifying after ~45s and parks anything unproven
    in `Verifying` with a Recheck After stamp, so a profile never holds a phone
    for five minutes over one post. This loop is the other half of that
    bargain -- run it on a timer (every ~15 min) or those rows never resolve.
    """
    from adb_bot.automation import post_ledger, recheck_runner
    airtable = _airtable(args.base_id, args.airtable_token)

    rows = airtable.list_posts_awaiting_recheck()
    if not args.apply:
        # Plan-only: pure, no device. Shows what is parked and what evidence
        # each row has, which is also the fastest way to spot posts that are
        # unresolvable because no baseline count was captured.
        ledger = post_ledger.PostLedger()
        entries = {r.queue_id: r for r in ledger.pending() if r.queue_id}
        logger.info("[DRY-RUN] %s row(s) awaiting recheck", len(rows or []))
        for row in rows or []:
            fields = row.get("fields", {}) or {}
            entry = entries.get(row.get("id"))
            if entry is None:
                logger.info("  %s: no local ledger entry -- cannot be resolved here",
                            fields.get(at.F_PQ_NAME))
                continue
            logger.info("  %s: shared %.0f min ago, baseline=%s%s",
                        fields.get(at.F_PQ_NAME), entry.age_seconds / 60.0,
                        entry.baseline_count if entry.baseline_count >= 0 else "unavailable",
                        "" if entry.baseline_exact else " (not exact -- unresolvable)")
        return 0

    from adb_bot.automation.bootstrap import build_automation, build_mlx_clients
    from adb_bot.automation.workflow import run_profile_workflow
    token = _mlx_token(args.mlx_token)
    clients = build_mlx_clients(token)
    automation = build_automation()

    def read_post_count(profile_id, fields):
        """Launch the profile, read its post count, shut it down again.

        `profile_id` comes from the ledger entry and IS the 18-digit MLX launch
        key -- the flow records `str(profile.id)` at post time. Re-deriving it
        from the queue row was wrong twice over: it looked an *account* id up in
        `profile_launch_map()`, which is keyed by Profile record id and returns a
        dict, so the lookup could never hit; and it read only Target Account, so
        a profile-driven row had nothing to look up at all. Both failures
        surfaced identically -- "could not read the profile's post count" -- and
        the pass reported `unknown` without ever launching a phone.
        """
        if not profile_id:
            logger.warning("No profile id on the ledger entry for %s; cannot probe",
                           fields.get(at.F_PQ_NAME))
            return None
        launch_id = profile_id
        captured = {}

        def capture(result):
            if result.get("post_count") is not None:
                captured["count"] = reel_verify.Count(int(result["post_count"]),
                                                      bool(result.get("post_count_exact")))

        # This probe opens a phone too, so it takes a slot from the same global
        # ceiling posting and warmup draw on -- one loop that is "only one
        # profile" is still one more phone on the box. Without a slot the probe
        # is not run at all: returning None leaves the row parked in Verifying
        # (`decide_recheck` calls that unknown), which is the correct answer
        # here -- nothing was read -- and the next 15-minute pass retries it.
        with locks.live_profile_slot(owner="recheck") as slot:
            if slot is None:
                logger.warning(
                    "Skipping the recheck probe for %s: %s phone(s) already open across all "
                    "loops (global ceiling). The row stays in Verifying for the next pass.",
                    launch_id, locks.live_profile_count())
                return None

            run_profile_workflow(
                launch_id, clients.api.bearer_token, clients.api, clients.adb_enable,
                clients.shutdown, automation, logger,
                # Same readiness budget as posting and warmup. Left at the
                # defaults (2 x 10s) this probe gave a phone ~20s to come up,
                # while a cold one here needs 45-120s -- so it reported "could
                # not read the post count" for a phone that was merely still
                # booting, the row re-parked in Verifying, and the next pass
                # repeated it. Proven 2026-08-03: three rechecks, three unknowns,
                # none of which ever read a counter.
                readiness_wait_seconds=settings.get_saved_readiness_wait(),
                readiness_max_attempts=settings.get_saved_readiness_attempts(),
                flow_name="reel_post_count_probe", result_callback=capture,
                shutdown_on_success=True, launcher_client=clients.launcher,
            )
        return captured.get("count")

    tally = recheck_runner.recheck_pending_posts(airtable, read_post_count, logger=logger)
    logger.info("recheck result: %s", tally)
    _watch(logger, airtable, lambda wd: loop_watchdog.observe_recheck(wd, tally))
    return 0


def _run_doctor(args, logger) -> int:
    from adb_bot.automation import doctor
    results = doctor.run_checks()
    report = doctor.format_report(results)
    print(report)
    for line in report.splitlines():
        logger.info("%s", line)
    # Unlike the loops this runs on every invocation, not just `--apply`: doctor
    # never writes anything but its own watchdog entry, and a hand-run preflight
    # that found a dead agent should clear the alert exactly like a timed one.
    #
    # Best-effort client: with one, alerts also reach the Airtable Run Log; with
    # none -- no token, or Airtable itself being the thing that is broken -- the
    # log and alerts.log sinks still fire, which is the case that matters most.
    try:
        airtable = _airtable(args.base_id, args.airtable_token)
    except (SystemExit, Exception):
        airtable = None
    _watch(logger, airtable, lambda wd: doctor.observe_doctor(wd, results))
    return doctor.exit_code(results)


def _run_report(args, logger) -> int:
    """Write the operational report to a file (the shareable snapshot).

    The live view is `report_server`; this is the same collector and the same
    renderer with the meta-refresh left out, because a snapshot that silently
    reloads itself into a blank page once it is off the box is a trap.
    """
    from adb_bot.automation import report, report_html

    try:
        airtable = _airtable(args.base_id, args.airtable_token)
    except (SystemExit, Exception):
        airtable = None
        logger.warning("report: no Airtable client; writing local sections only")

    data = report.collect(airtable=airtable, use_cache=False)
    page = report_html.render(data, live=False)
    out = Path(args.out or (schedule_spec.repo_root() / "logs" / "report.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    logger.info("report: wrote %s (%d bytes)", out, len(page))
    print(out)
    return 0


def _run_reap_phones(args, logger) -> int:
    """Close phones no loop owns any more.

    Dry-run by default like every other pass here: it reports what it would
    close and touches nothing without --apply.
    """
    from adb_bot.automation import phone_reaper

    shutdown_client = None
    if args.apply:
        try:
            from adb_bot.automation.bootstrap import build_mlx_clients
            shutdown_client = build_mlx_clients(_mlx_token(args.mlx_token)).shutdown
        except Exception as exc:
            # Without the API we can still signal the processes; say so rather
            # than silently degrading to the blunter tool.
            logger.warning("reaper: no MultiLogin client (%s); will signal processes instead", exc)

    report = phone_reaper.reap(shutdown_client=shutdown_client, logger=logger,
                               dry_run=not args.apply)
    logger.info("reap-phones result: %s", report.summary())
    return 0


_DISPATCH = {
    "posting": _run_posting,
    "recheck": _run_recheck,
    "warmup": _run_warmup,
    "pipeline": _run_pipeline,
    "queue": _run_queue,
    "retry": _run_retry,
    "recovery": _run_recovery,
    "mlx-sync": _run_mlx_sync,
    "cleanup": _run_cleanup,
    "doctor": _run_doctor,
    "report": _run_report,
    "reap-phones": _run_reap_phones,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one scheduled bot loop once and exit.")
    parser.add_argument("loop", choices=COMMANDS,
                        help="Which loop to run, or 'doctor' to preflight-check the setup.")
    parser.add_argument("--apply", action="store_true", help="Do real work (default: dry-run/plan-only).")
    # On by default now that reel media is wired (Drive -> spoof pipeline ->
    # Spoof Variants). `--no-reels` disables them, e.g. to run a warm-up day
    # without posting while the content side is being reworked.
    parser.add_argument("--reels", action=argparse.BooleanOptionalAction, default=True,
                        help="warmup: run reel actions from the plan (default: on; use --no-reels to skip).")
    parser.add_argument("--max-age-days", type=float, default=None,
                        help="cleanup: delete finished media older than this (default 2).")
    parser.add_argument("--max-concurrent", type=int, default=None,
                        help="posting/warmup: max profiles running at once (default 10).")
    parser.add_argument("--max-variants", type=int, default=None,
                        help="pipeline: max variants produced per run (default 20; 0 = no cap).")
    parser.add_argument("--profile", default=None,
                        help="pipeline: restrict to these target handles (comma-separated, "
                             "e.g. 'Jil 1'). Use to try one profile end to end.")
    parser.add_argument("--slots", default=None,
                        help="queue: comma-separated fallback slot times, used only for a base "
                             "with no per-model Reel Post Times (default 09:00,11:00,...,21:00).")
    parser.add_argument("--model-times", action=argparse.BooleanOptionalAction, default=True,
                        help="queue: take each model's posting times from Airtable "
                             "(Models.Reel Post Times). --no-model-times puts every model back "
                             "on the one --slots grid for this run.")
    parser.add_argument("--anytime-gap", type=int, default=None,
                        help="queue: minutes between posts for a model that picked no times "
                             "(default 120).")
    parser.add_argument("--anytime-max", type=int, default=None,
                        help="queue: most posts per day for a model that picked no times "
                             "(default 7; Models.Reels Per Day overrides it per model).")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="retry: give up on a row once Retry Count reaches this (default 3).")
    parser.add_argument("--targets", choices=("accounts", "profiles"), default="accounts",
                        help="pipeline/queue/warmup: what to work on -- Airtable Accounts at "
                             "Lifecycle Stage Active (default), or the MLX profile inventory, "
                             "for models that have phones but no Accounts rows yet. For warmup, "
                             "'profiles' warms up the MLX profiles carrying --warmup-tag.")
    parser.add_argument("--warmup-tag", default=None,
                        help="warmup --targets profiles: the MultiLogin tag that marks a profile "
                             "as ready to warm up (default 'Created').")
    parser.add_argument("--skip-staging", action="store_true",
                        help="mlx-sync: skip staging profiles that belong to no model.")
    parser.add_argument("--out", default=None,
                        help="report: file to write the HTML to (default logs/report.html).")
    parser.add_argument("--raw-root", default=None, help="pipeline: raw-videos root (overrides config).")
    parser.add_argument("--out-root", default=None, help="pipeline: spoofed-videos output root (overrides config).")
    parser.add_argument("--drive-folder", default=None, help="pipeline: Google Drive folder id of 01_Raw_Videos.")
    parser.add_argument("--spoofer-python", default=None, help="pipeline: interpreter for the video_spoofer project.")
    parser.add_argument("--spoofer-root", default=None, help="pipeline: root folder of the video_spoofer project.")
    parser.add_argument("--mlx-token", default=None)
    parser.add_argument("--airtable-token", default=None)
    parser.add_argument("--base-id", default=None)
    args = parser.parse_args(argv)

    logger = get_logger(f"loop_{args.loop}", log_file=f"logs/loop_{args.loop}.log")
    logger.setLevel(logging.INFO)

    # Every loop -- posting, warmup, recheck, and the ones that touch no device
    # -- comes through here, so this is the one place that covers all of them.
    # Without it a `systemctl stop` leaves this run's profile locks behind for
    # their 45-minute TTL (the next run then finds every profile "busy" and does
    # nothing) and leaves its phones running (TODO 3.2 / 3.3).
    shutdown.install_signal_handlers(logger)

    logger.info("=== loop '%s' start (%s) ===", args.loop, "APPLY" if args.apply else "DRY-RUN")
    try:
        code = _DISPATCH[args.loop](args, logger)
    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("loop '%s' failed: %s", args.loop, exc)
        return 1
    logger.info("=== loop '%s' done (exit %s) ===", args.loop, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
