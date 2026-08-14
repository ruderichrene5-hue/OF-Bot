"""Keep the number of phones running at once to something the box can take.

Both runners used to launch *every* due profile up front and then hand the whole
list to a thread pool sized to match. With ~80 due accounts that meant ~80
MultiLogin profiles booted simultaneously and ~80 worker threads -- far more than
a single server can drive, and the fastest way to make every flow flaky at once.

The first fix processed work in fixed batches: launch a batch, wait for **all**
of it, move on. That capped the load but wasted a lot of time -- a batch runs
only as fast as its slowest profile, and every phone that finished early sat idle
until the laggard was done. With a 3-minute post-verification floor, one slow
account could hold four finished ones hostage.

`run_rolling` replaces that with a sliding window: `concurrency` units run at
once, and the moment one finishes the next starts. Same ceiling on live phones,
no idle waiting. `chunked` is kept for callers that genuinely want fixed groups.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# How many MultiLogin profiles one loop may DRIVE at the same moment. Note what
# this does not bound: the phone's OS processes outlive the window. A profile
# the bot has finished with keeps its WebKitWebProcess (~150 MB) alive for tens
# of minutes after shutdown is issued, and each loop applies this cap
# independently -- posting and warmup do not know about each other.
#
# On 2026-08-04 that gap emptied the box twice. The kernel's OOM dump at the
# first kill counted 78 WebKitWebProcess (12.8 GB) and 77 phone processes
# (3.6 GB) alive at once: 17.7 GB of RSS on 15.6 GB of RAM with no swap. The
# cap was doing its job -- five profiles were being driven -- while seventy-odd
# phones from earlier runs had not yet gone away. The OOM killer took the user
# session's systemd and dbus, which took the MultiLogin agent down with them.
#
# Raised to 10 once the leak itself was fixed: `run_profile_workflow` now closes
# every phone on every exit path and force-closes anything still open after
# MAX_PROFILE_OPEN_SECONDS (7 min). With phones actually being reaped, the live
# count tracks this cap instead of climbing all evening, so 10 x ~150 MB is a
# bounded ~1.5 GB per loop rather than the unbounded growth that hit 78.
# 8 GB of swap was added the same night as a second line of defence.
#
# "Each loop applies this cap independently" is no longer the end of the story:
# `locks.live_profile_slot` is the ceiling that spans loops (default 12 phones,
# ADBBOT_MAX_LIVE_PROFILES), and every path that opens a phone takes a slot from
# it before launching. This constant still bounds one loop's rolling window --
# how much work it will try to have in flight -- while the slot decides whether
# the box can afford the next phone at all.
MAX_CONCURRENT_PROFILES = 10


def chunked(items, size: int) -> list:
    """Split `items` into consecutive lists of at most `size` (>=1)."""
    seq = list(items)
    step = max(1, int(size))
    return [seq[i:i + step] for i in range(0, len(seq), step)]


def resolve_concurrency(requested=None) -> int:
    """The effective profile-concurrency cap: a positive request, else the
    default. Never returns less than 1."""
    try:
        value = int(requested) if requested is not None else MAX_CONCURRENT_PROFILES
    except (TypeError, ValueError):
        value = MAX_CONCURRENT_PROFILES
    return max(1, value)


class LaunchGate:
    """Serialise profile launches so the workers don't all hit MultiLogin at once.

    The window lets `concurrency` units run in parallel, but *starting* a profile
    is the one step that should stay sequential: the old batch code deliberately
    spaced launches by `batch_launch_delay_seconds`, and firing five simultaneous
    launch calls is exactly the burst that used to make profiles come up unready.
    Only the launch call is held; everything after it runs concurrently.
    """

    def __init__(self, delay_seconds: float = 0.0):
        self._lock = threading.Lock()
        self._delay = max(0.0, float(delay_seconds or 0))

    def launch(self, call):
        with self._lock:
            result = call()
            if self._delay:
                time.sleep(self._delay)
            return result


def run_rolling(units, run_unit, concurrency=None, should_stop=None, logger=None) -> dict:
    """Run `run_unit(unit)` over `units`, at most `concurrency` at a time.

    As soon as one unit finishes the next starts, so a slow unit delays only
    itself. Returns {"completed": n, "failed": n, "skipped": n, "aborted": bool}.

    A unit that raises is logged and counted, never re-raised: one bad profile
    must not abandon the rest of the run, which is what a bare `future.result()`
    over a batch would do. `should_stop` is checked before each unit starts, so
    an abort drains the queue instead of killing work already in flight.
    """
    units = list(units)
    cap = resolve_concurrency(concurrency)
    counts = {"completed": 0, "failed": 0, "skipped": 0, "aborted": False}
    lock = threading.Lock()

    def stopped() -> bool:
        return bool(callable(should_stop) and should_stop())

    def guarded(unit) -> None:
        if stopped():
            with lock:
                counts["skipped"] += 1
                counts["aborted"] = True
            return
        try:
            run_unit(unit)
        except Exception as exc:                     # noqa: BLE001 - isolation is the point
            with lock:
                counts["failed"] += 1
            if logger is not None:
                logger.exception("Unit %s failed: %s", unit, exc)
        else:
            with lock:
                counts["completed"] += 1

    if not units:
        return counts

    with ThreadPoolExecutor(max_workers=cap) as executor:
        futures = [executor.submit(guarded, unit) for unit in units]
        for future in as_completed(futures):
            future.result()      # `guarded` never raises; this just surfaces bugs
    return counts
