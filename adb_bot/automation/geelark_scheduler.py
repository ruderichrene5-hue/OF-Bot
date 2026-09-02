"""Time-windowed Geelark queue: Warmup at night, Active_Posting by day.

Confirmed schedule 2026-08-29:

* 06:00-23:00 Europe/Berlin -- only `Active_Posting`-tagged profiles run,
  each up to `POSTS_PER_LAUNCH` posts (a scroll before each) in one launch
  (`geelark_lifecycle.run_active_posting_cycle`).
* 23:00-06:00 Europe/Berlin -- only `Warmup`-tagged profiles run, each one
  10-minute pass, then the tag flips to `Active_Posting` on success
  (`geelark_lifecycle.run_warmup_cycle`). Already-`Active_Posting` profiles
  are not touched at all during this window -- they sleep, matching real
  human behavior overnight.

Exactly 4 concurrent -- one per real modem -- refilling from the tag's
worklist as each slot frees up. `proxy_pool.acquire_proxy`'s file-lock is
what actually prevents two workers from landing on the same port; this
module only owns picking the window/tag and keeping 4 slots busy.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from adb_bot.automation import geelark_lifecycle as lifecycle
from adb_bot.clients.geelark.transport import GeelarkTransport

BERLIN = ZoneInfo("Europe/Berlin")
WARMUP_WINDOW_START_HOUR = 23    # inclusive
WARMUP_WINDOW_END_HOUR = 6       # exclusive

CONCURRENCY = 4    # one per real modem

# Posts per launch, not per day. Changed 2026-08-31: the launch itself (cold
# boot + IP rotation + ADB connect) is a fixed ~90s cost regardless of how
# many clips get posted once it's up, so batching posts into one launch
# instead of running separate launches saves that ~90s every extra post.
# Raised 2 -> 3 on 2026-09-02, explicit instruction, once the scroll gap
# between posts was dropped (see geelark_lifecycle.run_active_posting_cycle)
# made a third post cheap enough to be worth it.
POSTS_PER_LAUNCH = 3

# How long before the 23:00 Warmup window an Active_Posting pass must stop
# claiming new profiles. Not zero: the last profile claimed just before the
# deadline still needs time to actually finish, so this is a floor under the
# deadline, not the deadline itself -- see _active_posting_deadline.
NIGHT_WINDOW_SAFETY_BUFFER_SECONDS = 15 * 60

# The day-posting timer's own fire times (Europe/Berlin) -- deliberately
# duplicated from adbbot-geelark-day-posting.timer's OnCalendar lines.
# Added 2026-08-31: without this, one long Active_Posting pass silently
# absorbed a later scheduled fire (systemd does not start a second instance
# of an already-active oneshot service), which meant a code change made
# between two fire times never actually ran until the pass finished on its
# own, hours later. active_posting_budget_seconds now yields at whichever
# comes first, this or the night boundary, so each scheduled slot reliably
# gets a fresh process -- and whatever is on disk at that moment.
#
# Was briefly hourly (tuple((h, 0) for h in range(7, 23))) the same day, then
# reverted back to 3x/day on explicit instruction ("stell wieder zurück") --
# the hourly change traded away the larger per-slot time budget the 3x/day
# spacing gave each run, which matters for how long the live post-confirm
# window (reel_verify.FAST_TIMEOUT_SECONDS) can afford to be.
DAY_POSTING_FIRE_TIMES_BERLIN = ((7, 0), (12, 30), (18, 30))


def in_warmup_window(now: datetime | None = None) -> bool:
    """True during the 23:00-06:00 Berlin freeze window -- the one that
    wraps past midnight, so it's an OR, not a simple between-check."""
    now = (now or datetime.now(BERLIN))
    hour = now.astimezone(BERLIN).hour
    return hour >= WARMUP_WINDOW_START_HOUR or hour < WARMUP_WINDOW_END_HOUR


def active_tag_for_now(now: datetime | None = None) -> str:
    # Off-switch for the whole time-of-day resolution -- added 2026-09-02,
    # same manual "run until the fleet is actually through" mode as the two
    # budget bypasses above: forces every pass to resolve Active_Posting so
    # a backlog can clear across the 23:00 Berlin boundary instead of the
    # very next pass silently switching over to Warmup on the clock alone.
    if os.environ.get("ADBBOT_GEELARK_FORCE_ACTIVE_POSTING", "0").strip().lower() in ("1", "true"):
        return lifecycle.TAG_ACTIVE_POSTING
    return lifecycle.TAG_WARMUP if in_warmup_window(now) else lifecycle.TAG_ACTIVE_POSTING


