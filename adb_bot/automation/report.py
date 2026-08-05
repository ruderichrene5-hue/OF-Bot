"""Operational report: what ran today, what is running now, what failed.

Everything here is *read-only* and derived from sources that already exist --
the Posting Queue, the post ledger, the loop logs, the lock directory, the
watchdog state and systemd. Nothing new is recorded to build this page, so the
report can never be the reason a number is wrong; it can only be late.

Two consumers, one collector:

- `report_server` renders `collect()` on each request, so a browser on the box
  sees the fleet as it is right now.
- `run_loop report` writes the same HTML to a file, for sharing a snapshot with
  somebody who is not on the box.

The Airtable read is the only expensive part, so it is cached briefly
(`CACHE_SECONDS`). A dashboard that is refreshed every few seconds must not be
able to rate-limit the loops that are actually doing the work.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from adb_bot.automation import schedule_spec

# How long a collected snapshot may be reused. Short enough that the page is
# honest about "running now", long enough that holding the refresh key cannot
# turn into an Airtable rate limit for the posting loop.
CACHE_SECONDS = 20.0

# Loop logs live here; the posting log carries the per-run summary lines this
# module parses for wall-clock and launch health.
LOG_DIR = "logs"
POSTING_LOG = "loop_posting.log"

_RUN_START = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Posting (?P<planned>\d+) profile\(s\)")
_RUN_DONE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Posting run complete \((?P<posts>\d+) post")
_LAUNCH_HEALTH = re.compile(
    r"(?P<attempts>\d+) attempt\(s\), (?P<ok>\d+) ok, (?P<mlx>\d+) MLX-side 500 "
    r"\((?P<rate>[\d.]+)%\), (?P<other>\d+) our-side/other failure\(s\), "
    r"(?P<burned>\d+) queue retry")

# Strip the ANSI colour the logger writes to a tty; the log file keeps it.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _repo_root() -> Path:
    return schedule_spec.repo_root()


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


# --- what is happening right now ---------------------------------------------

def systemd_state(loops=None) -> list:
    """(loop, active-state) for each scheduled loop, newest state from systemd.

    One `systemctl show` call for every unit at once: asking per unit is eight
    subprocesses per page load, which is the kind of cost that makes people
    stop opening the dashboard.
    """
    loops = list(loops or schedule_spec.RECOMMENDED_LOOPS)
    units = [schedule_spec.unit_name(loop) for loop in loops]
    try:
        out = subprocess.run(
            ["systemctl", "show", "--property=Id", "--property=ActiveState", *units],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return [(loop, "unknown") for loop in loops]

    states: dict = {}
    current: dict = {}
    for line in out.splitlines():
        if not line.strip():
            if current.get("Id"):
                states[current["Id"]] = current.get("ActiveState", "unknown")
            current = {}
            continue
        key, _, value = line.partition("=")
        current[key] = value
    if current.get("Id"):
        states[current["Id"]] = current.get("ActiveState", "unknown")

    return [(loop, states.get(schedule_spec.unit_name(loop), "unknown")) for loop in loops]


def live_phones() -> int:
    """Phone processes MultiLogin currently has open, counted from the OS.

    Deliberately not the slot count: slots are what we *think* is open, and the
    gap between the two is the leak that OOM-killed this box on 2026-08-04.
    Showing both side by side is the point.
    """
    try:
        out = subprocess.run(["pgrep", "-fc", "phone_launcher_linux"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip() or 0)
    except Exception:
        return 0


def running_now() -> dict:
    """Loops active, profiles locked, slots held, phones open."""
    from adb_bot.core import locks

    try:
        slots = locks.held_slots()
    except Exception:
        slots = []
    try:
        ceiling = locks.max_live_profiles()
    except Exception:
        ceiling = 0
    try:
        held = sorted(p.stem for p in locks.lock_dir().glob("*.lock"))
    except Exception:
        held = []

    states = systemd_state()
    return {
        "loops": states,
        "active_loops": [name for name, state in states if state in ("active", "activating")],
        "profiles": held,
        "slots_held": len(slots),
        "slot_ceiling": ceiling,
        "phones": live_phones(),
        "agent_up": mlx_agent_up(),
    }


def mlx_agent_up() -> bool:
    """Whether anything is listening on the MultiLogin agent's port.

    Its own supervision is still a hand-started process, so this is worth its
    own tile rather than being buried in the doctor section.
    """
    try:
        out = subprocess.run(["ss", "-lnt"], capture_output=True, text=True, timeout=10).stdout
        return ":45001" in out
    except Exception:
        return False


# --- what ran today -----------------------------------------------------------

@dataclass
class RunSummary:
    """One posting run, reconstructed from its own log lines."""

    started: str = ""
    finished: str = ""
    planned: int = 0
    posts: int = 0
    seconds: float = 0.0
    attempts: int = 0
    ok: int = 0
    mlx_500: int = 0
    mlx_rate: float = 0.0
    other_failures: int = 0
    retries_burned: int = 0

    @property
    def seconds_per_post(self) -> float:
        return (self.seconds / self.posts) if self.posts else 0.0


def rotated_logs(path: Path) -> list:
    """`path` and its RotatingFileHandler backups, oldest first.

    The loop logger rotates at 5 MB keeping 5 backups, and a busy posting night
    rotates inside the day -- on 2026-08-05 the log rolled at 17:26 and took
    every earlier run of that day with it. Reading only the live file made the
    report say "0 runs today" an hour after 48 posts went out, so the backups
    are part of the source, not an optional extra.
    """
    backups = []
    for index in range(1, 10):
        candidate = path.with_name(f"{path.name}.{index}")
        if candidate.exists():
            backups.append(candidate)
        else:
            break
    return list(reversed(backups)) + [path]      # .3, .2, .1, then the live one


def parse_posting_runs(log_path=None, day: str = "") -> list:
    """Reconstruct today's posting runs from the loop log and its backups.

    Pairs each "Posting N profile(s)" with the "Posting run complete" that
    follows it. A run with no completion line is still reported -- that is
    exactly the shape of a run that was killed or is still going, and dropping
    it would hide the thing worth seeing.
    """
    path = Path(log_path) if log_path else (_repo_root() / LOG_DIR / POSTING_LOG)
    # A backup last written before the day we are reporting on cannot contain a
    # line from it. Skipping those keeps a page load off ~25 MB of old logs.
    cutoff = 0.0
    if day:
        try:
            cutoff = datetime.strptime(day, "%Y-%m-%d").timestamp()
        except ValueError:
            cutoff = 0.0

    lines: list = []
    for candidate in rotated_logs(path):
        try:
            if cutoff and candidate.stat().st_mtime < cutoff:
                continue
            lines.extend(candidate.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    if not lines:
        return []

    runs: list = []
    open_run = None
    for raw in lines:
        line = _ANSI.sub("", raw)
        start = _RUN_START.search(line)
        if start:
            if open_run is not None:
                runs.append(open_run)          # previous run never completed
            open_run = RunSummary(started=start.group("ts"),
                                  planned=int(start.group("planned")))
            continue
        done = _RUN_DONE.search(line)
        if done and open_run is not None:
            open_run.finished = done.group("ts")
            open_run.posts = int(done.group("posts"))
            try:
                t0 = datetime.strptime(open_run.started, "%Y-%m-%d %H:%M:%S")
                t1 = datetime.strptime(open_run.finished, "%Y-%m-%d %H:%M:%S")
                open_run.seconds = max(0.0, (t1 - t0).total_seconds())
            except ValueError:
                open_run.seconds = 0.0
            health = _LAUNCH_HEALTH.search(line)
            if health:
                open_run.attempts = int(health.group("attempts"))
                open_run.ok = int(health.group("ok"))
                open_run.mlx_500 = int(health.group("mlx"))
                open_run.mlx_rate = float(health.group("rate"))
                open_run.other_failures = int(health.group("other"))
                open_run.retries_burned = int(health.group("burned"))
            runs.append(open_run)
            open_run = None
    if open_run is not None:
        runs.append(open_run)

    if day:
        runs = [r for r in runs if r.started.startswith(day)]
    return runs


def ledger_today(ledger=None, day: str = "") -> dict:
    """Shares this machine actually made today, by status."""
    from adb_bot.automation.post_ledger import PostLedger

    ledger = ledger or PostLedger()
    try:
        records = list(ledger.load().values())
    except Exception:
        return {"total": 0, "by_status": {}, "verify_seconds": 0.0}

    todays = []
    for record in records:
        shared = getattr(record, "shared_at", 0.0) or 0.0
        if not shared:
            continue
        if day and datetime.fromtimestamp(shared).strftime("%Y-%m-%d") != day:
            continue
        todays.append(record)

    latencies = [r.resolved_at - r.shared_at for r in todays
                 if getattr(r, "resolved_at", 0.0) and r.resolved_at > r.shared_at]
    return {
        "total": len(todays),
        "by_status": dict(Counter(getattr(r, "status", "?") for r in todays)),
        "verify_seconds": (sum(latencies) / len(latencies)) if latencies else 0.0,
    }


# --- Airtable-derived ---------------------------------------------------------

def queue_today(airtable, day: str) -> dict:
    """Posting Queue rows scheduled today, grouped, plus the failure detail."""
    rows = airtable.list_queue_rows()
    fields = lambda r: (r.get("fields") or {})            # noqa: E731
    todays = [r for r in rows
              if str(fields(r).get("Scheduled DateTime", "")).startswith(day)]

    failures = []
    for row in todays:
        f = fields(row)
        if f.get("Post Status") != "Failed":
            continue
        failures.append({
            "name": f.get("Name") or "(unnamed)",
            "issue": f.get("Issue Type") or "-",
            "retries": f.get("Retry Count") or 0,
            "notes": (f.get("Notes") or "")[:160],
            "slot": str(f.get("Scheduled DateTime", ""))[11:16],
        })
    failures.sort(key=lambda d: (d["issue"], d["name"]))

    by_slot = defaultdict(Counter)
    for row in todays:
        f = fields(row)
        by_slot[str(f.get("Scheduled DateTime", ""))[11:16]][f.get("Post Status") or "?"] += 1

    return {
        "total": len(todays),
        "by_status": dict(Counter(fields(r).get("Post Status") or "(empty)" for r in todays)),
        "by_slot": {slot: dict(counts) for slot, counts in sorted(by_slot.items())},
        "failures": failures,
    }


def content_stock(airtable) -> dict:
    """Ready variants that no queue row has claimed -- i.e. postable content."""
    try:
        variants = airtable.list_ready_variants()
    except Exception:
        return {"ready": 0, "by_model": {}}

    # `list_ready_variants` returns its own flattened dicts, not raw Airtable
    # records -- the model comes out of the spoofed file's path.
    # Folder case is not consistent on disk ("Jasmin" and "jasmin" both exist),
    # and two spellings of one model reads as two models with half the stock
    # each -- exactly the wrong thing on a page you check before a run.
    per_model = Counter()
    for variant in variants:
        path = str(variant.get("file_path") or "")
        match = re.search(r"/spoofed/([^/]+)/", path)
        per_model[match.group(1).capitalize() if match else "unknown"] += 1
    return {"ready": len(variants), "by_model": dict(sorted(per_model.items()))}


# --- health -------------------------------------------------------------------

def health() -> dict:
    """Watchdog state per loop, including doctor's own health entry."""
    from adb_bot.automation import loop_watchdog

    try:
        states = loop_watchdog.LoopWatchdog(sinks=[]).snapshot()
    except Exception:
        return {"loops": [], "bad": []}

    rows = []
    for name, state in sorted(states.items()):
        rows.append({
            "loop": name,
            "state": state.state,
            "due": state.last_due,
            "produced": state.last_produced,
            "last_seen": state.last_seen,
            "detail": state.detail or "",
            "bad": state.state in loop_watchdog.BAD_STATES,
        })
    return {"loops": rows, "bad": [r for r in rows if r["bad"]]}


