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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adb_bot.automation import schedule_spec

# How long a collected snapshot may be reused. Short enough that the page is
# honest about "running now", long enough that holding the refresh key cannot
# turn into an Airtable rate limit for the posting loop.
CACHE_SECONDS = 20.0

# Reads that only move when a person moves them: a clip uploaded to Drive, a
# posting time edited, a profile parked. They are the expensive calls on this
# page -- one of them leaves the box entirely -- and the loopback dashboard
# re-collects every 20 seconds, which would turn each of them into a request
# every 20 seconds for a number that changes a few times a week.
SLOW_CACHE_SECONDS = 180.0

# Loop logs live here; the posting log carries the per-run summary lines this
# module parses for wall-clock and launch health.
LOG_DIR = "logs"
POSTING_LOG = "loop_posting.log"
RETRY_LOG = "loop_retry.log"
RECHECK_LOG = "loop_recheck.log"

# What one profile's turn in a run came to. Ordered worst-first, which is also
# the order the page lists them in: the reason to open a run is what went wrong.
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_PENDING = "pending"
OUTCOME_SKIPPED = "skipped"
OUTCOME_VERIFYING = "verifying"
OUTCOME_POSTED = "posted"
OUTCOME_ORDER = (OUTCOME_FAILED, OUTCOME_UNKNOWN, OUTCOME_PENDING, OUTCOME_SKIPPED,
                 OUTCOME_VERIFYING, OUTCOME_POSTED)

# `<model>/run<N>/<source>__<Handle>.mp4` -- one run folder is one source video
# spoofed once per profile of that model, which is the unit a person means by
# "the run": one clip, everybody who was supposed to get it.
_SPOOF_RUN_DIR = re.compile(r"^run(\d+)$", re.I)
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")

_RUN_START = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Posting (?P<planned>\d+) profile\(s\)")
_RUN_DONE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Posting run complete \((?P<posts>\d+) post")
_PHONE_EVENT = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?"
    r"(?P<event>Launched|Closed) profile (?P<id>\d+)")
_LAUNCH_HEALTH = re.compile(
    r"(?P<attempts>\d+) attempt\(s\), (?P<ok>\d+) ok, (?P<mlx>\d+) MLX-side 500 "
    r"\((?P<rate>[\d.]+)%\), (?P<other>\d+) our-side/other failure\(s\), "
    r"(?P<burned>\d+) queue retry")

# --- one profile's turn inside a run -----------------------------------------
#
# The posting loop writes one `Post result for <name> (profile <id>): <status>`
# line per finished post (posting_runner._cb), which is all this needs. The
# other patterns reconstruct the same thing for the failures that never reach
# that line -- a launch that 500ed, a phone that never became ADB-ready -- and
# for logs written before that line existed. Together they answer "which
# profiles posted, which did not" for every profile the run touched.
_POST_RESULT = re.compile(
    r"Post result for (?P<name>.+?) \(profile (?P<id>\d+)\): (?P<status>[a-z_]+)"
    r"(?: -- (?P<detail>.*))?$")
_MEDIA_PUSH = re.compile(
    r"Preparing to push reel media for profile (?P<id>\d+): (?P<path>\S+)")
_LAUNCH_500 = re.compile(r"Failed to launch profile (?P<id>\d+) -- MultiLogin-side 500")
_LAUNCH_FAILED = re.compile(r"Failed to launch profile (?P<id>\d+): (?P<why>.*)")
_LAUNCHED = re.compile(r"Launched profile (?P<id>\d+)")
_WF_DONE = re.compile(r"Workflow completed for profile (?P<id>\d+)")
_WF_UNCERTAIN = re.compile(r"Workflow outcome UNCERTAIN for profile (?P<id>\d+)")
_WF_FAILED = re.compile(r"Workflow failed for profile (?P<id>\d+)(?:: (?P<why>.*))?$")
_WF_FLAGGED = re.compile(r"Instagram flagged profile (?P<id>\d+) \((?P<kind>[^)]+)\)")
_WF_HEARTBEAT = re.compile(r"Workflow aborted for profile (?P<id>\d+) by the heartbeat")
_WF_ADB = re.compile(r"ADB connection failed for profile (?P<id>\d+)")
_WF_NOT_READY = re.compile(r"Profile (?P<id>\d+) is not ready for ADB automation")
_WF_ALREADY_SHARED = re.compile(
    r"Skipping profile (?P<id>\d+): this clip was already sent to it")
_CEILING_SKIP = re.compile(r"Skipping profile (?P<id>\d+) this round")

# What the retry pass decided about a failed row afterwards. Its log is the only
# place that says "this one is coming back" in words, and it names the queue row
# ("<Profile> / <slot>"), not the MLX id -- so the join is on the profile name.
_RETRY_REQUEUE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Re-queueing (?P<name>.+?) in "
    r"(?P<mins>[\d.]+) min \(attempt (?P<attempt>\d+)/(?P<max>\d+)\)")
_RETRY_PARKED = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Not retrying (?P<name>.+?): (?P<why>.*)")
_RETRY_HUMAN = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ .*?Profile needs a human for "
    r"(?P<name>.+?): (?P<why>.*)")
# `Recheck for <Name> (<id>)` is the one line anywhere that carries both, which
# makes the recheck log a free id -> name dictionary for the posting log.
_RECHECK_NAME = re.compile(r"Recheck for (?P<name>.+?) \((?P<id>\d+)\):")

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


def profile_locks() -> tuple:
    """(live, stale) profile locks, each with who holds it and for how long.

    Two lists, not one. A lock whose owner died still sits on disk until the
    45-minute TTL lets the next loop steal it, and counting those as "held by a
    running loop" reports the exact condition that cost 17 minutes of dead time
    on 2026-08-04 as if it were healthy work in progress.

    The lock file records `pid=... owner=<loop> at=<time>`, and the phone
    processes carry the profile's name in argv, so both can be shown instead of
    an 18-digit id nobody can read.
    """
    from adb_bot.core import locks

    try:
        paths = sorted(locks.lock_dir().glob("*.lock"))
    except Exception:
        return [], []

    names = {}
    try:
        from adb_bot.automation import phone_reaper
        names = {phone.profile_id: phone.name
                 for phone in phone_reaper.list_phones() if phone.profile_id}
    except Exception:
        pass

    now = time.time()
    live, stale = [], []
    for path in paths:
        try:
            payload = path.read_text(encoding="utf-8", errors="replace")
            age = max(0.0, now - path.stat().st_mtime)
        except OSError:
            continue
        owner = ""
        pid = ""
        for token in payload.split():
            key, _, value = token.partition("=")
            if key == "owner":
                owner = value
            elif key == "pid":
                pid = value
        entry = {"profile_id": path.stem, "name": names.get(path.stem, ""),
                 "owner": owner or "unknown", "pid": pid, "age_seconds": age}
        (stale if age > locks.DEFAULT_TTL_SECONDS else live).append(entry)
    live.sort(key=lambda e: e["age_seconds"])
    stale.sort(key=lambda e: e["age_seconds"], reverse=True)
    return live, stale


def server_stats() -> dict:
    """Processes, memory and CPU, read straight from /proc.

    No subprocess: this renders on every page load, and `ps`/`free` would fork
    twice for numbers the kernel already exposes as files. Memory and swap are
    here because this box was OOM-killed twice on 2026-08-04 -- swap pressure is
    the early warning that phones are not being reaped.
    """
    stats = {"processes": 0, "cores": os.cpu_count() or 0, "load1": 0.0, "cpu_percent": 0.0,
             "mem_total_mb": 0, "mem_used_mb": 0, "mem_percent": 0.0,
             "swap_total_mb": 0, "swap_used_mb": 0}
    try:
        stats["processes"] = sum(1 for name in os.listdir("/proc") if name.isdigit())
    except OSError:
        pass
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            stats["load1"] = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])      # kB
        total = info.get("MemTotal", 0) / 1024
        available = info.get("MemAvailable", 0) / 1024
        swap_total = info.get("SwapTotal", 0) / 1024
        swap_free = info.get("SwapFree", 0) / 1024
        stats.update(
            mem_total_mb=round(total), mem_used_mb=round(total - available),
            mem_percent=round(100.0 * (total - available) / total, 1) if total else 0.0,
            swap_total_mb=round(swap_total), swap_used_mb=round(swap_total - swap_free))
    except (OSError, ValueError, IndexError):
        pass
    stats["cpu_percent"] = _cpu_percent()
    return stats