def _next_night_window_start(now: datetime) -> datetime:
    """The next 23:00 Berlin at or after `now` -- today's if `now` is still
    before it, otherwise tomorrow's."""
    berlin_now = now.astimezone(BERLIN)
    candidate = berlin_now.replace(hour=WARMUP_WINDOW_START_HOUR, minute=0,
                                   second=0, microsecond=0)
    if candidate <= berlin_now:
        candidate += timedelta(days=1)
    return candidate


def _next_day_posting_fire(now: datetime) -> datetime | None:
    """The next of DAY_POSTING_FIRE_TIMES_BERLIN strictly after `now`, today.
    None once `now` is past all of today's -- the night boundary is always
    earlier than tomorrow's 07:00 anyway, so there is nothing to gain from
    wrapping to the next day here."""
    berlin_now = now.astimezone(BERLIN)
    for hour, minute in DAY_POSTING_FIRE_TIMES_BERLIN:
        candidate = berlin_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > berlin_now:
            return candidate
    return None


def active_posting_budget_seconds(now: datetime | None = None) -> float:
    """How many seconds an Active_Posting pass starting at `now` has before
    it must stop claiming new profiles -- `NIGHT_WINDOW_SAFETY_BUFFER_SECONDS`
    before whichever comes first: the next 23:00 Berlin, or the day-posting
    timer's own next scheduled fire (see DAY_POSTING_FIRE_TIMES_BERLIN).
    A duration, not an absolute deadline, on purpose: `run_queue` turns it
    into a real deadline by adding it to its own wall-clock start time, so a
    caller testing with a fake `now` (see `tests/test_geelark_scheduler.py`)
    gets a budget measured against that fake `now`, not one the real clock
    has no relationship to.

    The fire-time half exists so a long pass yields control back at each
    scheduled slot instead of silently absorbing it -- systemd does not
    start a second instance of an already-active oneshot service, so
    without this, a pass that happened to still be running at 12:30 or
    18:30 meant that slot did nothing at all, including never picking up
    whatever code changed since the pass started. The night-boundary half
    is what makes the fleet's size a visible, self-limiting problem instead
    of a silent one as more profiles are added over time -- three (or six,
    or however many later) fixed fire times never by themselves guarantee a
    pass finishes before the next one; only checking against the actual
    clock does, at any fleet size. See run_queue's deadline handling for
    what happens when a pass doesn't fit either boundary -- it stops
    cleanly and logs how much of the worklist was reached, rather than
    getting killed mid-cycle.
    """
    now = now or datetime.now(BERLIN)
    deadlines = []
    # Off-switch for the night-window half of the boundary -- added
    # 2026-09-02 for the same manual "run until the fleet is actually
    # through" mode as the fire-time switch below, for a night where the
    # night-sequence timer itself is disabled (so there is no Warmup/human-
    # verification handoff to protect) and the day's posting pass should
    # keep going past 23:00 Berlin instead of stopping with a backlog.
    ignore_night_boundary = os.environ.get(
        "ADBBOT_GEELARK_IGNORE_NIGHT_BUDGET", "0").strip().lower() in ("1", "true")
    if not ignore_night_boundary:
        deadlines.append(_next_night_window_start(now) - timedelta(
            seconds=NIGHT_WINDOW_SAFETY_BUFFER_SECONDS))
    # Off-switch for the fire-time half of the boundary -- added 2026-09-01
    # for a manual "run until the fleet is actually through" day, requested
    # explicitly rather than waiting out the normal fire-time yield points.
    # The night boundary above still applies unconditionally by default: it
    # protects the nightly in-review-recheck/human-verification/Warmup
    # sequence, not just this pass's own tidiness -- unless disabled too.
    ignore_fire_boundary = os.environ.get(
        "ADBBOT_GEELARK_IGNORE_FIRE_BUDGET", "0").strip().lower() in ("1", "true")
    if not ignore_fire_boundary:
        next_fire = _next_day_posting_fire(now)
        if next_fire is not None:
            deadlines.append(next_fire - timedelta(seconds=NIGHT_WINDOW_SAFETY_BUFFER_SECONDS))
    if not deadlines:
        # Both boundaries disabled -- fall back to a generous cap so a
        # stuck pass still ends instead of running forever unbounded.
        deadlines.append(now + timedelta(hours=12))
    deadline_dt = min(deadlines)
    return (deadline_dt - now).total_seconds()