def recent_alerts(limit: int = 8) -> list:
    path = _repo_root() / loop_alerts_file()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [_ANSI.sub("", line) for line in lines[-limit:]]


def loop_alerts_file() -> str:
    from adb_bot.automation import loop_watchdog
    return loop_watchdog.ALERT_LOG_FILE


# --- the whole picture --------------------------------------------------------

_cache: dict = {"at": 0.0, "data": None}


def collect(airtable=None, now=None, use_cache: bool = True) -> dict:
    """Everything the page shows. Cached for `CACHE_SECONDS`."""
    if use_cache and _cache["data"] is not None and (time.time() - _cache["at"]) < CACHE_SECONDS:
        return _cache["data"]

    now = now or datetime.now()
    day = _today(now)

    runs = parse_posting_runs(day=day)
    posts_today = sum(r.posts for r in runs)
    run_seconds = sum(r.seconds for r in runs if r.seconds)
    attempts = sum(r.attempts for r in runs)
    mlx_500 = sum(r.mlx_500 for r in runs)

    data = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "day": day,
        "now": running_now(),
        "runs": runs,
        "totals": {
            "runs": len(runs),
            "posts": posts_today,
            "seconds": run_seconds,
            "seconds_per_post": (run_seconds / posts_today) if posts_today else 0.0,
            "attempts": attempts,
            "mlx_500": mlx_500,
            "mlx_rate": (100.0 * mlx_500 / attempts) if attempts else 0.0,
            "retries_burned": sum(r.retries_burned for r in runs),
            "other_failures": sum(r.other_failures for r in runs),
        },
        "ledger": ledger_today(day=day),
        "health": health(),
        "alerts": recent_alerts(),
        "queue": {"total": 0, "by_status": {}, "by_slot": {}, "failures": []},
        "content": {"ready": 0, "by_model": {}},
        "airtable_error": "",
    }

    if airtable is not None:
        try:
            data["queue"] = queue_today(airtable, day)
            data["content"] = content_stock(airtable)
        except Exception as exc:
            # A dashboard that 500s because Airtable is having a moment is worse
            # than one that says so and still shows everything local.
            data["airtable_error"] = f"{type(exc).__name__}: {exc}"

    _cache.update(at=time.time(), data=data)
    return data


def invalidate_cache() -> None:
    _cache.update(at=0.0, data=None)