def _cpu_percent(interval: float = 0.15) -> float:
    """Busy CPU over a short sample, from /proc/stat.

    Load average alone is a poor answer to "is the CPU busy?" on a box that
    spends its time waiting on phones -- a run can show load 12 while barely
    computing. A short delta is worth the 150 ms.
    """
    def snapshot():
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = [float(x) for x in fh.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
        return sum(parts), idle

    try:
        total_a, idle_a = snapshot()
        time.sleep(interval)
        total_b, idle_b = snapshot()
        total_delta = total_b - total_a
        if total_delta <= 0:
            return 0.0
        return round(100.0 * (1.0 - (idle_b - idle_a) / total_delta), 1)
    except (OSError, ValueError, IndexError):
        return 0.0


def phone_durations(log_path=None, day: str = "") -> dict:
    """How long a phone is actually held, per post.

    The obvious metric -- run wall-clock divided by posts -- is wrong whenever
    posting runs concurrently, and it always does (up to 10 at a time). It
    reports a 21-minute run of 48 posts as "26s per post" when each phone was
    really busy for minutes. Pairing `Launched profile <id>` with its
    `Closed profile <id>` gives the real per-phone figure.

    Launches with no close are excluded rather than guessed at -- that is a
    phone still running, or one leaked before the close guarantee existed -- but
    they are counted so the page can say what the average is based on.
    """
    path = Path(log_path) if log_path else (_repo_root() / LOG_DIR / POSTING_LOG)
    launched: dict = {}
    durations: list = []
    launches = 0

    for candidate in rotated_logs(path):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = _ANSI.sub("", raw)
            match = _PHONE_EVENT.search(line)
            if not match:
                continue
            stamp, event, profile = match.group("ts"), match.group("event"), match.group("id")
            if day and not stamp.startswith(day):
                continue
            try:
                when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if event == "Launched":
                launched[profile] = when
                launches += 1
            elif profile in launched:
                seconds = (when - launched.pop(profile)).total_seconds()
                # A negative or absurd span means the pairing is wrong (a log
                # rotated mid-run, an id reused across days); drop it rather
                # than let one bad pair move the average.
                if 0 < seconds < 3600:
                    durations.append(seconds)

    durations.sort()
    middle = durations[len(durations) // 2] if durations else 0.0
    return {
        "samples": len(durations),
        "launches": launches,
        "unclosed": len(launched),
        "average": (sum(durations) / len(durations)) if durations else 0.0,
        "median": middle,
        "longest": durations[-1] if durations else 0.0,
    }


def disk_usage(paths=None) -> list:
    """Free space where it actually runs out.

    Spoofed variants are the thing that grows without bound -- one encoded video
    per profile per raw clip, 140 of them in a single evening -- and a full disk
    stops the pipeline with an error that looks nothing like "no space".
    """
    import shutil

    paths = paths or ["/", "/opt/adbbot", "/root/adb_bot/logs"]
    out = []
    seen = set()
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
            device = os.stat(path).st_dev   # the real identity of a filesystem
        except OSError:
            continue
        if device in seen:
            continue                        # same filesystem under another path
        seen.add(device)
        out.append({
            "path": path,
            "total_gb": round(usage.total / 1024 ** 3, 1),
            "used_gb": round((usage.total - usage.free) / 1024 ** 3, 1),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "percent": round(100.0 * (usage.total - usage.free) / usage.total, 1) if usage.total else 0.0,
        })
    return out


def uptime_seconds() -> float:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def top_processes(limit: int = 6) -> list:
    """The biggest memory consumers on the box.

    Worth a table rather than a number: on 2026-08-05 a single process reached
    13.8 GB and was OOM-killed, and the totals alone could not say what it was.
    """
    procs = []
    page_size = os.sysconf("SC_PAGE_SIZE")
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/statm", encoding="utf-8") as fh:
                rss_pages = int(fh.read().split()[1])
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                name = fh.read().strip()
        except (OSError, ValueError, IndexError):
            continue                        # exited while we walked /proc
        procs.append({"pid": int(pid), "name": name,
                      "rss_mb": round(rss_pages * page_size / (1024 * 1024), 1)})
    procs.sort(key=lambda proc: proc["rss_mb"], reverse=True)
    return procs[:limit]


def _cmdline(pid) -> list:
    """A process's argv, or [] if it exited while we were reading it."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [part.decode("utf-8", "replace")
                    for part in fh.read().split(b"\0") if part]
    except OSError:
        return []


def _process_role(name: str, argv) -> str:
    """What a process is *for*, in the operator's words.

    "ffmpeg at 480%" is not an answer to "why is the CPU pinned?" -- the answer
    is "it is spoofing Viktoria's clip", and only the command line knows that.
    Anything unrecognised gets no label rather than a guessed one.
    """
    line = " ".join(argv)
    if name.startswith("ffmpeg"):
        for flag, value in zip(argv, argv[1:]):
            if flag == "-i":
                return f"spoofing {_raw_clip_label(value)}"
        return "encoding"
    if "video_testing_framework.cli" in line:
        return "spoofer (drives ffmpeg)"
    if "phone_launcher" in name or "phone_launcher" in line:
        # The launcher is named after the profile it is showing: `-n <Name>`.
        for flag, value in zip(argv, argv[1:]):
            if flag == "-n":
                return f"phone: {value}"
        return "phone"
    if name.startswith("WebKit"):
        return "phone screen (WebKit)"
    if "adb_bot.automation.run_loop" in argv:
        # Match the argv element, not the joined string: `python -c "from
        # adb_bot.automation.run_loop import ..."` is not a loop, and reading
        # the word after the match would label it "loop: import".
        after = argv[argv.index("adb_bot.automation.run_loop") + 1:]
        return f"loop: {after[0]}" if after else "loop"
    if "adb_bot.automation.site" in argv:
        return "this dashboard"
    if name == "adb":
        return "adb server"
    return ""


def _raw_clip_label(path: str) -> str:
    """`/tmp/adbbot_raw/Viktoria/viktoria 3 I 6 aug.mp4` -> `Viktoria / viktoria 3 I 6 aug`."""
    clip = Path(path)
    stem = re.sub(r"\.(mp4|mov|m4v|webm|mkv)$", "", clip.name, flags=re.I)
    folder = clip.parent.name
    return f"{folder} / {stem}" if folder else stem


def cpu_processes(limit: int = 6, interval: float = 0.15) -> list:
    """The biggest CPU consumers, sampled the way `top` does it.

    `top_processes` answers "what will get us OOM-killed"; this answers the
    other question a pinned box provokes, and they are rarely the same process.
    Percentages are per-core like `top`'s, so one saturated core reads 100% and
    an encode spread over eight can read 500% -- capping it at 100 would hide
    exactly the case worth seeing.

    Two passes over /proc/<pid>/stat with a short sleep between: a process's
    *total* CPU time since it started says nothing about now (adb has burned
    minutes over five days while doing nothing today), only the delta does.
    """
    def sample() -> tuple:
        with open("/proc/stat", encoding="utf-8") as fh:
            total = sum(float(x) for x in fh.readline().split()[1:])
        ticks: dict = {}
        try:
            pids = [name for name in os.listdir("/proc") if name.isdigit()]
        except OSError:
            return total, ticks
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    # The comm field is parenthesised and may itself contain
                    # spaces, so split after the last ")" -- state is then [0]
                    # and utime/stime are [11] and [12].
                    fields = fh.read().rsplit(")", 1)[1].split()
                ticks[pid] = float(fields[11]) + float(fields[12])
            except (OSError, ValueError, IndexError):
                continue                    # exited while we walked /proc
        return total, ticks

    cores = os.cpu_count() or 1
    try:
        total_a, before = sample()
        time.sleep(interval)
        total_b, after = sample()
    except OSError:
        return []
    elapsed = total_b - total_a
    if elapsed <= 0:
        return []

    busy = []
    for pid, ticks in after.items():
        used = ticks - before.get(pid, ticks)     # a pid unseen before is new: 0
        if used > 0:
            busy.append((100.0 * cores * used / elapsed, pid))
    busy.sort(reverse=True)

    out = []
    for percent, pid in busy[:limit]:
        # Only the survivors are worth a cmdline read; there are hundreds of pids.
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                name = fh.read().strip()
        except OSError:
            continue
        out.append({"pid": int(pid), "name": name, "cpu_percent": round(percent, 1),
                    "role": _process_role(name, _cmdline(pid))})
    return out


def phone_processes() -> list:
    """Live phones with the profile each belongs to, newest first."""
    from adb_bot.automation import phone_reaper

    try:
        phones = phone_reaper.list_phones()
        orphan_pids = {p.pid for p in phone_reaper.find_orphans(phones)}
    except Exception:
        return []
    rows = [{"pid": p.pid, "name": p.name or "(unknown)", "profile_id": p.profile_id,
             "age_seconds": p.age_seconds, "rss_mb": p.rss_mb,
             "orphan": p.pid in orphan_pids} for p in phones]
    rows.sort(key=lambda r: r["age_seconds"])
    return rows


def timer_states(loops=None) -> list:
    """Each loop's timer: whether it is active, when it last fired, when next.

    The production watchdog cannot see a loop whose timer is stopped -- no tick,
    no observation, no alert. This table is what makes that visible.

    Read from `systemctl list-timers` rather than `show`: these are monotonic
    (`OnUnitActiveSec`) timers, so `NextElapseUSecRealtime` is empty for them and
    only list-timers resolves the next wall-clock time.
    """
    loops = list(loops or schedule_spec.RECOMMENDED_LOOPS)
    rows = {loop: {"loop": loop, "state": "not installed", "last": "", "next": "",
                   "interval_min": schedule_spec.RECOMMENDED_INTERVALS.get(loop, 0),
                   "stopped": True} for loop in loops}
    try:
        out = subprocess.run(
            ["systemctl", "list-timers", "adbbot-*", "--all", "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return list(rows.values())

    for line in out.splitlines():
        if ".timer" not in line:
            continue
        parts = line.split()
        unit = next((p for p in parts if p.endswith(".timer")), "")
        loop = unit[len(schedule_spec.UNIT_PREFIX):-len(".timer")] if unit else ""
        if loop not in rows:
            continue
        index = parts.index(unit)
        # Layout: NEXT... LEFT... LAST... PASSED... UNIT ACTIVATES.
        head = parts[:index]
        rows[loop].update(
            next=" ".join(head[1:3]) if head and head[0] != "-" and len(head) > 3 else "",
            last=_last_from_timer_line(head),
        )

    # State comes from the unit itself, never from an empty NEXT column: systemd
    # prints "-" for NEXT while the timer's *service* is running, so reading the
    # column would report every actively-working loop as stopped.
    for loop, state in _unit_active_states(
            [schedule_spec.unit_name(loop, "timer") for loop in loops], loops):
        if loop in rows:
            rows[loop].update(state=state, stopped=state != "active")

    now = datetime.now()
    for row in rows.values():
        # "15:09:42" answers "when" only if you also know what time it is now.
        row["seconds_until"] = _seconds_until(row["next"], now)
        # A daily timer's cadence is the useless half of what it does: "daily"
        # without the hour is the one row on this table nobody can act on.
        row["at"] = (schedule_spec.DEFAULT_DAILY_START.get(row["loop"], "")
                     if row["interval_min"] >= 1440 else "")
    return list(rows.values())


def _seconds_until(stamp: str, now) -> float:
    """Seconds from `now` to a "YYYY-MM-DD HH:MM:SS" timestamp; 0 when there is
    none, or when it has already passed (the timer is firing, not overdue)."""
    try:
        return max(0.0, (datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S") - now).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _unit_active_states(units, loops) -> list:
    try:
        out = subprocess.run(["systemctl", "show", "--property=Id",
                              "--property=ActiveState", *units],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return [(loop, "unknown") for loop in loops]
    blocks, current = {}, {}
    for line in out.splitlines():
        if not line.strip():
            if current.get("Id"):
                blocks[current["Id"]] = current.get("ActiveState", "unknown")
            current = {}
            continue
        key, _, value = line.partition("=")
        current[key] = value
    if current.get("Id"):
        blocks[current["Id"]] = current.get("ActiveState", "unknown")
    return [(loop, blocks.get(schedule_spec.unit_name(loop, "timer"), "not installed"))
            for loop in loops]


def _last_from_timer_line(head) -> str:
    """The LAST column, which follows NEXT/LEFT and is itself a date or '-'."""
    for index, token in enumerate(head):
        if re.fullmatch(r"\d{4}-\d\d-\d\d", token) and index + 1 < len(head):
            # First date is NEXT, second is LAST; return whichever is last seen.
            pass
    dates = [i for i, token in enumerate(head) if re.fullmatch(r"\d{4}-\d\d-\d\d", token)]
    if not dates:
        return ""
    index = dates[-1]
    return " ".join(head[index:index + 2])


def _clean_timestamp(value: str) -> str:
    """systemd prints 'Wed 2026-08-05 19:22:23 UTC' or 'n/a'."""
    value = (value or "").strip()
    if not value or value == "n/a":
        return ""
    parts = value.split()
    return " ".join(parts[1:3]) if len(parts) >= 3 else value


def running_now() -> dict:
    """Loops active, profiles locked, slots held, phones open."""
    from adb_bot.core import locks

    # `live_profile_count`, NOT `held_slots`: the latter returns the slots *this
    # process* holds, and the report server holds none, so it always read 0 and
    # made every healthy run look like a fleet-wide phone leak (2026-08-05).
    # The global count is what the ceiling is actually enforced against.
    try:
        slots_in_use = locks.live_profile_count()
    except Exception:
        slots_in_use = 0
    try:
        ceiling = locks.max_live_profiles()
    except Exception:
        ceiling = 0
    held, stale = profile_locks()

    states = systemd_state()
    return {
        "loops": states,
        "active_loops": [name for name, state in states if state in ("active", "activating")],
        "profiles": held,
        "stale_locks": stale,
        "slots_held": slots_in_use,
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
class ProfileRun:
    """What one profile did in one run.

    `outcome` is what the run itself achieved; `next_step` is what the *retry
    pass* decided about it afterwards. Keeping them apart is the point -- a
    failure that is already re-queued needs nobody, and a failure that is not
    needs somebody, and on the old report both looked identical.
    """

    launch_id: str = ""
    name: str = ""
    outcome: str = OUTCOME_UNKNOWN
    detail: str = ""
    next_step: str = ""
    next_tone: str = ""             # "", "ok", "warn", "bad" -- for the pill
    media: str = ""                 # the variant this profile was given
    at: str = ""                    # when the attempt happened, if known

    @property
    def label(self) -> str:
        """Name if we know it, the MLX id if we do not."""
        return self.name or self.launch_id


def outcome_counts(profiles) -> dict:
    """How many of `profiles` ended in each outcome, every key present."""
    tally = Counter(p.outcome for p in profiles)
    return {name: tally.get(name, 0) for name in OUTCOME_ORDER}


def sort_profiles(profiles) -> list:
    """Worst outcome first, then alphabetical -- problems at the top."""
    rank = {name: index for index, name in enumerate(OUTCOME_ORDER)}
    return sorted(profiles, key=lambda p: (rank.get(p.outcome, 99), p.label.lower()))


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
    profiles: list = field(default_factory=list)

    @property
    def seconds_per_post(self) -> float:
        return (self.seconds / self.posts) if self.posts else 0.0

    def profile(self, launch_id: str) -> ProfileRun:
        """This run's record for `launch_id`, created on first sight."""
        for entry in self.profiles:
            if entry.launch_id == launch_id:
                return entry
        entry = ProfileRun(launch_id=launch_id)
        self.profiles.append(entry)
        return entry

    def counts(self) -> dict:
        return outcome_counts(self.profiles)

    def sorted_profiles(self) -> list:
        return sort_profiles(self.profiles)


@dataclass
class VideoRun:
    """One source video, and every profile it was spoofed for.

    This is the run a person means: `Nikki / run4` is one clip, encoded once
    per Nikki profile, and the question about it is always the same -- who got
    it, who did not, and is anyone still trying.
    """

    model: str = ""
    run: str = ""                   # the folder name, e.g. "run4"
    number: int = 0                 # the folder's number, which counts from the
                                    # day the model was set up and never resets
    day_number: int = 0             # this clip's place in *today*, counting from 1
    source: str = ""                # the raw clip's name, without the handle
    built: str = ""                 # when the variants were written
    profiles: list = field(default_factory=list)

    @property
    def title(self) -> str:
        """`Nikki · run 2` -- the day's second Nikki clip.

        A page about one day counts from one. The folder number keeps rising
        forever (today's first Nikki clip lives in `run4`), so showing it here
        would make a Monday morning start at run 47.
        """
        if not self.model:
            return self.run
        return f"{self.model} · run {self.day_number or self.number}"

    def counts(self) -> dict:
        return outcome_counts(self.profiles)

    def sorted_profiles(self) -> list:
        return sort_profiles(self.profiles)


def name_from_media_path(path: str) -> str:
    """`.../nikki_1_I_5_aug__Nikki_15.mp4` -> `Nikki 15`.

    `spoof_pipeline.finalize_variant` stamps the profile handle onto every
    variant filename after a double underscore, which is why a run's media-push
    line can name a profile the posting log otherwise only knows by MLX id.
    Anything that does not have that shape returns "" rather than a guess.
    """
    stem = path.rsplit("/", 1)[-1]
    if "__" not in stem:
        return ""
    tail = stem.rsplit("__", 1)[-1]
    tail = re.sub(r"\.(mp4|mov|m4v|webm|mkv)$", "", tail, flags=re.I)
    return tail.replace("_", " ").strip()


def profile_names(ledger=None, log_dir=None) -> dict:
    """MLX launch id -> profile name, built from what is already on disk.

    Airtable holds the authoritative map, but the report must stay readable
    when Airtable is unreachable -- and the failures worth reading are exactly
    the ones that happen when something else is unreachable too. Both sources
    here are local: the post ledger (media path per share) and the recheck log
    (the one line that prints a name and an id together).
    """
    names: dict = {}

    try:
        from adb_bot.automation.post_ledger import PostLedger
        records = (ledger or PostLedger()).load()
    except Exception:
        records = {}
    for record in (records or {}).values():
        name = name_from_media_path(getattr(record, "media_path", "") or "")
        if name and getattr(record, "profile_id", ""):
            names.setdefault(str(record.profile_id), name)

    base = Path(log_dir) if log_dir else (_repo_root() / LOG_DIR)
    try:
        text = (base / RECHECK_LOG).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for match in _RECHECK_NAME.finditer(text):
        names.setdefault(match.group("id"), match.group("name").strip())
    return names


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


def _trim(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# A `Post result` status, as the page should say it.
_RESULT_OUTCOMES = {
    "done": (OUTCOME_POSTED, ""),
    "uncertain": (OUTCOME_VERIFYING, "share tapped, not yet confirmed"),
    "already_shared": (OUTCOME_SKIPPED, "clip had already been sent to this profile"),
    "human_verification": (OUTCOME_FAILED, "Instagram asked for human verification"),
    "banned": (OUTCOME_FAILED, "account banned or suspended"),
    "action_block": (OUTCOME_FAILED, "action blocked (temporary)"),
    "adb_connect_failed": (OUTCOME_FAILED, "ADB connect failed"),
    "heartbeat_lost": (OUTCOME_FAILED, "phone lost mid-post (heartbeat)"),
    "failed": (OUTCOME_FAILED, "the flow reported a failure"),
}


def _note_profile_event(run: RunSummary, line: str) -> None:
    """Fold one log line into the run's per-profile record.

    Last event wins, because that is how the log reads: a launch that 500s and
    then succeeds on the relaunch ends the run posted, and saying otherwise
    would turn MultiLogin's flakiness into a fake failure. The name, once
    learned, is never unlearned.
    """
    result = _POST_RESULT.search(line)
    if result:
        entry = run.profile(result.group("id"))
        entry.name = entry.name or _trim(result.group("name"), 60)
        outcome, note = _RESULT_OUTCOMES.get(result.group("status"),
                                             (OUTCOME_FAILED, result.group("status")))
        entry.outcome = outcome
        entry.detail = _trim(result.group("detail") or note)
        return

    media = _MEDIA_PUSH.search(line)
    if media:
        entry = run.profile(media.group("id"))
        entry.name = entry.name or name_from_media_path(media.group("path"))
        # Which clip this profile was given -- the join that lets the report be
        # read the other way round, one video at a time.
        entry.media = media.group("path")
        return

    for pattern, outcome, note in (
        (_WF_DONE, OUTCOME_POSTED, ""),
        (_WF_UNCERTAIN, OUTCOME_VERIFYING, "share tapped, not yet confirmed"),
        (_WF_HEARTBEAT, OUTCOME_FAILED, "phone lost mid-post (heartbeat)"),
        (_WF_ADB, OUTCOME_FAILED, "ADB connect failed"),
        (_WF_NOT_READY, OUTCOME_FAILED, "phone never became ADB-ready"),
        (_WF_ALREADY_SHARED, OUTCOME_SKIPPED, "clip had already been sent to this profile"),
        (_LAUNCH_500, OUTCOME_FAILED, "MultiLogin-side 500 on launch"),
        (_CEILING_SKIP, OUTCOME_SKIPPED, "deferred to the next run by the phone ceiling"),
    ):
        match = pattern.search(line)
        if match:
            entry = run.profile(match.group("id"))
            entry.outcome = outcome
            entry.detail = note
            return

    flagged = _WF_FLAGGED.search(line)
    if flagged:
        entry = run.profile(flagged.group("id"))
        entry.outcome = OUTCOME_FAILED
        entry.detail = f"Instagram flagged the account ({flagged.group('kind')})"
        return

    failed = _WF_FAILED.search(line)
    if failed:
        entry = run.profile(failed.group("id"))
        entry.outcome = OUTCOME_FAILED
        entry.detail = _trim(failed.group("why") or "the flow reported a failure")
        return

    launch_failed = _LAUNCH_FAILED.search(line)
    if launch_failed:
        entry = run.profile(launch_failed.group("id"))
        entry.outcome = OUTCOME_FAILED
        entry.detail = _trim(f"launch failed: {launch_failed.group('why')}")
        return

    launched = _LAUNCHED.search(line)
    if launched:
        # Seen but not yet finished. If nothing else ever arrives for this id
        # the run ends with it `unknown`, which is exactly the right answer for
        # a post that was still going when the log ran out.
        run.profile(launched.group("id"))


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
        if open_run is not None:
            _note_profile_event(open_run, line)
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


def retry_events(log_path=None, day: str = "") -> list:
    """What the retry pass decided about each failed row, oldest first.

    Keyed by profile name rather than MLX id because that is all the retry log
    knows -- its rows are queue rows ("Nikki 15 / 20:00"), and the profile name
    is the part before the slot.
    """
    path = Path(log_path) if log_path else (_repo_root() / LOG_DIR / RETRY_LOG)
    events: list = []
    for candidate in rotated_logs(path):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = _ANSI.sub("", raw)
            requeue = _RETRY_REQUEUE.search(line)
            if requeue:
                events.append({
                    "ts": requeue.group("ts"),
                    "profile": requeue.group("name").split(" / ")[0].strip().lower(),
                    "next": (f"retrying by itself — attempt {requeue.group('attempt')}"
                             f"/{requeue.group('max')}, next try in "
                             f"{float(requeue.group('mins')):.0f} min"),
                    "tone": "ok",
                })
                continue
            human = _RETRY_HUMAN.search(line)
            if human:
                events.append({
                    "ts": human.group("ts"),
                    "profile": human.group("name").split(" / ")[0].strip().lower(),
                    "next": f"needs a person — {_trim(human.group('why'), 80)}",
                    "tone": "bad",
                })
                continue
            parked = _RETRY_PARKED.search(line)
            if parked:
                events.append({
                    "ts": parked.group("ts"),
                    "profile": parked.group("name").split(" / ")[0].strip().lower(),
                    "next": f"not retried — {_trim(parked.group('why'), 80)}",
                    "tone": "warn",
                })
    if day:
        events = [e for e in events if e["ts"].startswith(day)]
    events.sort(key=lambda e: e["ts"])
    return events


def _apply_retry_verdict(entry, floor: str, events) -> None:
    """Say what the retry pass decided about this failure, after `floor`.

    Matched on profile name because that is all the retry log knows, and on
    "the first decision after the attempt" because a profile that failed twice
    today gets two verdicts and each belongs to its own attempt.
    """
    key = (entry.name or "").strip().lower()
    decision = None
    if key:
        decision = next((e for e in events
                         if e["profile"] == key and e["ts"] >= floor), None)
    if decision:
        entry.next_step, entry.next_tone = decision["next"], decision["tone"]
    else:
        entry.next_step = "waiting for the next retry pass"
        entry.next_tone = "warn"


def annotate_runs(runs, names=None, events=None) -> list:
    """Fill in profile names, and what happened to each failure afterwards.

    The retry decision is looked up by "the first thing the retry pass said
    about this profile *after* the run ended" -- which is what makes the page
    able to say `retrying, attempt 2/3` instead of leaving a red row that
    somebody then investigates for nothing.
    """
    names = names or {}
    events = events or []
    for run in runs:
        floor = run.finished or run.started
        for entry in run.profiles:
            if not entry.name:
                entry.name = names.get(entry.launch_id, "")
            if entry.outcome == OUTCOME_POSTED:
                continue
            if entry.outcome == OUTCOME_VERIFYING:
                entry.next_step = "the recheck pass will confirm or fail it"
                entry.next_tone = "warn"
                continue
            if entry.outcome != OUTCOME_FAILED:
                continue
            _apply_retry_verdict(entry, floor, events)
    return runs


def spoof_root(spoof_dir=None) -> Path:
    """Where the pipeline writes `<model>/run<N>/` folders."""
    if spoof_dir:
        return Path(spoof_dir)
    from adb_bot.config import settings

    return Path(settings.get_saved_spoofed_videos_dir() or "")


def scan_video_runs(spoof_dir=None) -> list:
    """Every `<model>/run<N>` folder on disk, with one entry per variant in it.

    The folder is the only place that knows who a clip was *meant* for. A
    profile that never got a queue row is invisible everywhere else, and it is
    exactly the profile a person is looking for when they ask why an account
    did not post today.
    """
    root = spoof_root(spoof_dir)
    out: list = []
    try:
        models = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out

    for model_dir in models:
        try:
            run_dirs = sorted(p for p in model_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for run_dir in run_dirs:
            match = _SPOOF_RUN_DIR.match(run_dir.name)
            if not match:
                continue
            try:
                files = sorted(p for p in run_dir.iterdir()
                               if p.suffix.lower() in _VIDEO_EXTS)
            except OSError:
                continue
            if not files:
                continue
            newest = max(p.stat().st_mtime for p in files)
            video = VideoRun(
                model=model_dir.name,
                run=run_dir.name,
                number=int(match.group(1)),
                # Some raw clips carry ".mp4" inside their own name, so the part
                # before the handle can end in it twice; show it once.
                source=re.sub(r"\.(mp4|mov|m4v|webm|mkv)$", "",
                              files[0].name.rsplit("__", 1)[0], flags=re.I),
                built=datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S"),
            )
            for path in files:
                video.profiles.append(ProfileRun(name=name_from_media_path(str(path)),
                                                 outcome=OUTCOME_PENDING,
                                                 media=str(path)))
            out.append(video)
    return out


def spoof_now(spoof_dir=None) -> dict:
    """The encode happening right now, if there is one.

    Read from the process table rather than a log, because the pipeline says
    nothing between "<clip> -> run4" and the summary twenty minutes later: for
    the whole span in between, the log of a healthy run and a stuck one are the
    same text. The command line always knows which clip is on the encoder.

    `done` is counted from the run folder, where a finished variant has the
    handle stamped into its name -- so a clip half way through its profiles
    reads as "4 of 8" instead of "running".
    """
    from adb_bot.automation import phone_reaper
    from adb_bot.automation.spoof_pipeline import resolve_model

    state = {"running": False, "encoding": False, "pid": 0, "clip": "", "model": "",
             "run": "", "seconds": 0.0, "done": []}
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return state

    raw_path = dest = ""
    for pid in pids:
        argv = _cmdline(pid)
        if not argv:
            continue
        line = " ".join(argv)
        if "video_testing_framework.cli" in line and "run" in argv:
            state["running"] = True
            state["pid"] = int(pid)
            # `vtf run <raw> --dest <dir>`: the first non-flag after "run".
            rest = argv[argv.index("run") + 1:]
            raw_path = next((a for a in rest if not a.startswith("-")), "")
            for flag, value in zip(argv, argv[1:]):
                if flag == "--dest":
                    dest = value
            state["seconds"] = phone_reaper._process_age(int(pid))
        elif os.path.basename(argv[0]).startswith("ffmpeg"):
            state["encoding"] = True
            if not raw_path:
                for flag, value in zip(argv, argv[1:]):
                    if flag == "-i":
                        raw_path = value

    if not (state["running"] or state["encoding"]):
        return state

    if raw_path:
        folder, _, clip = _raw_clip_label(raw_path).partition(" / ")
        # The raw folder is a label, not always the model -- see the alias map.
        state["model"], state["clip"] = resolve_model(folder), clip
    if dest:
        run_dir = Path(dest)
        state["run"] = run_dir.name
        try:
            state["done"] = sorted(
                filter(None, (name_from_media_path(str(p)) for p in run_dir.iterdir()
                              if p.suffix.lower() in _VIDEO_EXTS)))
        except OSError:
            pass
    return state


def _slow(key: str, build):
    """Memoise one slow read for `SLOW_CACHE_SECONDS`.

    `build` must report its own failures in the value it returns rather than
    raising, so a bad answer is cached too -- an outage retried on every render
    is a slow page for the length of the outage and no new information.
    """
    entry = _slow_cache.get(key)
    if entry and (time.time() - entry[0]) < SLOW_CACHE_SECONDS:
        return entry[1]
    data = build()
    _slow_cache[key] = (time.time(), data)
    return data


def spoof_queue(airtable, spoof_dir=None) -> dict:
    """Raw clips waiting to be spoofed, and who each one is waiting for.

    The pipeline's own definition, read the same way it reads it: a clip is
    pending when the raw source still has it and no Content Pipeline row names
    it. Which means a clip already on the encoder counts as *done* here -- the
    row is written before the first variant is -- and `spoof_now` is what covers
    the gap.

    One clip is not one unit of work: it is one encode per active profile under
    its model, serially, minutes each. So the count that matters for "when will
    this be finished" is the variant count, not the clip count.
    """
    if airtable is None:
        return {"models": [], "clips": 0, "variants": 0, "unroutable": [],
                "error": "no Airtable client"}
    return _slow("spoof_queue", lambda: _spoof_queue(airtable))


def _spoof_queue(airtable) -> dict:
    out = {"models": [], "clips": 0, "variants": 0, "unroutable": [], "error": ""}
    try:
        from adb_bot.automation import spoof_pipeline
        from adb_bot.config import settings

        # Airtable first, deliberately. The raw source is Google Drive, and
        # listing it is the expensive half; there is no point paying for it to
        # then discover the client that says what has already been done is
        # unusable. It also keeps a caller with no real Airtable -- a test, a
        # dry run -- from reaching the network at all.
        processed = airtable.content_pipeline_names()
        # The live loop takes its targets from the profile inventory
        # (`--targets profiles`), so a profile parked by Status disappears from
        # this count the moment it is parked -- which is the point.
        targets = airtable.profile_targets_by_model()
        source = spoof_pipeline.build_source(
            settings.get_saved_raw_videos_dir(),
            settings.get_saved_drive_folder_id(),
            settings.get_saved_google_service_account_json())
        if source is None:
            out["error"] = "no raw source configured"
            return out
        by_folder = source.list_by_model()
    except Exception as exc:
        # Returned, not raised, so `_slow` caches it: a Drive outage retried
        # every 20 seconds is a slow page for as long as the outage lasts, and
        # no new information.
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    for folder, videos in sorted(by_folder.items()):
        model = spoof_pipeline.resolve_model(folder)
        waiting = sorted(v.name for v in videos if v.name not in processed)
        handles = [t["handle"] for t in targets.get(model.lower(), [])]
        if not waiting:
            continue
        if not handles:
            # Nothing will ever pick these up: no active profile to spoof for.
            # Silent in the pipeline's own output, which only logs a skip line.
            out["unroutable"].append({"folder": folder, "model": model,
                                      "clips": len(waiting)})
            continue
        out["models"].append({
            "folder": folder, "model": model, "clips": waiting,
            "handles": handles, "variants": len(waiting) * len(handles),
        })
        out["clips"] += len(waiting)
        out["variants"] += len(waiting) * len(handles)
    out["models"].sort(key=lambda m: m["variants"], reverse=True)
    return out


def _ledger_by_path(ledger=None) -> dict:
    """Variant path -> the most recent share record for it."""
    from adb_bot.automation.post_ledger import PostLedger

    try:
        records = (ledger or PostLedger()).load()
    except Exception:
        return {}
    out: dict = {}
    for record in (records or {}).values():
        path = getattr(record, "media_path", "") or ""
        if not path:
            continue
        known = out.get(path)
        if known is None or (getattr(record, "shared_at", 0.0) or 0.0) > (
                getattr(known, "shared_at", 0.0) or 0.0):
            out[path] = record
    return out


def _queue_by_path(queue_rows=None, variants=None) -> dict:
    """Variant path -> the Posting Queue row that claimed it."""
    out: dict = {}
    for row in (queue_rows or []):
        fields = row.get("fields", {}) or {}
        linked = fields.get("Spoof Variant") or []
        if not linked:
            continue
        variant = (variants or {}).get(linked[0]) or {}
        path = variant.get("file_path")
        if path:
            out[path] = {"status": fields.get("Post Status") or "",
                         "name": fields.get("Name") or ""}
    return out


def video_runs(spoof_dir=None, day: str = "", ledger=None, tick_runs=None,
               events=None, queue_rows=None, variants=None) -> list:
    """Today's clips, each with what happened to every profile it was made for.

    Four sources, in order of how much they actually prove: the post ledger
    (we tapped Share, and whether it was later confirmed), the posting log (why
    an attempt died before that), the queue row (it is claimed but untried),
    and the folder itself (nobody has touched this one).
    """
    videos = scan_video_runs(spoof_dir)
    if not videos:
        return []

    by_ledger = _ledger_by_path(ledger)
    by_log: dict = {}
    for run in (tick_runs or []):
        for entry in run.profiles:
            if entry.media:
                by_log[entry.media] = (entry, run.finished or run.started)
    by_queue = _queue_by_path(queue_rows, variants)

    for video in videos:
        for entry in video.profiles:
            record = by_ledger.get(entry.media)
            if record is not None:
                shared = getattr(record, "shared_at", 0.0) or 0.0
                entry.at = (datetime.fromtimestamp(shared).strftime("%Y-%m-%d %H:%M:%S")
                            if shared else "")
                status = getattr(record, "status", "")
                if status == "confirmed":
                    entry.outcome, entry.detail = OUTCOME_POSTED, ""
                elif status == "disproved":
                    entry.outcome = OUTCOME_FAILED
                    entry.detail = _trim(getattr(record, "detail", "")
                                         or "the recheck could not find the post")
                else:
                    entry.outcome = OUTCOME_VERIFYING
                    entry.detail = "share tapped, not yet confirmed"
                continue

            logged = by_log.get(entry.media)
            if logged is not None:
                attempt, when = logged
                entry.outcome = attempt.outcome
                entry.detail = attempt.detail
                entry.launch_id = attempt.launch_id
                entry.at = when
                continue

            claimed = by_queue.get(entry.media)
            if claimed is not None:
                status = (claimed.get("status") or "").strip()
                if status == "Posted":
                    entry.outcome, entry.detail = OUTCOME_POSTED, ""
                elif status == "Verifying":
                    entry.outcome = OUTCOME_VERIFYING
                    entry.detail = "share tapped, not yet confirmed"
                elif status == "Failed":
                    entry.outcome = OUTCOME_FAILED
                    entry.detail = "the attempt never reached the phone"
                else:
                    entry.outcome = OUTCOME_PENDING
                    entry.detail = f"waiting in the queue ({claimed.get('name') or status})"
                continue

            entry.outcome = OUTCOME_PENDING
            entry.detail = "no post scheduled for it yet"

    if day:
        # The day's clips, by the day they were *made*. An older clip that
        # happened to go out this morning belongs to the run it came from, and
        # letting those in turned a daily page into a growing pile -- yesterday's
        # runs reappear every time one of their leftovers is retried.
        todays = [v for v in videos if v.built.startswith(day)]
        if not todays and videos:
            # The day turns over at midnight; the pipeline builds in the
            # afternoon. Between those, "today" is honestly empty -- and a
            # blank section at 9am reads as a broken machine rather than an
            # early one. Show the last day that did build clips instead; the
            # page says which day it is showing.
            last = max(v.built[:10] for v in videos)
            todays = [v for v in videos if v.built.startswith(last)]
        videos = todays

    for video in videos:
        for entry in video.profiles:
            if entry.outcome == OUTCOME_FAILED:
                _apply_retry_verdict(entry, entry.at or video.built, events or [])
            elif entry.outcome == OUTCOME_VERIFYING:
                entry.next_step = "the recheck pass will confirm or fail it"
                entry.next_tone = "warn"

    videos.sort(key=lambda v: (v.model.lower(), v.built, v.number))
    # Number each model's clips within the listing, in the order they were made.
    seen: Counter = Counter()
    for video in videos:
        seen[video.model] += 1
        video.day_number = seen[video.model]
    return videos


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

def queue_today(airtable, day: str, rows=None) -> dict:
    """Posting Queue rows scheduled today, grouped, plus the failure detail."""
    rows = rows if rows is not None else airtable.list_queue_rows()
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


def claimed_variant_ids(queue_rows) -> set:
    """Variant ids already attached to a Posting Queue row, in any status.

    `queue_runner` excludes a variant from a fresh slot if *any* row links it --
    Posted and Failed included -- so these cannot serve another slot no matter
    what their own Status says.
    """
    from adb_bot.clients import airtable as at

    claimed = set()
    for row in queue_rows or []:
        for variant_id in ((row.get("fields") or {}).get(at.F_PQ_SPOOF_VARIANT) or []):
            claimed.add(variant_id)
    return claimed


# The only Issue Type the retry pass re-queues. Everything else is a statement
# about the account -- a ban, a verification prompt, or a human parking the row
# -- and posting into it again does not fix it. See retry_runner.
RETRYABLE_ISSUE = "Failed - Needs Retry"
DEFAULT_MAX_RETRIES = 3


def needs_human(airtable, max_retries: int = DEFAULT_MAX_RETRIES) -> dict:
    """Everything waiting on a person, from both places it can be recorded.

    Two separate worklists, deliberately kept apart:

    - **Queue rows** the retry pass will never re-queue. Read via
      `list_failed_posts`, which is what the retry pass itself reads --
      `list_queue_rows` does not return Issue Type or Retry Count at all, and
      building this from it silently produced a table of blanks (2026-08-05).
    - **Profiles** carrying `Needs Human Check`. The retry pass sets it and
      never clears it; clearing the checkbox is how a person signals they
      looked.

    Not filtered to today. A row that failed last night still needs the same
    person to do the same thing, and dropping it at midnight is how it gets
    forgotten.
    """
    from adb_bot.clients import airtable as at

    out = {"rows": [], "retrying": [], "profiles": [], "error": ""}
    field = lambda record, key: (record.get("fields") or {}).get(key)   # noqa: E731

    try:
        failed = airtable.list_failed_posts()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    for record in failed:
        issue = str(field(record, at.F_PQ_ISSUE_TYPE) or "").strip()
        try:
            retries = int(field(record, at.F_PQ_RETRY_COUNT) or 0)
        except (TypeError, ValueError):
            retries = 0
        entry = {
            "name": field(record, at.F_PQ_NAME) or "(unnamed)",
            "slot": str(field(record, at.F_PQ_SCHEDULED) or "")[:16].replace("T", " "),
            "issue": issue or "(none set)",
            "retries": retries,
            "notes": str(field(record, at.F_PQ_NOTES) or "")[:200],
        }
        if issue == RETRYABLE_ISSUE and retries < max_retries:
            out["retrying"].append(entry)
        else:
            out["rows"].append(entry)

    # Worst first: a banned account is not the same errand as an exhausted one.
    order = {"Banned / Blocked": 0, "Human Verification Required": 1,
             "Retries Exhausted": 2}
    out["rows"].sort(key=lambda e: (order.get(e["issue"], 9), e["name"]))
    out["retrying"].sort(key=lambda e: e["name"])

    try:
        profiles = airtable._list_table(
            at.TABLE_PROFILES,
            fields=[at.F_PROF_NAME, at.F_PROF_NEEDS_HUMAN, at.F_PROF_ISSUE_REASON,
                    at.F_PROF_ISSUE_NOTES, at.F_PROF_FLAGGED_AT, at.F_PROF_STATUS],
            filter_formula=f"{{{at.F_PROF_NEEDS_HUMAN}}}=1",
        )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    for record in profiles:
        out["profiles"].append({
            "name": field(record, at.F_PROF_NAME) or "(unnamed)",
            "reason": field(record, at.F_PROF_ISSUE_REASON) or "(none set)",
            "status": field(record, at.F_PROF_STATUS) or "Active",
            "flagged_at": str(field(record, at.F_PROF_FLAGGED_AT) or "")[:16].replace("T", " "),
            # Newest entry first in the field itself, so the first line is the
            # current story and the rest is history nobody needs on a dashboard.
            "note": str(field(record, at.F_PROF_ISSUE_NOTES) or "").splitlines()[:1],
        })
    out["profiles"].sort(key=lambda e: (e["reason"], e["name"]))
    return out


def content_stock(airtable, claimed=None) -> dict:
    """Postable content: Ready variants, split by whether a queue row holds them.

    The distinction is the point. A variant only becomes `Used` on a successful
    post, so a Failed row leaves its variant `Ready` *and* linked -- permanently
    unusable but still counted. On 2026-08-05 Viktoria showed 11 Ready variants
    and could post none of them: all 11 were held by Failed or Verifying rows
    from the day before. Reporting the raw Ready count invites exactly that
    misreading, so `drawable` is what the page leads with.
    """
    try:
        variants = airtable.list_ready_variants()
    except Exception:
        return {"ready": 0, "drawable": 0, "held": 0, "by_model": {}, "held_by_model": {}}
    claimed = claimed if claimed is not None else set()

    # `list_ready_variants` returns its own flattened dicts, not raw Airtable
    # records -- the model comes out of the spoofed file's path.
    # Folder case is not consistent on disk ("Jasmin" and "jasmin" both exist),
    # and two spellings of one model reads as two models with half the stock
    # each -- exactly the wrong thing on a page you check before a run.
    drawable_by_model, held_by_model = Counter(), Counter()
    for variant in variants:
        path = str(variant.get("file_path") or "")
        match = re.search(r"/spoofed/([^/]+)/", path)
        model = match.group(1).capitalize() if match else "unknown"
        if variant.get("id") in claimed:
            held_by_model[model] += 1
        else:
            drawable_by_model[model] += 1

    models = sorted(set(drawable_by_model) | set(held_by_model))
    return {
        "ready": len(variants),
        "drawable": sum(drawable_by_model.values()),
        "held": sum(held_by_model.values()),
        "by_model": {m: drawable_by_model.get(m, 0) for m in models},
        "held_by_model": {m: held_by_model.get(m, 0) for m in models},
    }


def model_schedules(airtable, content=None, now=None) -> dict:
    """When each model posts, for how many profiles, and whether the stock lasts.

    Three facts that are only useful together. "Laila posts at 09:00 and 17:00"
    says nothing on its own; "Laila posts twice a day across 8 profiles, which is
    16 posts, and there are 6 videos free" is a decision. The page had the last
    of those (Content stock) and neither of the first two, so the question it
    could not answer was the one worth asking before a posting day: does what we
    have cover what is scheduled?

    A model with no times picked is not switched off -- that is the flexible
    mode, where the queue loop posts whenever a video is free, up to its daily
    cap. Reporting an empty Reel Post Times as "not scheduled" would read as a
    fault, and it is the default state of every model row.
    """
    from adb_bot.automation import queue_runner

    out = {"models": [], "timezone": queue_runner.DEFAULT_TIMEZONE,
           "fallback": list(queue_runner.DEFAULT_SLOT_TIMES), "per_model": True,
           # Posting slots are wall clock for the audience; the loop timetable on
           # the same tab is wall clock for the server, and this box runs UTC
           # while the slots are Berlin. Two tables of times that are not in the
           # same clock have to say so.
           "server_timezone": "", "same_clock": True,
           "error": ""}
    if airtable is None:
        out["error"] = "no Airtable client"
        return out

    # Only the Airtable half is cached. The stock column and the next slot are
    # arithmetic over data the caller already has, and caching those would let
    # this table disagree with the Content stock section below it -- and hold a
    # "next: 17:00" for three minutes after 17:00.
    inputs = _slow("model_inputs", lambda: _model_inputs(airtable))
    if inputs["error"]:
        out["error"] = inputs["error"]
        return out
    counts, schedules = inputs["counts"], inputs["schedules"]
    stock = dict((content or {}).get("by_model") or {})

    # None is the client saying this base has no Reel Post Times field at all,
    # which is not the same as every model being flexible: the queue loop keeps
    # its single global grid, and so must this table.
    out["per_model"] = schedules is not None

    tz = queue_runner._zone(out["timezone"])
    now = now or datetime.now(tz)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)

    # Offsets, not names: "CEST" and "Europe/Berlin" are the same clock spelled
    # two ways, and comparing the spellings would cry wolf every summer.
    server = local_now.astimezone(datetime.now().astimezone().tzinfo)
    out["server_timezone"] = server.tzname() or ""
    out["same_clock"] = server.utcoffset() == local_now.utcoffset()

    for key in sorted(set(counts) | set(schedules or {})):
        schedule = (schedules or {}).get(key)
        if schedules is None:
            times, per_day, flexible = list(out["fallback"]), len(out["fallback"]), False
        elif schedule is not None and schedule.times:
            times, per_day, flexible = list(schedule.times), len(schedule.times), False
        else:
            times, flexible = [], True
            per_day = (schedule.per_day if schedule and schedule.per_day
                       else queue_runner.DEFAULT_ANYTIME_MAX_PER_DAY)

        # `content_stock` keys its models off the spoofed folder name, which it
        # capitalises to keep "jasmin" and "Jasmin" from reading as two models
        # with half the stock each. Match that or every row shows zero.
        name = key.capitalize()
        profiles = counts.get(key, 0)
        out["models"].append({
            "model": name, "times": times, "flexible": flexible,
            "per_day": per_day, "profiles": profiles,
            # In flexible mode this is a ceiling, not a plan: the loop posts when
            # a video is free, up to the cap. The renderer says "up to" for those,
            # because reading it as a plan turns "we have enough" into its
            # opposite.
            "posts_per_day": profiles * per_day,
            "free": stock.get(name, 0),
            # Whether Airtable has a Models row for this name at all. A target
            # whose model does not exist is the MLX inventory leaking into the
            # posting plan -- the 45 "Blank (n)" staging profiles are exactly
            # that -- and it is invisible everywhere else.
            "known": schedules is None or key in (schedules or {}),
            # For a model Airtable does not have a row for, the raw folder its
            # content actually sits in -- the alias map already knows that
            # `01_Raw_Videos/Corina` holds Nikki's clips, and without saying so
            # "Nikki is not a model" looks like an inventory gap rather than two
            # names for one person.
            "raw_folder": _aliased_folder(key),
            "next": _next_slot(times, local_now),
        })
    return out


def _model_inputs(airtable) -> dict:
    """The two Airtable reads behind the schedule table: how many targets each
    model has, and the times it picked. Failures come back in `error` rather
    than raised, so `_slow` caches a bad answer instead of retrying it per page."""
    from adb_bot.automation import queue_runner

    try:
        targets = queue_runner.collect_targets(airtable)
        schedules = queue_runner.schedules_from_airtable(airtable.reel_schedules_by_model())
    except Exception as exc:
        return {"counts": {}, "schedules": None, "error": f"{type(exc).__name__}: {exc}"}
    counts: dict = {}
    for target in targets:
        counts[target.model_key] = counts.get(target.model_key, 0) + 1
    return {"counts": counts, "schedules": schedules, "error": ""}


def posting_outlook(queue_rows, schedules=None, now=None) -> dict:
    """When the next posts actually happen, as timestamps rather than a policy.

    "Any time, up to 7 a day" is what the *rule* is; it is not an answer to "when
    does Laila 3 post next", and the schedule table could only ever give the
    rule. The answer is arithmetic on the Posting Queue: a flexible target may
    post again `DEFAULT_ANYTIME_GAP_MINUTES` after its last scheduled post, up
    to its model's daily cap, so its next possible moment is a real time on the
    clock.

    Note what "eligible" does and does not mean. It is the *schedule* allowing a
    post, not a promise of one: the queue loop still needs a spoofed video that
    no other row has claimed, and on this fleet that -- not the clock -- is
    usually what decides whether anything goes out.

    Rows already Pending are the other half, and the more literal one: they
    carry a real Scheduled DateTime and the posting loop takes them on its next
    tick once it passes. A future-dated one is the retry pass holding a failed
    row back, which is the only thing on this base that schedules ahead.
    """
    from adb_bot.automation import queue_runner
    from adb_bot.clients import airtable as at

    out = {"queued": [], "profiles": [], "gap_minutes": queue_runner.DEFAULT_ANYTIME_GAP_MINUTES,
           "default_cap": queue_runner.DEFAULT_ANYTIME_MAX_PER_DAY, "timezone": "",
           "eligible_now": 0, "waiting": 0, "capped": 0, "any_fixed": False}

    tz = queue_runner._zone(queue_runner.DEFAULT_TIMEZONE)
    out["timezone"] = queue_runner.DEFAULT_TIMEZONE
    now = now or datetime.now(tz)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    gap = timedelta(minutes=out["gap_minutes"])

    caps: dict = {}
    for key, schedule in (schedules or {}).items():
        caps[key] = schedule.per_day or out["default_cap"]
        if schedule.times:
            out["any_fixed"] = True

    last: dict = {}
    today_count: dict = {}
    for record in queue_rows or []:
        fields = record.get("fields", {}) or {}
        name = str(fields.get(at.F_PQ_NAME) or "").strip()
        stamp = _parse_airtable_dt(fields.get(at.F_PQ_SCHEDULED))
        # The row's Name is "<profile> / <slot>" -- the only place a queue row
        # says which target it belongs to without resolving its link.
        if not stamp or "/" not in name:
            continue
        who = name.rsplit("/", 1)[0].strip()
        when = stamp.astimezone(tz)
        if who not in last or when > last[who]:
            last[who] = when
        if when.date() == local_now.date():
            today_count[who] = today_count.get(who, 0) + 1
        if str(fields.get(at.F_PQ_POST_STATUS) or "") == at.POST_STATUS_PENDING:
            out["queued"].append({
                "name": name, "profile": who,
                "when": when.strftime("%H:%M"),
                "day": when.strftime("%Y-%m-%d"),
                "due": when <= local_now,
                "seconds": max(0.0, (when - local_now).total_seconds()),
            })
    out["queued"].sort(key=lambda row: (row["day"], row["when"]))

    for who, when in last.items():
        model = who.split()[0].lower() if who.split() else ""
        cap = caps.get(model, out["default_cap"])
        posted = today_count.get(who, 0)
        nxt = when + gap
        if cap and posted >= cap:
            state = "capped"
        elif nxt <= local_now:
            state = "ready"
        else:
            state = "waiting"
        out[{"capped": "capped", "ready": "eligible_now", "waiting": "waiting"}[state]] += 1
        out["profiles"].append({
            "profile": who, "model": who.split()[0] if who.split() else who,
            "last": when.strftime("%H:%M"), "last_day": when.strftime("%Y-%m-%d"),
            "next": "now" if state == "ready" else nxt.strftime("%H:%M"),
            "seconds": 0.0 if state == "ready" else max(0.0, (nxt - local_now).total_seconds()),
            "today": posted, "cap": cap, "state": state,
        })
    # Soonest first, and a profile that could post this second before one that
    # cannot: the top of this list is what the next posting tick will consider.
    out["profiles"].sort(key=lambda row: (row["state"] == "capped", row["seconds"], row["profile"]))
    return out


def _schedules_for_outlook(airtable):
    """The per-model schedules `posting_outlook` needs for each model's daily cap,
    off the same cached read the schedule table uses."""
    return _slow("model_inputs", lambda: _model_inputs(airtable))["schedules"]


def _parse_airtable_dt(value):
    """Airtable's ISO dateTime (UTC 'Z') as an aware datetime; None if unusable."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _aliased_folder(model_key: str) -> str:
    """The raw folder whose contents belong to `model_key`, when it is not named
    after that model. "" when the folder and the model agree."""
    from adb_bot.automation.spoof_pipeline import RAW_FOLDER_MODEL_ALIASES

    for folder, model in RAW_FOLDER_MODEL_ALIASES.items():
        if str(model).strip().lower() == model_key:
            return folder.capitalize()
    return ""


def _next_slot(times, local_now) -> str:
    """The next time today one of `times` comes round, "tomorrow" once they have
    all passed, and "" for a model on no fixed times at all."""
    from adb_bot.automation.queue_runner import parse_slot_times

    slots = parse_slot_times(times)
    if not slots:
        return ""
    for slot in slots:
        if local_now.replace(hour=slot.hour, minute=slot.minute,
                             second=0, microsecond=0) > local_now:
            return slot.strftime("%H:%M")
    return f"{slots[0].strftime('%H:%M')} tomorrow"


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
_slow_cache: dict = {}


def collect(airtable=None, now=None, use_cache: bool = True) -> dict:
    """Everything the page shows. Cached for `CACHE_SECONDS`."""
    if use_cache and _cache["data"] is not None and (time.time() - _cache["at"]) < CACHE_SECONDS:
        return _cache["data"]

    now = now or datetime.now()
    day = _today(now)

    runs = parse_posting_runs(day=day)
    retries: list = []
    try:
        retries = retry_events(day=day)
        annotate_runs(runs, names=profile_names(), events=retries)
    except Exception:
        # Names and retry verdicts are the nice-to-have half of the run detail;
        # the outcomes themselves are already parsed. Losing the annotation must
        # not cost the page the runs.
        pass
    try:
        videos = video_runs(day=day, tick_runs=runs, events=retries)
    except Exception:
        videos = []
    posts_today = sum(r.posts for r in runs)
    run_seconds = sum(r.seconds for r in runs if r.seconds)
    attempts = sum(r.attempts for r in runs)
    mlx_500 = sum(r.mlx_500 for r in runs)

    data = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "day": day,
        "now": running_now(),
        "server": server_stats(),
        "phone": phone_durations(day=day),
        "disks": disk_usage(),
        "uptime": uptime_seconds(),
        "top_processes": top_processes(),
        "cpu_processes": cpu_processes(),
        "spoof": {"now": spoof_now(), "models": [], "clips": 0, "variants": 0,
                  "unroutable": [], "error": ""},
        "phones": phone_processes(),
        "timers": timer_states(),
        "runs": runs,
        "videos": videos,
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
        "content": {"ready": 0, "drawable": 0, "held": 0, "by_model": {}, "held_by_model": {}},
        "needs_human": {"rows": [], "retrying": [], "profiles": [], "error": ""},
        "schedules": {"models": [], "timezone": "", "fallback": [], "per_model": True,
                      "error": ""},
        "outlook": {"queued": [], "profiles": [], "gap_minutes": 0, "default_cap": 0,
                    "timezone": "", "eligible_now": 0, "waiting": 0, "capped": 0,
                    "any_fixed": False},
        "airtable_error": "",
    }

    if airtable is not None:
        # Outside the block below on purpose: this one reports its own failures
        # in place (`spoof.error`) and must not be skipped because an unrelated
        # Airtable call above it raised. The local half -- what is on the
        # encoder right now -- is already collected either way.
        data["spoof"].update(spoof_queue(airtable))
        try:
            # One listing serves both: the day's rows, and which variants every
            # row (of any age) has already claimed.
            rows = airtable.list_queue_rows()
            data["queue"] = queue_today(airtable, day, rows=rows)
            data["content"] = content_stock(airtable, claimed=claimed_variant_ids(rows))
            data["needs_human"] = needs_human(airtable)
            # After `content`: the schedule table reads its per-model stock from
            # it, and a schedule with no stock beside it is half the answer.
            data["schedules"] = model_schedules(airtable, content=data["content"], now=now)
            # Reuses the listing above rather than asking again -- and the
            # schedules it needs are the ones just read, not a second copy.
            data["outlook"] = posting_outlook(
                rows, schedules=_schedules_for_outlook(airtable), now=now)
            # Redo the per-video view with the queue in hand: a clip whose
            # profile never reached a phone leaves no trace on this box, and
            # only its queue row can say whether it is waiting or was written
            # off. Local sources still win where they disagree.
            data["videos"] = video_runs(day=day, tick_runs=runs, events=retries,
                                        queue_rows=rows,
                                        variants=airtable.variants_by_id())
        except Exception as exc:
            # A dashboard that 500s because Airtable is having a moment is worse
            # than one that says so and still shows everything local.
            data["airtable_error"] = f"{type(exc).__name__}: {exc}"

    _cache.update(at=time.time(), data=data)
    return data


def invalidate_cache() -> None:
    _cache.update(at=0.0, data=None)
    _slow_cache.clear()
