"""Time-windowed Geelark queue: Warmup at night, Active_Posting by day.

Confirmed schedule 2026-08-29:

* 06:00-23:00 Europe/Berlin -- only `Active_Posting`-tagged profiles run,
  each a short scroll then a post (`geelark_lifecycle.run_active_posting_cycle`).
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

import queue
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from adb_bot.automation import geelark_lifecycle as lifecycle
from adb_bot.clients.geelark.transport import GeelarkTransport

BERLIN = ZoneInfo("Europe/Berlin")
WARMUP_WINDOW_START_HOUR = 23    # inclusive
WARMUP_WINDOW_END_HOUR = 6       # exclusive

CONCURRENCY = 4    # one per real modem


def in_warmup_window(now: datetime | None = None) -> bool:
    """True during the 23:00-06:00 Berlin freeze window -- the one that
    wraps past midnight, so it's an OR, not a simple between-check."""
    now = (now or datetime.now(BERLIN))
    hour = now.astimezone(BERLIN).hour
    return hour >= WARMUP_WINDOW_START_HOUR or hour < WARMUP_WINDOW_END_HOUR


def active_tag_for_now(now: datetime | None = None) -> str:
    return lifecycle.TAG_WARMUP if in_warmup_window(now) else lifecycle.TAG_ACTIVE_POSTING


def run_queue(worklist: list[dict], work_fn, concurrency: int = CONCURRENCY) -> list[dict]:
    """Keeps exactly `concurrency` cycles running at once, refilling from
    `worklist` (Geelark phone rows, each needs "id" and "serialName") as
    slots free up. `work_fn(phone_id, name) -> dict` is one full cycle
    (warmup or active-posting) for one phone."""
    pending: queue.Queue = queue.Queue()
    for row in worklist:
        pending.put(row)

    results: list[dict] = []
    results_lock = threading.Lock()

    def worker():
        while True:
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


def run_night_sequence(adb_client, transport: GeelarkTransport | None = None,
                       logger=None, concurrency: int = CONCURRENCY,
                       now: datetime | None = None) -> dict:
    """The confirmed nightly order, 2026-08-30: in-review recheck first (any
    profile it clears joins the Warmup/Active_Posting worklist the very same
    run), then the regular window pass -- which resolves to Warmup on its
    own via `active_tag_for_now`, since this only runs at night.

    One service, run sequentially rather than two independently-scheduled
    timers, so "recheck, then warmup" is guaranteed order rather than two
    units racing close together.
    """
    transport = transport or GeelarkTransport()
    review_results = run_in_review_recheck_pass(adb_client, transport=transport,
                                                logger=logger, concurrency=concurrency)
    warmup_results = run_scheduled_pass(adb_client, transport=transport, logger=logger,
                                        concurrency=concurrency, now=now)
    return {"in_review_recheck": review_results, "warmup": warmup_results}


def run_scheduled_pass(adb_client, transport: GeelarkTransport | None = None,
                       logger=None, media_path: str | None = None,
                       caption: str | None = None, concurrency: int = CONCURRENCY,
                       now: datetime | None = None) -> list[dict]:
    """One pass: pick the tag for the current time, list its profiles, run
    them 4-at-a-time through the matching lifecycle cycle.

    `media_path`/`caption` only matter during the day window -- Active_Posting
    posts if given one, otherwise just runs the pre-post scroll (see
    `geelark_lifecycle.run_active_posting_cycle`). Warmup never posts.
    """
    transport = transport or GeelarkTransport()
    tag = active_tag_for_now(now)
    worklist = lifecycle.phones_by_tag(tag, transport=transport)

    if tag == lifecycle.TAG_WARMUP:
        def work_fn(phone_id, name):
            return lifecycle.run_warmup_cycle(phone_id, name, adb_client,
                                              transport=transport, logger=logger)
    else:
        def work_fn(phone_id, name):
            return lifecycle.run_active_posting_cycle(
                phone_id, name, adb_client, transport=transport, logger=logger,
                media_path=media_path, caption=caption)

    return run_queue(worklist, work_fn, concurrency=concurrency)


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
    args = parser.parse_args()

    logger = get_logger("adb_bot")
    adb_client = ADBClient()

    if args.night_sequence:
        print(f"night sequence: recheck -> warmup ({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        combined = run_night_sequence(adb_client, logger=logger)
        results = combined["in_review_recheck"] + combined["warmup"]
    elif args.in_review_recheck:
        print(f"in-review recheck ({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        results = run_in_review_recheck_pass(adb_client, logger=logger)
    else:
        tag = active_tag_for_now()
        print(f"window: {tag} ({datetime.now(BERLIN).strftime('%H:%M %Z')})")
        results = run_scheduled_pass(adb_client, logger=logger)

    print(f"{len(results)} profile(s) processed")
    for r in results:
        print(f"  {r.get('name')}: {r.get('result')}")
    if not results:
        sys.exit(0)