def run_queue(worklist: list[dict], work_fn, concurrency: int = CONCURRENCY,
             budget_seconds: float | None = None, logger=None) -> list[dict]:
    """Keeps exactly `concurrency` cycles running at once, refilling from
    `worklist` (Geelark phone rows, each needs "id" and "serialName") as
    slots free up. `work_fn(phone_id, name) -> dict` is one full cycle
    (warmup or active-posting) for one phone.

    `budget_seconds`, if given, is turned into a real deadline right here
    (`time.time() + budget_seconds`) and checked only before a worker claims
    its *next* item -- never mid-cycle, so a phone already launched always
    runs to completion and is always closed. Taking a duration rather than
    an absolute deadline is what keeps this testable with a fake `now`
    upstream (`geelark_scheduler.active_posting_budget_seconds`): the
    duration is still meaningful however that `now` was constructed, whereas
    an absolute deadline computed from a fake `now` would be compared
    against the real wall clock and could already be in the past. This is
    also what keeps the fleet's size from being a silent problem: as long as
    each day-posting pass logs how much of the worklist it actually reached,
    a fleet that has outgrown its window shows up as "cut off at N/M" in the
    log rather than an external kill leaving a phone open mid-post. Added
    2026-08-31 -- an external RuntimeMaxSec kill (systemd's blunt tool for
    "must not run into the night window") can land mid-launch or mid-post.
    """
    deadline = time.time() + budget_seconds if budget_seconds is not None else None

    pending: queue.Queue = queue.Queue()
    for row in worklist:
        pending.put(row)

    results: list[dict] = []
    results_lock = threading.Lock()

    def worker():
        while True:
            if deadline is not None and time.time() >= deadline:
                return
            try:
                row = pending.get_nowait()
            except queue.Empty:
                return
            phone_id = str(row.get("id"))
            name = str(row.get("serialName") or phone_id)
            out = work_fn(phone_id, name)
            with results_lock:
                results.append(out)
            pending.task_done()

    threads = [threading.Thread(target=worker, daemon=True)
              for _ in range(min(concurrency, len(worklist)) or 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if deadline is not None and not pending.empty():
        remaining = pending.qsize()
        if logger:
            logger.warning(
                "geelark_scheduler: deadline reached with %s/%s profiles still "
                "unprocessed -- the fleet no longer fits its window at the "
                "current size/concurrency; consider a earlier start, a shorter "
                "POSTS_PER_LAUNCH, or more real proxies", remaining, len(worklist))

    return results


def run_in_review_recheck_pass(adb_client, transport: GeelarkTransport | None = None,
                               logger=None, concurrency: int = CONCURRENCY
                               ) -> list[dict]:
    """Once-a-day sweep of every `in review`-tagged profile (confirmed
    protocol 2026-08-30): read-only screen check, retags itself back into
    Active_Posting once Instagram clears the review, no manual look needed.

    Meant to run once, e.g. right before the night's Warmup pass or before
    the first Active_Posting wave -- call this from wherever that trigger
    lives (a systemd timer alongside the other adbbot-* ones, or a manual
    invocation), not from inside `run_scheduled_pass` itself: that one fires
    every cycle, this one is once a day.
    """
    from adb_bot.automation.flows.verification import TAG_IN_REVIEW

    transport = transport or GeelarkTransport()
    worklist = lifecycle.phones_by_tag(TAG_IN_REVIEW, transport=transport)

    def work_fn(phone_id, name):
        return lifecycle.run_in_review_recheck_cycle(
            phone_id, name, adb_client, transport=transport, logger=logger)

    return run_queue(worklist, work_fn, concurrency=concurrency)


# Not CONCURRENCY (4) by default -- this pass rents real SMS numbers and
# possibly pays for a captcha solve per profile, and it is meant to run
# *alongside* Warmup/Active_Posting on the same 4 real proxy ports rather
# than claim all of them. 2026-08-31 explicit instruction: 2 profiles
# warmup, 2 profiles human verification, at the same time.
HUMAN_VERIFICATION_CONCURRENCY = 2


def run_human_verification_pass(adb_client, transport: GeelarkTransport | None = None,
                                logger=None,
                                concurrency: int = HUMAN_VERIFICATION_CONCURRENCY
                                ) -> list[dict]:
    """Works the `human verification`-tagged fleet, spending real money
    (SMS numbers, a captcha solve where needed) via
    `geelark_lifecycle.run_human_verification_cycle`. Called from
    `run_night_sequence` (2026-08-31), between the in-review recheck and
    Warmup -- kept to that one nightly slot rather than its own timer, so it
    never runs unattended outside a time someone chose deliberately. Can
    still be invoked directly (`--human-verification`) for a manual, one-off
    run."""
    from adb_bot.automation.flows.verification import TAG_HUMAN_VERIFICATION

    transport = transport or GeelarkTransport()
    worklist = lifecycle.phones_by_tag(TAG_HUMAN_VERIFICATION, transport=transport)

    def work_fn(phone_id, name):
        return lifecycle.run_human_verification_cycle(
            phone_id, name, adb_client, transport=transport, logger=logger)

    return run_queue(worklist, work_fn, concurrency=concurrency)


def run_night_sequence(adb_client, transport: GeelarkTransport | None = None,
                       logger=None, concurrency: int = CONCURRENCY,
                       now: datetime | None = None) -> dict:
    """The confirmed nightly order, updated 2026-08-31: in-review recheck
    first (any profile it clears joins the human-verification or Warmup
    worklist the very same run), then human verification -- every profile
    tagged `human verification` at this point, including ones the recheck
    step or the day's Active_Posting just added -- and only then Warmup,
    which resolves on its own via `active_tag_for_now` since this only runs
    at night.

    One service, run sequentially rather than independently-scheduled
    timers, so the order is guaranteed rather than units racing close
    together -- and so human verification (spends real money: SMS numbers,
    captcha solves) never runs unattended outside this one nightly slot.
    """
    transport = transport or GeelarkTransport()
    review_results = run_in_review_recheck_pass(adb_client, transport=transport,
                                                logger=logger, concurrency=concurrency)
    human_verification_results = run_human_verification_pass(
        adb_client, transport=transport, logger=logger, concurrency=concurrency)
    warmup_results = run_scheduled_pass(adb_client, transport=transport, logger=logger,
                                        concurrency=concurrency, now=now)
    return {"in_review_recheck": review_results,
           "human_verification": human_verification_results,
           "warmup": warmup_results}


def _best_sms_balance(logger=None) -> float | None:
    """The best (not necessarily currently-active) SMS provider's balance --
    the breaker switches to whichever still has credit, so a flat 5sim does
    not mean SMSPool is broke too. Same check verification_runner's own
    preflight uses.

    Returns None when the check itself couldn't run (import/config problem,
    every provider's own `.balance()` raising) -- distinct from a real read
    of a low number. A caller should treat None as "assume funded, try
    anyway": failing to preflight is never itself a reason to refuse real
    work, only a genuinely low balance is.
    """
    try:
        from adb_bot.clients.sms.router import build_router
        providers = build_router(logger=logger).providers
    except Exception as exc:
        if logger:
            logger.warning("geelark_scheduler: SMS router unavailable for the "
                          "balance preflight (%s)", exc)
        return None
    best = None
    for provider in providers:
        try:
            value = float(provider.balance())
        except Exception as exc:
            if logger:
                logger.warning("geelark_scheduler: %s balance unreadable (%s)",
                              provider.name, exc)
            continue
        best = value if best is None else max(best, value)
    return best


def run_day_pass(adb_client, transport: GeelarkTransport | None = None,
                 logger=None, concurrency: int = CONCURRENCY,
                 now: datetime | None = None) -> dict:
    """Active_Posting's own pass, then human verification with whatever
    proxy capacity it leaves free -- or Warmup, if there's no SMS balance to
    work human verification with. Instruction 2026-08-31: if Active_Posting
    finishes ahead of the next scheduled fire (nothing due, fewer profiles
    than usual, whatever the reason), that freed-up capacity should not sit
    idle until the once-nightly slot, and it should always have *something*
    to do rather than nothing just because human verification specifically
    is blocked on money.

    Only runs either trailing step when the tag this pass actually resolved
    to was Active_Posting (daytime). If it resolved to Warmup instead --
    this function called outside its normal daytime slot --
    run_night_sequence already owns human verification for that window, and
    a second, unscheduled run here would double-spend the real money it
    costs (SMS numbers, captcha solves) rather than use idle capacity.

    A day with nothing tagged `human verification` (or nothing tagged
    `Warmup`, on the fallback path) costs nothing extra: the worklist comes
    back empty and that step returns immediately.
    """
    transport = transport or GeelarkTransport()
    tag = active_tag_for_now(now)
    scheduled_results = run_scheduled_pass(adb_client, transport=transport, logger=logger,
                                           concurrency=concurrency, now=now)

    # Resolve whatever post_ledger shares are old enough to check -- added
    # 2026-08-31, found live: GeeLark shares had no deferred-recheck path at
    # all (recheck_runner.py is Airtable-bound), so "uncertain" just sat
    # there forever. Runs every hourly fire; a share younger than
    # geelark_recheck.RECHECK_AFTER_SECONDS is simply not in this pass's
    # worklist yet, so this costs nothing on an hour with nothing due.
    #
    # Deliberately placed right after the posting pass, ahead of human
    # verification -- moved 2026-08-31 after human verification (SMS-bound,
    # can run 30+ min on a bad provider day) starved recheck of a turn for
    # a full hourly cycle even though recheck itself is bounded and fast.
    # The video-posting backlog this exists for matters more than verification
    # throughput, and unlike verification it never waits on external SMS state.
    #
    # Off-switch added 2026-09-02, explicit instruction: recheck's real
    # launches (not the free no-launch "abandoned" path) share the same 4
    # proxy slots as posting itself -- measured live that day at ~104 real
    # relaunches just to reread a post count, real capacity taken from
    # posting while a reworked approach is designed separately. Unset or
    # any value other than "0"/"false" leaves it on.
    recheck_enabled = os.environ.get(
        "ADBBOT_GEELARK_RECHECK_ENABLED", "1").strip().lower() not in ("0", "false")
    if recheck_enabled:
        from adb_bot.automation.geelark_recheck import run_geelark_recheck
        recheck_results = run_geelark_recheck(adb_client, transport=transport, logger=logger)
    else:
        recheck_results = []
        if logger:
            logger.info("geelark_scheduler: recheck disabled via "
                       "ADBBOT_GEELARK_RECHECK_ENABLED -- skipping this pass")

    # A real posting attempt already reads a phone's screen once before
    # doing anything else and retags it away from Active_Posting if it's
    # unhealthy -- but only for phones the day's worklist actually reached.
    # Added 2026-08-31: of 172 Active_Posting phones that day, 92 never got
    # a post attempt at all, so their screen state was never looked at.
    # This sweeps whichever of them haven't been checked yet today (by a
    # real post or by this sweep itself) -- same reasoning as recheck: runs
    # every hourly fire, costs nothing once today's phones are all covered.
    from adb_bot.automation.geelark_account_check import run_geelark_account_check
    account_check_results = run_geelark_account_check(adb_client, transport=transport,
                                                       logger=logger, now=now)

    human_verification_results: list[dict] = []
    warmup_results: list[dict] = []
    # Off-switch for the whole human-verification step -- added 2026-08-31 so
    # it can be paused (focus capacity on posting/recheck instead) without a
    # code change. Unset or any value other than "0"/"false" leaves it on.
    verification_enabled = os.environ.get(
        "ADBBOT_GEELARK_HUMAN_VERIFICATION_ENABLED", "1").strip().lower() not in ("0", "false")
    if tag == lifecycle.TAG_ACTIVE_POSTING and not verification_enabled:
        if logger:
            logger.info("geelark_scheduler: human verification disabled via "
                       "ADBBOT_GEELARK_HUMAN_VERIFICATION_ENABLED -- skipping this pass")
    elif tag == lifecycle.TAG_ACTIVE_POSTING:
        from adb_bot.automation.verification_runner import MIN_BALANCE_TO_START
        balance = _best_sms_balance(logger=logger)
        if balance is None or balance >= MIN_BALANCE_TO_START:
            human_verification_results = run_human_verification_pass(
                adb_client, transport=transport, logger=logger, concurrency=concurrency)
        else:
            if logger:
                logger.info("geelark_scheduler: best SMS balance %.2f is under the "
                          "%.2f floor -- running Warmup instead of human verification "
                          "so the freed-up capacity isn't idle", balance, MIN_BALANCE_TO_START)
            warmup_results = run_scheduled_pass(adb_client, transport=transport, logger=logger,
                                                concurrency=concurrency, now=now,
                                                tag=lifecycle.TAG_WARMUP)
    return {"scheduled": scheduled_results, "human_verification": human_verification_results,
           "warmup": warmup_results, "recheck": recheck_results,
           "account_check": account_check_results}


def run_scheduled_pass(adb_client, transport: GeelarkTransport | None = None,
                       logger=None, concurrency: int = CONCURRENCY,
                       now: datetime | None = None, tag: str | None = None) -> list[dict]:
    """One pass: pick the tag for the current time, list its profiles, run
    them 4-at-a-time through the matching lifecycle cycle.

    `tag` overrides the time-of-day resolution (`active_tag_for_now`) --
    used by `run_day_pass` to run a Warmup pass outside its normal nightly
    window, as the fallback when there's no SMS balance for human
    verification. Leave it None for the normal, clock-driven behavior.

    Active_Posting stops claiming new profiles `NIGHT_WINDOW_SAFETY_BUFFER_SECONDS`
    before the next 23:00 Berlin (`active_posting_deadline`), whatever fire
    time this pass started at -- a phone already launched always finishes and
    closes normally, only the *next* claim is refused past the deadline. This
    is deliberately a deadline the code itself enforces, not just a systemd
    RuntimeMaxSec: an external kill can land mid-launch or mid-post, and
    neither fixed fire times nor a fixed kill timeout stay correct as the
    fleet grows -- the clock check does, at any size, and logs "N/M reached"
    when it doesn't fit rather than failing silently.

    Active_Posting resolves its own content per phone (today's Drive date
    folder for that phone's model -- geelark_content.get_post_media, no
    caption per the confirmed 2026-08-30 decision) rather than taking a
    single media_path for the whole pass, since different phones belong to
    different models. Up to `POSTS_PER_LAUNCH` distinct videos are resolved
    per phone per pass (changed 2026-08-31, was one) and posted in the same
    launch -- get_post_media's own dedup (by name, checked against both the
    post_ledger and this batch's own earlier picks) means a model with fewer
    videos than POSTS_PER_LAUNCH today just gets a shorter batch, never a
    repeat. No content at all today for a phone's model means that phone is
    never launched at all (changed 2026-09-01 -- was launched anyway for a
    "free" challenge check; geelark_account_check.py now covers that need
    directly, once a day, for whichever phones a post never reaches, so the
    launch was only ever duplicating that check. Found live: 25 no-content
    launches for one model alone cost ~80 of 99 total minutes spent on it
    that day, for zero posts). Not an error, not a fallback to older
    content. Every spoofed variant in the batch is deleted after the cycle
    either way; nothing else tracks or cleans these up.
    """
    transport = transport or GeelarkTransport()
    tag = tag or active_tag_for_now(now)
    worklist = lifecycle.phones_by_tag(tag, transport=transport)

    # Push one or more models' phones to the back of the worklist -- added
    # 2026-09-01 for a manual run where a model's Drive content lands later
    # than the others' (so hitting its phones early would just be "no
    # content, skip" instead of a real attempt), and extended the same day
    # to a second, unrelated reason: a model whose phones are all failing
    # at launch right now (found live: Luisa alone ate 8 fully-exhausted
    # launch attempts and 11+ minutes with zero other model even touched,
    # while every other model's phones sat untried behind it in the queue).
    # Comma-separated, case-insensitive. Stable partition, not a sort:
    # everything keeps its relative order within its own half.
    defer_models = {
        name.strip().lower()
        for name in os.environ.get("ADBBOT_GEELARK_DEFER_MODEL", "").split(",")
        if name.strip()
    }
    if defer_models:
        def _is_deferred(row):
            return ((row.get("group") or {}).get("name") or "").lower() in defer_models
        worklist = (
            [row for row in worklist if not _is_deferred(row)]
            + [row for row in worklist if _is_deferred(row)]
        )

    budget_seconds = None

    if tag == lifecycle.TAG_WARMUP:
        def work_fn(phone_id, name):
            return lifecycle.run_warmup_cycle(phone_id, name, adb_client,
                                              transport=transport, logger=logger)
    else:
        budget_seconds = active_posting_budget_seconds(now)
        model_by_id = {str(row.get("id")): (row.get("group") or {}).get("name")
                      for row in worklist}

        def work_fn(phone_id, name):
            from adb_bot.automation import geelark_content

            model = model_by_id.get(phone_id)
            spoofed_batch = []
            if model:
                excluded = set()
                for _ in range(POSTS_PER_LAUNCH):
                    spoofed = geelark_content.get_post_media(
                        model, phone_id, logger=logger, exclude_names=excluded)
                    if spoofed is None:
                        break
                    spoofed_batch.append(spoofed)
                    excluded.add(spoofed.raw_video.name)
            try:
                return lifecycle.run_active_posting_cycle(
                    phone_id, name, adb_client, transport=transport, logger=logger,
                    media_paths=[s.path for s in spoofed_batch])
            finally:
                for spoofed in spoofed_batch:
                    try:
                        Path(spoofed.path).unlink()
                    except OSError:
                        pass

    return run_queue(worklist, work_fn, concurrency=concurrency,
                     budget_seconds=budget_seconds, logger=logger)


if __name__ == "__main__":
    import argparse
    import sys

    from adb_bot.clients.adb import ADBClient
    from adb_bot.core.logger import get_logger

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-review-recheck", action="store_true",
                       help="run only the once-daily in-review screen check")
    parser.add_argument("--night-sequence", action="store_true",
                       help="run the in-review recheck, then the regular "
                            "window pass (Warmup, at night) -- the confirmed "
                            "once-nightly order")
    parser.add_argument("--human-verification", action="store_true",
                       help="work the human-verification-tagged fleet -- "
                            "spends real money (SMS numbers, captcha "
                            "solves); runs automatically as part of "
                            "--night-sequence, this flag is for a manual "
                            "one-off run")
    parser.add_argument("--concurrency", type=int, default=None,
                       help="override the default concurrency for this run")
    args = parser.parse_args()

    logger = get_logger("adb_bot")
    adb_client = ADBClient()
    recheck_results: list[dict] = []
    account_check_results: list[dict] = []

    if args.night_sequence:
        print(f"night sequence: recheck -> warmup ({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        combined = run_night_sequence(adb_client, logger=logger)
        results = (combined["in_review_recheck"] + combined["human_verification"]
                  + combined["warmup"])
    elif args.in_review_recheck:
        print(f"in-review recheck ({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        results = run_in_review_recheck_pass(adb_client, logger=logger)
    elif args.human_verification:
        concurrency = args.concurrency or HUMAN_VERIFICATION_CONCURRENCY
        print(f"human verification, {concurrency} concurrent "
             f"({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        results = run_human_verification_pass(adb_client, logger=logger,
                                              concurrency=concurrency)
    else:
        tag = active_tag_for_now()
        print(f"window: {tag} ({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        kwargs = {"concurrency": args.concurrency} if args.concurrency else {}
        combined = run_day_pass(adb_client, logger=logger, **kwargs)
        results = (combined["scheduled"] + combined["human_verification"]
                  + combined["warmup"])
        recheck_results = combined["recheck"]
        account_check_results = combined["account_check"]

    print(f"{len(results)} profile(s) processed")
    for r in results:
        suffix = f" ({r['error']})" if r.get("error") else ""
        print(f"  {r.get('name')}: {r.get('result')}{suffix}")
    if recheck_results:
        print(f"{len(recheck_results)} geelark_recheck entrie(s) processed")
        for r in recheck_results:
            print(f"  {r.get('phone_id')}: {r.get('outcome')}")
    if account_check_results:
        print(f"{len(account_check_results)} geelark_account_check phone(s) processed")
        for r in account_check_results:
            print(f"  {r.get('name')}: {r.get('result')}")
    if not results:
        sys.exit(0)
