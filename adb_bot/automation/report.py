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
from datetime import date, datetime, timedelta
from pathlib import Path

from adb_bot.automation import schedule_spec, warmup_completion

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
    # `label` rather than a bare "(unknown)": a phone whose name could not be
    # read is still a phone with an id, and an 18-digit id can be pasted into
    # MultiLogin to find out whose it is. The word cannot be. It also used to
    # defeat the profile-id fallback further down `live_work`, because a row
    # already holding the string "(unknown)" is not an empty one.
    rows = [{"pid": p.pid, "name": p.label, "profile_id": p.profile_id,
             "age_seconds": p.age_seconds, "rss_mb": p.rss_mb,
             "orphan": p.pid in orphan_pids} for p in phones]
    rows.sort(key=lambda r: r["age_seconds"])
    return rows


#: What each loop does to a profile it holds, in the words someone reading the
#: page would use. The key is the `owner=` a loop writes into its lock file.
_LOOP_WORK = {
    "posting": "posting a reel",
    "warmup": "warm-up actions",
    "recheck": "checking a post landed",
    "ui": "driven by hand from the desktop app",
}


def slot_holders() -> list:
    """Which loop holds each live-phone slot, and since when.

    The slot is the only ownership mark some loops leave. The recheck probe
    opens a phone under a slot and takes **no profile lock at all** -- it drives
    one profile, so it has nothing to exclude anyone else from -- and without
    reading slots its phone shows up owned by nobody, which is this page's word
    for an orphan and the exact opposite of the truth.
    """
    from adb_bot.core import locks

    try:
        paths = sorted(locks.slot_dir().glob(f"*{locks._SLOT_SUFFIX}"))
    except Exception:
        return []

    now, out = time.time(), []
    for path in paths:
        try:
            if locks._slot_is_reclaimable(path, locks.SLOT_TTL_SECONDS):
                continue                       # owner is gone; not a live phone
            payload = path.read_text(encoding="utf-8", errors="replace")
            age = max(0.0, now - path.stat().st_mtime)
        except Exception:
            continue
        fields = dict(token.partition("=")[::2] for token in payload.split())
        out.append({"slot": path.stem, "owner": fields.get("owner") or "unknown",
                    "pid": fields.get("pid") or "", "age_seconds": age})
    return out


def live_work(now=None) -> list:
    """One row per profile being worked on right now: which loop has it, what
    that loop does, and when its phone came up.

    Two sources, joined on the profile id, because neither alone is the answer:

    * The **lock** says which loop owns the profile. It is the only thing that
      knows *why* a phone is open -- posting, warm-up and recheck all open the
      same phones the same way.
    * The **phone process** says when work on that profile actually started.
      Locks are taken for a whole batch up front (`ProfileLocks.acquire_all`),
      so a lock's age can be ten minutes older than the phone under it and
      would report a profile as "running for 12 minutes" while it sat in the
      queue waiting for a slot. The phone's start is the honest clock.

    A phone with no lock is not automatically unowned: `slot_holders` covers
    the loops that take a slot and no profile lock (recheck), and only what is
    left after that is reported as belonging to nobody.

    Rows appear for a lock with no phone yet (launching) and for a phone with
    no lock (an orphan, or a run that died) -- both are things worth seeing,
    and dropping either would make this table agree with neither tab above it.
    """
    now = now or datetime.now()
    held, _stale = profile_locks()
    phones = phone_processes()
    slots = slot_holders()

    rows: dict = {}

    def blank(key):
        return rows.setdefault(key, {
            "profile_id": "", "name": "", "loop": "", "doing": "", "pid": "",
            "started": "", "for_seconds": 0.0, "held_for_seconds": 0.0,
            "has_phone": False, "has_lock": False, "orphan": False,
            "by_slot": False, "reel": "", "reel_path": "", "reel_row": "",
        })

    for entry in held:
        row = blank(entry["profile_id"])
        row.update(profile_id=entry["profile_id"], name=entry["name"] or "",
                   loop=entry["owner"], has_lock=True,
                   held_for_seconds=entry["age_seconds"])
        row["doing"] = _LOOP_WORK.get(entry["owner"], "")

    for phone in phones:
        # A phone whose profile could not be read from argv still belongs on the
        # page -- it is holding memory and a slot -- so it is keyed by its pid
        # rather than dropped for having no id to join on.
        key = phone["profile_id"] or f"pid:{phone['pid']}"
        # Two processes can carry one profile id for a moment: readiness
        # relaunches a profile whose launch did not take, and both are open
        # until MultiLogin reaps the first. Collapsing them onto one row would
        # put this table one phone below the Server tab's count, which is the
        # one number a reader checks it against.
        if rows.get(key, {}).get("has_phone"):
            key = f"pid:{phone['pid']}"
        row = blank(key)
        row.update(profile_id=phone["profile_id"] or row["profile_id"],
                   pid=phone["pid"], has_phone=True, orphan=phone["orphan"],
                   for_seconds=phone["age_seconds"])
        if not row["name"] or row["name"] == "(unknown)":
            row["name"] = phone["name"]

    # A loop holding more slots than it holds profile locks is driving phones
    # without one. Subtracting rather than just listing slot owners matters:
    # posting takes a slot *and* a lock per profile, so its slots are already
    # spoken for and attributing an unlocked phone to it would be wrong.
    spare = Counter(slot["owner"] for slot in slots)
    spare.subtract(Counter(entry["owner"] for entry in held))
    unattributed = sorted(owner for owner, count in spare.items() if count > 0)

    claimed = Counter()
    for row in rows.values():
        if not row["has_lock"] and not row["orphan"] and unattributed:
            # One candidate is an answer; several is a narrowing, and saying so
            # beats picking one of them and sounding certain.
            if len(unattributed) == 1:
                row.update(loop=unattributed[0], by_slot=True)
                row["doing"] = _LOOP_WORK.get(unattributed[0], "")
                claimed[unattributed[0]] += 1
            else:
                row["by_slot"] = True
                row["doing"] = "one of: " + ", ".join(unattributed)

    # A spare slot with no phone under it is a loop working on a profile whose
    # phone has not come up -- a recheck probe grinding through its fifteen
    # readiness attempts against a profile that never starts, which is a real
    # thing this fleet does and the reason a slot can sit occupied for minutes.
    # Without a row for it the panel reads "nothing is running" while a slot of
    # the ceiling is spoken for. There is no profile id in a slot file, so the
    # row can say which loop and for how long, and honestly not which account.
    ages: dict = defaultdict(list)
    for slot in slots:
        ages[slot["owner"]].append(slot["age_seconds"])
    for owner in unattributed:
        spare_ages = sorted(ages[owner], reverse=True)[claimed[owner]:spare[owner]]
        for index, age in enumerate(spare_ages):
            row = blank(f"slot:{owner}:{index}")
            row.update(loop=owner, by_slot=True, held_for_seconds=age,
                       name="(phone not up yet)")
            row["doing"] = _LOOP_WORK.get(owner, "")

    for row in rows.values():
        # No phone yet means the lock was taken and the launch has not landed;
        # the lock's own age is then the only clock there is.
        seconds = row["for_seconds"] if row["has_phone"] else row["held_for_seconds"]
        row["for_seconds"] = seconds
        row["started"] = (now - timedelta(seconds=seconds)).strftime("%H:%M:%S")
        if not row["doing"]:
            # A loop this module has no phrase for is still a loop that holds
            # the profile -- name it rather than reporting the profile as
            # unowned, which is what an operator acts on.
            row["doing"] = (f"held by the {row['loop']} loop" if row["loop"]
                            else "phone open, no loop holds it" if row["has_phone"]
                            else "waiting on its phone")
        if not row["name"]:
            row["name"] = row["profile_id"] or "(unknown)"

    return sorted(rows.values(), key=lambda r: r["for_seconds"], reverse=True)


def annotate_live_reels(rows, queue_rows, profiles=None, variants=None) -> None:
    """Fill in which reel each posting profile is sending, in place.

    Nothing writes the in-flight clip anywhere a reader can see it: the posting
    runner holds it in memory and the local ledger only learns of it once Share
    has been tapped, which is the *end* of a post. So it is derived instead --
    a Posting Queue row stays `Pending` until its result is written, so the
    oldest still-Pending row for a profile the posting loop is holding is the
    one in flight on it.

    Deliberately only for `posting`. A profile held by warm-up or recheck has
    queue rows too, and captioning them with a reel it is not sending would be
    an invention dressed as a reading.
    """
    from adb_bot.clients import airtable as at

    profiles, variants = profiles or {}, variants or {}
    by_launch: dict = {}
    for record in queue_rows or []:
        fields = record.get("fields", {}) or {}
        if at._select_name(fields.get(at.F_PQ_POST_STATUS)) != at.POST_STATUS_PENDING:
            continue
        links = fields.get(at.F_PQ_TARGET_PROFILE) or []
        launch_id = (profiles.get(links[0]) or {}).get("launch_id") if links else None
        if not launch_id:
            continue
        variant_links = fields.get(at.F_PQ_SPOOF_VARIANT) or []
        path = (variants.get(variant_links[0]) or {}).get("file_path") if variant_links else None
        by_launch.setdefault(str(launch_id), []).append({
            "scheduled": str(fields.get(at.F_PQ_SCHEDULED) or ""),
            "name": str(fields.get(at.F_PQ_NAME) or ""),
            "path": str(path or ""),
        })

    for row in rows or []:
        if row.get("loop") != "posting":
            continue
        due = sorted(by_launch.get(str(row.get("profile_id")), []),
                     key=lambda r: r["scheduled"] or "9999")
        if not due:
            continue
        row["reel_path"] = due[0]["path"]
        row["reel"] = due[0]["path"].rsplit("/", 1)[-1]
        row["reel_row"] = due[0]["name"]


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
    # `unclaimed`: a raw folder that exists but has no profile to spoof for AND
    # no clips waiting. That is exactly what a half-onboarded model looks like on
    # the day somebody makes its Drive folder, and until this key existed it was
    # the one state the page could not show -- `list_by_model` drops empty
    # folders, so the model was invisible rather than merely idle.
    out = {"models": [], "clips": 0, "variants": 0, "unroutable": [],
           "unclaimed": [], "error": ""}
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

    aliases = spoof_pipeline.raw_folder_model_aliases()
    # In its own try: the empty-folder listing is an extra, and the panel it is
    # bolted onto answers "how far behind is the encoder", which must keep
    # working even if the folder listing misbehaves.
    try:
        lister = getattr(source, "list_folder_names", None)
        all_folders = sorted(str(f) for f in lister()) if callable(lister) else []
    except Exception:
        all_folders = []
    for folder in all_folders:
        if folder in by_folder:
            continue
        model = spoof_pipeline.resolve_model(folder, aliases)
        if not targets.get(model.lower()):
            out["unclaimed"].append({"folder": folder, "model": model})

    for folder, videos in sorted(by_folder.items()):
        model = spoof_pipeline.resolve_model(folder, aliases)
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


# A day's rate is counted in *rows*, never in attempts. One Posting Queue row is
# one scheduled post, and its Post Status is that post's final verdict: the retry
# pass moves a row Failed -> Pending and the row is posted again under the same
# id, so a clip that needed three goes still ends as a single Posted row. That is
# the whole reason this reads off Airtable rather than off the posting log, where
# the same clip appears once per attempt and two failures followed by a success
# would score 33%.
#
# Only these two are a verdict. Pending and Verifying are the day still running:
# a Pending row may yet post, and a Verifying one has been posted but not proven,
# so counting either as a failure would libel a day that has not finished.
SETTLED_STATUSES = ("Posted", "Failed")
DAILY_HISTORY_DAYS = 30


def parked_reason(row, profiles_by_recid) -> str:
    """*Why* an unsettled row is waiting on a person, or "" if it is not.

    The reason, not just the fact, because "137 waiting on a person" is a number
    nobody can act on: a flagged phone, a phone mid hand-off and a phone with no
    MultiLogin id are three different jobs for three different people. The
    string is shown on the page as-is, so it names the gate in the words someone
    fixing it would use.

    Deliberately the *posting planner's* gates, in its order, because the
    planner is what decides: `needs_human`, then Status, then the warm-up
    hand-off, then a missing MLX id. Anything this returns "" for is a row the
    planner would accept today.

    Two limits worth stating rather than hiding:

    * a row with no profile link at all is parked -- the planner skips it for
      exactly that reason, and no amount of waiting fixes it;
    * a row linked to an Accounts row instead is counted as postable, because
      its guards live on that row and this summary does not read it. On this
      base every target is a profile, so that branch is currently unused.
    """
    from adb_bot.clients import airtable as at

    fields = row.get("fields") or {}

    # A `Verifying` row has already been posted and is only waiting to be
    # proven, so it is never parked however its phone looks *now* -- a phone
    # flagged after its post went out would otherwise drag that post into the
    # parked column and make a finished job look stuck.
    if at._select_name(fields.get(at.F_PQ_POST_STATUS)) == at.POST_STATUS_VERIFYING:
        return ""

    profile_links = fields.get(at.F_PQ_TARGET_PROFILE) or []
    if not profile_links:
        # No profile link. An account-linked row is judged elsewhere; a row with
        # neither link is one the planner throws away every tick.
        return "" if (fields.get(at.F_PQ_TARGET_ACCOUNT) or []) else "linked to no profile"

    info = (profiles_by_recid or {}).get(profile_links[0])
    if info is None:
        # Linked to a profile the map does not hold. Unknown, not clear -- and
        # an unknown phone has never posted anything on its own.
        return "phone not in the profile list"

    if info.get("needs_human"):
        return "flagged for a person"
    status = info.get("status")
    if status is not None and status != at.STATUS_SELECT_ACTIVE:
        return f"phone is {status}"
    if info.get("warmup_started") and info.get("handoff_outstanding"):
        return "warm-up hand-off unfinished"
    return "" if info.get("launch_id") else "no MultiLogin id"


def row_is_parked(row, profiles_by_recid) -> bool:
    """Whether an unsettled row is waiting on a *person* rather than on a turn.

    "Not settled" lumped two very different things together, and the difference
    is the whole question anyone asks of that number: a row queued behind
    throughput goes out on its own, and a row whose phone is flagged never does.
    On 2026-08-16 the split was 207 parked against 14 that would post -- read as
    one figure, "221 still to settle" sounds like a busy evening rather than a
    backlog nobody is working.

    The gates live in `parked_reason`, which this only reduces to a yes/no: two
    copies of the planner's rules would drift, and the day they disagreed the
    two tabs would each be confidently wrong about the same row.
    """
    return bool(parked_reason(row, profiles_by_recid))


def daily_success(rows=None, limit: int = DAILY_HISTORY_DAYS,
                  profiles_by_recid=None) -> dict:
    """Confirmed vs failed posts per day, newest first.

    Grouped by the day a post was *due* (`Scheduled DateTime`), not the day the
    phone ran. Those differ whenever a retry crosses midnight, and the due day is
    the one worth reporting: it answers "of the posts that day owed, how many
    landed?", and it keeps a row's retries on the day whose slot they were
    filling instead of smearing one clip across two rates.

    `profiles_by_recid` is `profile_launch_map()`. Given it, each unsettled row
    is split into **parked** (waiting on a person -- see `row_is_parked`) and
    **to_post** (the valid ones, which go out on their own). Without it both
    keys are None and the caller shows the old single figure: a missing column
    on Profiles (Cloning) must cost this table its new detail, not the rate it
    has always shown.
    """
    from adb_bot.clients import airtable as at

    rows = rows if rows is not None else []
    fields = lambda r: (r.get("fields") or {})            # noqa: E731

    by_day = defaultdict(Counter)
    parked_by_day: dict = defaultdict(int)
    undated = 0
    for row in rows:
        day = str(fields(row).get(at.F_PQ_SCHEDULED, ""))[:10]
        if not day:
            # A row with no slot cannot be attributed to a day. Rare, and never
            # silently: the count is reported so the totals can be reconciled.
            undated += 1
            continue
        status = fields(row).get(at.F_PQ_POST_STATUS) or "(empty)"
        by_day[day][status] += 1
        # Only unsettled rows can be parked: a Posted row went out and a Failed
        # one is the retry pass's business, whatever its phone looks like now.
        if profiles_by_recid is not None and status not in SETTLED_STATUSES:
            if row_is_parked(row, profiles_by_recid):
                parked_by_day[day] += 1

    days = []
    for day in sorted(by_day, reverse=True):
        counts = by_day[day]
        posted, failed = counts.get("Posted", 0), counts.get("Failed", 0)
        settled = posted + failed
        unsettled = sum(n for status, n in counts.items()
                        if status not in SETTLED_STATUSES)
        parked = parked_by_day[day] if profiles_by_recid is not None else None
        days.append({
            "day": day,
            "posted": posted,
            "failed": failed,
            "unsettled": unsettled,
            "parked": parked,
            # The valid ones: unsettled minus parked. `Verifying` rows count
            # here rather than as parked -- they have been posted and are
            # waiting to be proven, which is the opposite of stuck.
            "to_post": (None if parked is None else unsettled - parked),
            "total": settled + unsettled,
            # None, not 0.0: a day with nothing settled has no rate yet, and
            # rendering that as "0%" would read as a total wipeout.
            "rate": (100.0 * posted / settled) if settled else None,
        })

    omitted = max(0, len(days) - limit)
    days = days[:limit]

    posted = sum(d["posted"] for d in days)
    failed = sum(d["failed"] for d in days)
    settled = posted + failed
    return {
        "days": days,
        "totals": {
            "posted": posted,
            "failed": failed,
            "unsettled": sum(d["unsettled"] for d in days),
            "parked": (None if profiles_by_recid is None
                       else sum(d["parked"] for d in days)),
            "to_post": (None if profiles_by_recid is None
                        else sum(d["to_post"] for d in days)),
            "rate": (100.0 * posted / settled) if settled else None,
        },
        "omitted": omitted,
        "undated": undated,
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

# Imported, not redeclared: this page's job is to say which rows the retry pass
# will still pick up, and a second copy of the number is a page that quietly
# lies the day the limit changes.
from adb_bot.automation.retry_runner import DEFAULT_MAX_RETRIES  # noqa: E402


def second_accounts(airtable, rows=None, day: str = "") -> dict:
    """Phones carrying two Instagram accounts, and what each account did today.

    Two accounts on one phone are invisible everywhere else on this page: they
    share a Profiles row, a device and a launch key, so every other table counts
    them as one phone doing one phone's work. This is the one place that says
    otherwise -- and, more usefully, the place that shows a second account which
    is configured but *not posting*, which looks identical to a working one
    until you count its rows.

    `supported` False means the base has no `Has Second Account` field at all,
    which is a different thing from no phone having one.
    """
    from adb_bot.clients import airtable as at

    out = {"profiles": [], "supported": True, "error": "",
           "counts": {"phones": 0, "usable": 0, "incomplete": 0,
                      "posted_today": 0, "expected_today": 0}}
    try:
        profiles = airtable.second_account_profiles()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if profiles is None:
        out["supported"] = False
        return out
    if not profiles:
        return out

    rows = rows if rows is not None else []
    fields = lambda r: (r.get("fields") or {})            # noqa: E731

    # Today's rows only, keyed the same way `queue_today` keys them: the day a
    # post was due, not the day a phone happened to run it.
    by_profile: dict = {}
    for row in rows:
        f = fields(row)
        if day and not str(f.get(at.F_PQ_SCHEDULED, "")).startswith(day):
            continue
        links = f.get(at.F_PQ_TARGET_PROFILE) or []
        if not links:
            continue
        slot = at._select_name(f.get(at.F_PQ_ACCOUNT_SLOT)) or at.SLOT_PRIMARY
        status = f.get(at.F_PQ_POST_STATUS) or "(empty)"
        by_profile.setdefault(links[0], {}).setdefault(slot, Counter())[status] += 1

    for profile in profiles:
        slots = by_profile.get(profile["record_id"], {})
        primary = dict(slots.get(at.SLOT_PRIMARY, Counter()))
        second = dict(slots.get(at.SLOT_SECOND, Counter()))
        entry = dict(profile)
        entry["primary_today"] = primary
        entry["second_today"] = second
        entry["posted_today"] = (primary.get("Posted", 0) + second.get("Posted", 0))
        # The question the page exists to answer: is the second account actually
        # being scheduled? A phone whose second account has no rows at all is
        # either newly configured or quietly doing nothing.
        entry["second_queued"] = sum(second.values())
        out["profiles"].append(entry)

    out["counts"] = {
        "phones": len(out["profiles"]),
        "usable": sum(1 for p in out["profiles"] if p["usable"]),
        "incomplete": sum(1 for p in out["profiles"] if not p["usable"]),
        "posted_today": sum(p["posted_today"] for p in out["profiles"]),
        "expected_today": sum(sum(p["second_today"].values()) for p in out["profiles"]),
    }
    return out


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

    out = {"rows": [], "retrying": [], "profiles": [], "retired": [], "error": ""}
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
        entry = {
            # Carried so the site can offer a "somebody looked at this" button.
            # Nothing else needs it, and the page never shows it.
            "record_id": record.get("id") or "",
            "name": field(record, at.F_PROF_NAME) or "(unnamed)",
            "reason": field(record, at.F_PROF_ISSUE_REASON) or "(none set)",
            "status": field(record, at.F_PROF_STATUS) or "Active",
            "flagged_at": str(field(record, at.F_PROF_FLAGGED_AT) or "")[:16].replace("T", " "),
            # Newest entry first in the field itself, so the first line is the
            # current story and the rest is history nobody needs on a dashboard.
            "note": str(field(record, at.F_PROF_ISSUE_NOTES) or "").splitlines()[:1],
        }
        # A flag on a phone MultiLogin no longer has is not work anybody can do.
        # The flag is left ticked in Airtable on purpose -- it protects the
        # hand-written diagnosis in Issue Notes -- so the filtering has to happen
        # here rather than by clearing the box.
        if profile_retired(entry):
            out["retired"].append(entry["name"])
            continue
        out["profiles"].append(entry)
    out["profiles"].sort(key=lambda e: (e["reason"], e["name"]))
    out["retired"].sort()
    return out


def profile_retired(profile) -> bool:
    """Is this profile's phone gone from MultiLogin?

    `Issue Reason = Profile Deleted From MLX` means the MLX profile no longer
    exists, so no loop can ever launch, post or recover it. Twenty rows were in
    that state on 2026-08-18 and eleven of them were *also* flagged, which put
    them on the VA worklist under labels like "Human Verification Required" --
    asking a person to sign in to a phone that is not there.

    Retired rows are dropped from the people-facing tabs rather than deleted:
    keeping the row is deliberate (retiring a phone is a client decision), and
    the count is still reported, because a worklist that silently shrinks is
    the same failure as one padded with impossible work.

    Read off `Issue Reason` and not off a live MultiLogin diff: the tabs must
    classify the same way when MultiLogin is unreachable, and an inventory that
    failed to load looks exactly like every phone having been deleted.
    """
    from adb_bot.clients import airtable as at

    return str((profile or {}).get("reason") or "").strip() == at.PROFILE_ISSUE_DELETED


def drop_retired(profiles) -> tuple:
    """``(kept, retired_names)`` -- split a profile listing on `profile_retired`."""
    kept, retired = [], []
    for profile in profiles or []:
        (retired if profile_retired(profile) else kept).append(profile)
    return kept, sorted(str(p.get("name") or "(unnamed)") for p in retired)


# How a profile is classified on the Profiles tab, worst-first. A profile is in
# exactly one of these, and the order is the precedence: a flagged phone that is
# also posting belongs on somebody's worklist, not in the healthy column.
STAGE_ORDER = ("needs_person", "handoff", "posting", "ready", "warming", "parked", "other")
STAGE_LABELS = {
    "needs_person": "Needs a person",
    "handoff": "Waiting on bio / picture / first post",
    "posting": "Posting",
    "ready": "Ready to post",
    "warming": "Warming up",
    "parked": "Parked (Inactive)",
    "other": "Other",
}


def _handoff_outstanding(profile: dict) -> list:
    """Which of the three hand-off tasks this profile still needs, in order."""
    from adb_bot.clients import airtable as at

    handoff = profile.get("handoff") or {}
    return [at.HANDOFF_LABELS[name] for name in at.HANDOFF_FIELDS if not handoff.get(name)]


def classify_profile(profile: dict, warming: set = (), finished: set = ()) -> str:
    """Which single column of the Profiles tab this profile belongs in.

    `warming` and `finished` are sets of serials from the warm-up campaign --
    the MLX `Created` tag is the population and Airtable does not hold it, so
    "is this phone warming up" cannot be answered from the row alone.
    """
    serial = profile.get("serial") or ""
    if profile.get("needs_human"):
        return "needs_person"
    if profile.get("status") == "Inactive":
        return "parked"
    if serial in finished and _handoff_outstanding(profile):
        return "handoff"
    if profile.get("queue_rows") or profile.get("accounts"):
        return "posting"
    if serial in finished:
        return "ready"
    if serial in warming:
        return "warming"
    return "other"


def folder_by_serial(mlx_items=None, folder_names=None) -> dict:
    """``{MLX serial_no: folder name}``, for the phones MultiLogin still has.

    Airtable has no folder column, so MultiLogin is the only place the grouping
    exists and every panel that wants to name a phone's folder has to build the
    same map. Shared rather than rebuilt per section so the hand-off list and
    the folder breakdown cannot disagree about where the same phone lives.

    A serial MLX does not return is simply absent: the caller decides what to
    say about it, because "no folder" and "not in MultiLogin any more" read
    differently on a worklist than they do in a count.
    """
    from adb_bot.automation.mlx_sync import normalize_mlx_item

    out: dict = {}
    for item in mlx_items or []:
        normalized = normalize_mlx_item(item)
        if normalized is None:
            continue
        folder_id = str(item.get("folder_id") or "")
        out[normalized.serial_no] = (folder_names or {}).get(folder_id) or ""
    return out


def _finished_stage() -> str:
    """The Airtable Warm-up Stage value that means "done", or "" if unreadable."""
    try:
        from adb_bot.automation import warmup_state
        return warmup_state.TAG_FINISHED
    except Exception:
        return ""


def warmup_finished(profile: dict, entry: dict, finish_day: int,
                    finished_stage: str = None) -> bool:
    """Has this profile finished its warm-up? The one answer the page may give.

    Two sources, and both are needed. The campaign's own reading (`entry`, a row
    of `warmup_progress`) is the fresher one -- a profile that finished an hour
    ago should appear at once, before any loop has written anything -- but it
    only covers the profiles the campaign still tracks. Airtable's Warm-up Stage
    covers the rest: a phone that finished last week and dropped out of the
    campaign's window is still finished, and the hand-off work is still waiting.

    Shared because the Profiles tab and the hand-off worklist were each reading
    one half: the worklist said 25 profiles were waiting on a bio while the
    folder table counted 6, off the same refresh of the same data.
    """
    if entry and warmup_completion.is_finished(int(entry.get("day_done") or 0), finish_day):
        return True
    stage = _finished_stage() if finished_stage is None else finished_stage
    return bool(stage) and (profile or {}).get("warmup_stage") == stage


def handoff_queue(profiles, warmup_progress: dict, folder_of: dict = None) -> dict:
    """Profiles that finished their warm-up and are waiting on a person.

    A phone coming off the warm-up is not a posting target yet. It has no bio,
    no picture and has never posted, and an account whose first ever post is an
    automated reel is the one Instagram acts on -- so the three tasks are a
    person's, and until they are done nothing should schedule a reel for it.

    In practice these profiles cannot post anyway: they are the "Blank (NN)"
    phones, with no model folder, so no Accounts row and no Spoof Variants
    exist for them and the queue has nothing to build a row from. That is an
    accident of how they were made, not a rule -- assign one to a model and it
    becomes postable the same hour. `posting_planner` enforces it properly; this
    is what tells somebody the work is waiting.

    `folder_of` (see `folder_by_serial`) names the MultiLogin folder each phone
    sits in. It is optional because MultiLogin may not answer -- the work is
    still the work when it does not -- but without it the list names phones
    that are mostly called "Blank (NN)", and the person doing the hand-off has
    to open MultiLogin and search to find out whose bio they are writing.
    """
    finished_stage = _finished_stage()

    plan_days = int((warmup_progress or {}).get("plan_days") or 0)
    # The plan's length is for display; what admits a profile to this list is
    # the day whose completion ends the warm-up. They differ whenever the last
    # plan day asks for nothing the warm-up runs, and reading the wrong one is
    # why this list was empty while the warm-up tab counted 41 finished.
    finish_day = int((warmup_progress or {}).get("finish_day") or 0)
    by_serial = {p["serial"]: p for p in (warmup_progress or {}).get("profiles") or []}

    out = {"profiles": [], "done": 0, "plan_days": plan_days, "finish_day": finish_day}
    for profile in profiles or []:
        entry = by_serial.get(profile.get("serial"))
        if not warmup_finished(profile, entry, finish_day, finished_stage):
            continue
        outstanding = _handoff_outstanding(profile)
        if not outstanding:
            out["done"] += 1
            continue
        serial = profile.get("serial") or ""
        out["profiles"].append({
            "name": profile["name"],
            "serial": serial,
            # "" when MultiLogin could not be read at all, and "?" when it was
            # read and does not have this phone -- a real difference to whoever
            # is about to go looking for it. The renderer says which is which.
            "folder": (folder_of or {}).get(serial, "?") if folder_of else "",
            "launch_id": profile.get("launch_id") or "",
            "status": profile.get("status") or "",
            "day": (entry or {}).get("day") or profile.get("warmup_day") or 0,
            "finished_at": (entry or {}).get("last_at") or profile.get("warmup_last_run") or "",
            "outstanding": outstanding,
            "done_tasks": [t for t in ("bio", "profile picture", "first post")
                           if t not in outstanding],
        })

    # The ones a person has already started come last: a half-done profile is
    # somebody's open errand, and an untouched one is nobody's yet.
    out["profiles"].sort(key=lambda p: (-len(p["outstanding"]), p["name"].lower()))
    return out


#: The MultiLogin tag that says a phone runs two Instagram accounts in one
#: cloned app. Two spellings, applied by different people to different models
#: -- `Second Account` on the Jil/Jasmin phones, `2 accounts` on the Nikki ones
#: -- and anything reading them has to accept both or it silently sees half.
SECOND_ACCOUNT_TAGS = ("second account", "2 accounts")


def second_account_untracked(profiles, mlx_items=None) -> list:
    """Phones MultiLogin says carry two accounts that Airtable does not.

    `Has Second Account` on the Airtable row is what the bot acts on: it is what
    gives the second handle its own queue rows and its own spoofed encode. The
    MultiLogin tag is what a *person* applied when they set the phone up, and
    nothing carries one to the other.

    So a phone can be tagged in the workspace people work in and be a plain
    single-account phone everywhere the bot looks -- posting once a slot where it
    should post twice, with nothing anywhere reporting a fault. On 2026-08-16
    that was six phones against fourteen ticked, three of them Active.

    Parked phones are listed too, and marked: they produce nothing either way,
    but they will the moment somebody un-parks them, and finding out then is
    worse than knowing now.
    """
    from adb_bot.automation.mlx_sync import normalize_mlx_item

    by_serial = {p.get("serial") or "": p for p in profiles or []}
    out = []
    for item in mlx_items or []:
        profile = normalize_mlx_item(item)
        if profile is None:
            continue
        tags = [str(t) for t in (profile.tags or ())
                if str(t).lower() in SECOND_ACCOUNT_TAGS]
        if not tags:
            continue
        row = by_serial.get(profile.serial_no)
        if row is None or row.get("has_second"):
            continue
        out.append({
            "name": (row.get("name") if row else "") or profile.name,
            "serial": profile.serial_no,
            "tag": ", ".join(tags),
            "status": row.get("status") or "",
            # A parked phone is not losing posts today; an Active one is losing
            # every second-account post it should be making, right now.
            "live": (row.get("status") or "") != "Inactive",
        })
    # The Active ones first: those are the ones costing posts.
    out.sort(key=lambda p: (not p["live"], p["name"].lower()))
    return out


def mlx_only_issues(profiles, mlx_items=None) -> dict:
    """Phones carrying MultiLogin's `Issue` tag that Airtable does not flag.

    The dashboard's worklist is `{Needs Human Check}=1` on Airtable, and
    `issue_tags` mirrors that checkbox *onto* the MultiLogin tag. The mirror is
    deliberately one-way -- see that module on why a reverse sync would be
    wrong -- which leaves a blind spot nothing was reporting: a tag applied **by
    hand** in the workspace, where the VAs actually work, reaches no system at
    all. On 2026-08-10 that was 85 phones tagged `Issue` in MultiLogin against
    29 flagged in Airtable: 56 phones somebody had marked and nothing counted.

    Twenty-one of the 56 are in the warm-up, which is what makes this worth a
    section rather than a log line. A warm-up phone is invisible twice over --
    it has no queue rows to fail and no `Needs Human Check` to tick, so neither
    the flagged list nor the abandoned-rows list can ever mention it, and the
    only trace that a person found something wrong with it is the tag they
    added.

    Three groups, because they are three different errands:

    * **warmup** -- in the campaign (`Warm-up Started` is set). Somebody marked
      it mid-warm-up and no loop will act on it. This is the worklist.
    * **parked** -- `Status = Inactive`. Deliberately switched off, and the tag
      is usually the note explaining why. Reported as names, not as work.
    * **other** -- Active, not warming up. Mostly staging blanks; listed because
      "Active and tagged but not flagged" is the state that reads as fine
      everywhere else.

    Read-only, and the classification never ticks anything: turning these into
    real flags would put 56 profiles past `posting_planner`'s hard stop on the
    strength of a hand-applied tag, and re-tick the box the moment a VA cleared
    it while the tag remained. Showing them is the whole fix.
    """
    from adb_bot.automation.issue_tags import ISSUE_TAG
    from adb_bot.automation.warmup_state import tags_by_launch_id
    from adb_bot.clients import airtable as at

    out = {"warmup": [], "parked": [], "other": [], "error": "",
           "counts": {"tagged": 0, "flagged": 0, "unflagged": 0}}
    if not mlx_items:
        # Same refusal as the tag sweep itself: an unread inventory looks
        # exactly like "nobody has tagged anything", and a worklist that
        # silently empties on an outage is worse than one that says why.
        out["error"] = "could not read MultiLogin; tags not checked this refresh"
        return out

    by_launch = tags_by_launch_id(mlx_items)
    wanted = ISSUE_TAG.strip().lower()

    for profile in profiles or []:
        launch_id = str(profile.get("launch_id") or "").strip()
        if not launch_id:
            continue
        held = tuple(by_launch.get(launch_id) or ())
        if wanted not in {str(tag).strip().lower() for tag in held}:
            continue
        out["counts"]["tagged"] += 1
        if profile.get("needs_human"):
            # Already on the worklist above; the tag agreeing with the flag is
            # the mirror working, not a finding.
            out["counts"]["flagged"] += 1
            continue
        out["counts"]["unflagged"] += 1
        entry = {
            "name": profile.get("name") or "(unnamed)",
            "serial": profile.get("serial") or "",
            "launch_id": launch_id,
            "status": profile.get("status") or at.STATUS_SELECT_ACTIVE,
            "day": profile.get("warmup_day") or 0,
            "stage": profile.get("warmup_stage") or "",
            "last_run": profile.get("warmup_last_run") or "",
            # The other tags are the context a person left behind -- `Created`,
            # `Second Account`, `Link` -- and reading them beside the name is
            # usually enough to know which errand this is.
            "tags": [str(tag) for tag in held if str(tag).strip().lower() != wanted],
        }
        # Warm-up first, and before the parked check: the campaign is the more
        # specific fact about a phone, and `Status` is shown in the row anyway.
        if profile.get("warmup_started"):
            out["warmup"].append(entry)
        elif entry["status"] == at.STATUS_SELECT_INACTIVE:
            out["parked"].append(entry)
        else:
            out["other"].append(entry)

    for group in ("warmup", "parked", "other"):
        out[group].sort(key=lambda e: e["name"].lower())
    return out


def folder_breakdown(profiles, mlx_items=None, folder_names=None,
                     warmup_progress: dict = None) -> dict:
    """Every MultiLogin folder, and what its phones are doing.

    The folder is the model. Counting by it answers the question nothing on this
    page could answer before -- "how is Jasmin doing" -- without reading 151
    rows and knowing which "Blank (12)" belongs to whom.
    """
    progress = warmup_progress or {}
    # The same completion test the warm-up tab and the hand-off list use:
    # `classify_profile` files a phone under "handoff" or "ready" off this set,
    # so a second reading of "finished" here would fix those two tabs and leave
    # this one still counting the same phones as "warming up".
    finish_day = int(progress.get("finish_day") or 0)
    finished_stage = _finished_stage()
    by_serial = {row.get("serial") or "": row for row in progress.get("profiles") or []}
    warming = set(by_serial) - {""}
    # Read through `warmup_finished` and over the *profiles*, not just over the
    # campaign's rows: a phone that finished last week has dropped out of the
    # campaign window but still carries the finished Stage, and counting only
    # the campaign's own rows filed 19 phones the hand-off worklist was asking
    # for under "Other" -- 6 against the worklist's 25, same page, same refresh.
    finished = {
        (profile.get("serial") or "")
        for profile in profiles or []
        if warmup_finished(profile, by_serial.get(profile.get("serial") or ""),
                           finish_day, finished_stage)
    } - {""}

    # Serial -> folder, from MLX. Airtable has no folder column, so this is the
    # only place the grouping exists. Shared with the hand-off list.
    folder_of = folder_by_serial(mlx_items, folder_names)

    folders: dict = {}
    for profile in profiles or []:
        # A row whose phone MLX no longer has: real, and worth its own bucket
        # rather than being silently counted under a folder it is not in.
        name = folder_of.get(profile.get("serial") or "", "") or "(no MultiLogin folder)"
        stage = classify_profile(profile, warming=warming, finished=finished)
        bucket = folders.setdefault(name, {"folder": name, "total": 0,
                                           **{key: 0 for key in STAGE_ORDER}})
        bucket["total"] += 1
        bucket[stage] += 1

    rows = sorted(folders.values(),
                  key=lambda f: (f["folder"].startswith("("), -f["total"], f["folder"].lower()))
    totals = {"folder": "All folders", "total": sum(f["total"] for f in rows),
              **{key: sum(f[key] for f in rows) for key in STAGE_ORDER}}
    return {"folders": rows, "totals": totals, "known_folders": len(folder_names or {})}


def todays_posts(rows, day: str, variants=None, variants_fn=None, now=None,
                 profiles_by_recid=None) -> dict:
    """Today's Posting Queue, one line per post: which clip, on which profile.

    The Schedules tab answers "when", per model and as policy. This answers
    "what" -- the reel each profile is actually sending today and how that went
    -- which until now existed nowhere: the queue was only ever shown as five
    status counts.

    `profiles_by_recid` is `profile_launch_map()`, and splits the Pending rows
    into the ones a healthy phone will send on its own (`to_post`) and the ones
    waiting on a person (`parked`), with `parked_reasons` counting why. Without
    it both are None and the page shows the old single "Still to go" tile: a
    missing column on Profiles (Cloning) costs this tab its new detail, not the
    day's posts.

    The split covers **Pending only**, unlike `daily_success`, which splits
    everything unsettled. This tab already shows Verifying as its own tile, so
    folding those rows into `to_post` here would count them twice on one screen
    -- the two tabs then differ by exactly the Verifying count, which is what
    `_section_posts_today` says on the page rather than leaving anyone to
    rediscover it.
    """
    from adb_bot.clients import airtable as at

    out = {"posts": [], "by_status": {}, "by_profile": [], "total": 0,
           "clips": 0, "day": day, "reused_clips": [],
           "parked": None, "to_post": None, "parked_reasons": {}}
    now = now or datetime.now()

    today_rows = [r for r in (rows or [])
                  if str((r.get("fields") or {}).get(at.F_PQ_SCHEDULED) or "").startswith(day)]
    if variants is None and variants_fn is not None and today_rows:
        # Resolved here and not by the caller: naming the clips is a full read
        # of Spoof Variants (~630 records), and on a day with no rows the answer
        # is "nothing" and the read is pure cost. Most renders are idle.
        try:
            variants = variants_fn()
        except Exception:
            variants = {}
    variants = variants or {}

    per_profile: dict = defaultdict(lambda: {"profile": "", "total": 0, "posted": 0,
                                             "failed": 0, "pending": 0, "verifying": 0,
                                             "parked": 0})
    clips = set()
    reasons: Counter = Counter()
    for record in today_rows:
        fields = record.get("fields", {}) or {}
        scheduled = str(fields.get(at.F_PQ_SCHEDULED) or "")
        name = str(fields.get(at.F_PQ_NAME) or "").strip()
        # "<profile> / <slot>" is the only place a row names its target without
        # resolving the link, and resolving 60 links per render is two more
        # table reads for a column that is already in the string.
        who = name.rsplit("/", 1)[0].strip() if "/" in name else name
        status = at._select_name(fields.get(at.F_PQ_POST_STATUS)) or "Pending"

        variant_id = (fields.get(at.F_PQ_SPOOF_VARIANT) or [None])[0]
        clip = ""
        if variant_id:
            path = (variants.get(variant_id) or {}).get("file_path") or ""
            clip = path.rsplit("/", 1)[-1]
            if clip:
                clips.add(clip)

        handle = at._handle(fields.get(at.F_PQ_TARGET_HANDLE))
        key = {"Posted": "posted", "Failed": "failed",
               "Verifying": "verifying"}.get(status, "pending")
        # Only the Pending ones. A Posted row went out, a Failed one is the
        # retry pass's business, and a Verifying one is already on Instagram --
        # asking "will this go out?" of any of the three answers a question
        # nobody asked and would make the tiles sum to more than the day.
        reason = (parked_reason(record, profiles_by_recid)
                  if profiles_by_recid is not None and key == "pending" else "")
        if reason:
            reasons[reason] += 1

        out["posts"].append({
            "name": name,
            "profile": who,
            "when": scheduled[11:16],
            "status": status,
            "clip": clip,
            "handle": handle or "",
            "slot": at._select_name(fields.get(at.F_PQ_ACCOUNT_SLOT)) or "",
            "issue": at._select_name(fields.get(at.F_PQ_ISSUE_TYPE)) or "",
            "retries": fields.get(at.F_PQ_RETRY_COUNT) or 0,
            "parked_reason": reason,
        })
        out["by_status"][status] = out["by_status"].get(status, 0) + 1
        tally = per_profile[who]
        tally["profile"] = who
        tally["total"] += 1
        tally[key] += 1
        if reason:
            tally["parked"] += 1

    out["posts"].sort(key=lambda p: (p["when"], p["profile"]))
    out["total"] = len(out["posts"])
    out["clips"] = len(clips)
    # The same bucket the per-profile "To go" column counts, which is not quite
    # `by_status["Pending"]`: a row with any other unsettled status is still a
    # post that has not gone out, and the tile it sits under has to be the one
    # the split adds up to.
    pending = sum(1 for p in out["posts"]
                  if p["status"] not in ("Posted", "Failed", "Verifying"))
    out["pending"] = pending
    if profiles_by_recid is not None:
        out["parked"] = sum(reasons.values())
        # Not counted independently: the two must add up to the Pending tile
        # beside them, and a remainder is the only definition that cannot drift
        # from it.
        out["to_post"] = pending - out["parked"]
        # Biggest job first -- 109 rows behind a flag and 28 behind a hand-off
        # are two different people's afternoons.
        out["parked_reasons"] = dict(reasons.most_common())
    # Busiest first: on a fleet this size the question is which profile is
    # carrying the day and which has one row and failed it.
    out["by_profile"] = sorted(per_profile.values(),
                               key=lambda p: (-p["total"], p["profile"].lower()))
    # A clip sent twice on one day is the failure the spoof pipeline exists to
    # prevent -- one file on two accounts is what gets them flagged.
    seen: dict = defaultdict(list)
    for post in out["posts"]:
        if post["clip"]:
            seen[post["clip"]].append(post["profile"])
    out["reused_clips"] = sorted(
        ({"clip": clip, "profiles": sorted(set(who))} for clip, who in seen.items()
         if len(set(who)) > 1), key=lambda c: c["clip"])
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


def model_schedules(airtable, content=None, now=None, grid=None) -> dict:
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

    # The grid the queue loop is *started with*, not the one this module ships
    # as a default. They have not matched since 2026-08-05: the unit runs three
    # evening slots while `DEFAULT_SLOT_TIMES` is seven from 09:00, so every
    # model on this table claimed "7/day" against a fleet doing 3, and a model
    # with no times of its own read as scheduled for a grid nobody runs.
    # `queue_grid` already reads the unit for the outlook section below; using
    # it here is what stops the two tables from disagreeing on the same page.
    # Injectable so a test can state which runner it is describing instead of
    # inheriting whatever unit happens to be installed on the machine running
    # the suite -- the same reason `posting_outlook` takes one.
    grid = _slow("queue_grid", queue_grid) if grid is None else grid
    fallback = list(grid.get("slots") or ()) or list(queue_runner.DEFAULT_SLOT_TIMES)

    out = {"models": [], "timezone": queue_runner.DEFAULT_TIMEZONE,
           "fallback": fallback, "fallback_from_unit": bool(grid.get("slots")),
           "per_model": True,
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
    # `.astimezone(tz)` and not `.replace(tzinfo=tz)`: `collect` hands down a
    # naive `datetime.now()`, which is the *server's* wall clock, and stamping
    # Berlin onto a UTC reading moves every time on this tab by two hours.
    # astimezone reads a naive value as system-local and converts it properly.
    local_now = now.astimezone(tz)

    # Offsets, not names: "CEST" and "Europe/Berlin" are the same clock spelled
    # two ways, and comparing the spellings would cry wolf every summer.
    server = local_now.astimezone(datetime.now().astimezone().tzinfo)
    out["server_timezone"] = server.tzname() or ""
    out["same_clock"] = server.utcoffset() == local_now.utcoffset()

    # The running loop decides what this table means, not the Airtable field.
    # `queue_runner` on this box fills a fixed grid and has no per-model-times
    # code in it at all, so a model whose Reel Post Times are blank is NOT
    # "flexible, up to 7 a day" -- it posts on the unit's grid like every other
    # model. Reporting the flexible mode against a grid-only loop is the same
    # mistake `queue_grid` exists to prevent, one table further down the page.
    grid_only = bool(grid.get("slots")) and not grid.get("per_model")
    if grid_only:
        out["per_model"] = False

    for key in sorted(set(counts) | set(schedules or {})):
        schedule = (schedules or {}).get(key)
        if grid_only or schedules is None:
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


def queue_grid(log_path=None) -> dict:
    """What the queue loop is *actually* configured to do, read off the box.

    This module ships alongside a `queue_runner` that can post whenever a video
    is free; the loop on this machine may be running an older one that only
    fills a fixed grid, and the dashboard has no business describing the code it
    was built with instead of the code that is running. Two readings settle it:

    * the loop's own unit, for the ``--slots`` it is started with;
    * the loop's own log, for whether it has ever mentioned per-model times --
      a line only the newer runner can write.

    Getting this wrong is not cosmetic. On 2026-08-06 the page reported 59
    profiles "could post now" against a two-hour gap the running loop does not
    implement, while the real answer was "at 18:00, like every day".
    """
    out = {"slots": [], "unit": schedule_spec.unit_name("queue", "service"),
           "per_model": False, "known": False}
    try:
        shown = subprocess.run(["systemctl", "show", out["unit"], "--property=ExecStart"],
                               capture_output=True, text=True, timeout=10).stdout
    except Exception:
        shown = ""
    match = re.search(r"--slots[= ]([0-9:,]+)", shown)
    if match:
        out["slots"] = [s for s in match.group(1).split(",") if s.strip()]
        out["known"] = True

    path = Path(log_path) if log_path else (_repo_root() / LOG_DIR / "loop_queue.log")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    # Only the newer runner writes either sentence; neither means the loop
    # predates per-model times entirely, whatever this module can do.
    if "per-model reel times for" in text:
        out["per_model"], out["known"] = True, True
    elif "no per-model reel times" in text:
        out["known"] = True
    return out


#: Why the posting loop will refuse a due row before it ever looks at the clock.
#: The wording is the reader's, not the planner's -- `posting_planner` says
#: "profile needs a human check", which is not what a person on the schedules
#: tab is asking.
BLOCKED_FLAGGED = "held — the profile is flagged, waiting on a person"
BLOCKED_PARKED = "held — the profile is parked (Status Inactive)"
#: Retired: the phone itself is gone from MultiLogin. Its own wording because
#: the remedy is not the same -- a flag can be cleared and a park can be
#: un-parked, while these rows can only ever be cancelled.
BLOCKED_RETIRED = "held — its MultiLogin profile no longer exists"


def profiles_blocked_from_posting(profiles) -> dict:
    """``{profile name: why the posting loop will not take its rows}``.

    `posting_planner` refuses a due row for a flagged or parked profile *before*
    it looks at the clock, so a queue row belonging to one is not "going out on
    the next posting tick" and never was -- it is frozen until somebody clears
    the flag. Without this the outlook counted the whole backlog as imminent:
    425 rows described as due when the loop skips most of them every tick, which
    is the same reading that had the fleet's capacity blamed on concurrency.

    Keyed by profile name because that is all a queue row carries: its Name is
    "<profile> / <slot>" and resolving the link would be a read per row.
    """
    out: dict = {}
    for profile in profiles or []:
        name = str(profile.get("name") or "").strip()
        if not name:
            continue
        # Retired first: it outranks both of the others, because a flagged or
        # parked phone can come back and a deleted one cannot.
        if profile_retired(profile):
            out[name] = BLOCKED_RETIRED
        elif profile.get("needs_human"):
            out[name] = BLOCKED_FLAGGED
        elif str(profile.get("status") or "") == "Inactive":
            out[name] = BLOCKED_PARKED
    return out


def _blocked_reason(blocked, who: str) -> str:
    """`blocked[who]`, allowing for the second-account spelling of a row's name.

    A two-account phone writes its rows as "Nikki 12 (kikittie22)" while the
    Profiles row is plain "Nikki 12". Matching only the literal string let every
    second-account row past the gate.
    """
    if not blocked or not who:
        return ""
    if who in blocked:
        return blocked[who]
    if who.endswith(")") and "(" in who:
        return blocked.get(who[:who.rindex("(")].strip(), "")
    return ""


def posting_outlook(queue_rows, schedules=None, now=None, grid=None, blocked=None) -> dict:
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

    `blocked` (see `profiles_blocked_from_posting`) names the profiles the loop
    refuses outright. Their rows still exist and still carry a due time, so the
    arithmetic here would call them imminent; they are counted apart instead,
    because "due" and "going out" are only the same sentence for a profile
    nobody has flagged.
    """
    from adb_bot.automation import queue_runner
    from adb_bot.clients import airtable as at

    out = {"queued": [], "profiles": [], "gap_minutes": queue_runner.DEFAULT_ANYTIME_GAP_MINUTES,
           "default_cap": queue_runner.DEFAULT_ANYTIME_MAX_PER_DAY, "timezone": "",
           "eligible_now": 0, "waiting": 0, "capped": 0, "any_fixed": False,
           # Rows and profiles the posting loop refuses on a flag, not a clock.
           "blocked": 0, "queued_blocked": 0,
           "mode": "flexible", "slots": [], "next_slot": "", "next_slot_seconds": 0.0,
           # Slot times are the audience's wall clock; every other timestamp on
           # this page is the server's, and this box runs UTC while the slots are
           # Berlin. A bare "next slot 20:00" next to a UTC "generated 16:09"
           # reads as a four-hour wait or a broken clock, so the tile has to say
           # which clock it is in. Asked live 2026-08-06.
           "server_timezone": "", "same_clock": True, "clock_gap_hours": 0.0,
           "unit": ""}

    tz = queue_runner._zone(queue_runner.DEFAULT_TIMEZONE)
    out["timezone"] = queue_runner.DEFAULT_TIMEZONE
    now = now or datetime.now(tz)
    # `.astimezone(tz)` and not `.replace(tzinfo=tz)`: `collect` hands down a
    # naive `datetime.now()`, which is the *server's* wall clock, and stamping
    # Berlin onto a UTC reading moves every time on this tab by two hours.
    # astimezone reads a naive value as system-local and converts it properly.
    local_now = now.astimezone(tz)

    # Offsets, not names: "CEST" and "Europe/Berlin" are the same clock spelled
    # two ways, and comparing the spellings would cry wolf every summer.
    server = local_now.astimezone(datetime.now().astimezone().tzinfo)
    out["server_timezone"] = server.tzname() or ""
    out["same_clock"] = server.utcoffset() == local_now.utcoffset()
    out["clock_gap_hours"] = round(
        ((local_now.utcoffset() or timedelta()) - (server.utcoffset() or timedelta()))
        .total_seconds() / 3600.0, 2)

    gap = timedelta(minutes=out["gap_minutes"])

    # What the loop on this box actually does, not what this module can do.
    grid = queue_grid() if grid is None else grid
    out["unit"] = grid.get("unit", "")
    out["slots"] = list(grid.get("slots") or [])
    if out["slots"] and not grid.get("per_model"):
        out["mode"] = "grid"
        out["next_slot"] = _next_slot(out["slots"], local_now)
        out["next_slot_seconds"] = _seconds_to_slot(out["slots"], local_now)

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
            reason = _blocked_reason(blocked, who)
            if reason:
                out["queued_blocked"] += 1
            out["queued"].append({
                "name": name, "profile": who,
                "when": when.strftime("%H:%M"),
                "day": when.strftime("%Y-%m-%d"),
                "due": when <= local_now,
                "seconds": max(0.0, (when - local_now).total_seconds()),
                "blocked": reason,
            })
    out["queued"].sort(key=lambda row: (row["day"], row["when"]))

    for who, when in last.items():
        model = who.split()[0].lower() if who.split() else ""
        cap = caps.get(model, out["default_cap"])
        posted = today_count.get(who, 0)
        nxt = when + gap
        reason = _blocked_reason(blocked, who)
        if reason:
            # Ahead of the clock rules on purpose: the flag is checked first by
            # the planner too, and calling a flagged profile "could post now"
            # is the single most misread number this tab ever carried.
            state = "blocked"
        elif cap and posted >= cap:
            state = "capped"
        elif nxt <= local_now:
            state = "ready"
        else:
            state = "waiting"
        out[{"capped": "capped", "ready": "eligible_now", "waiting": "waiting",
             "blocked": "blocked"}[state]] += 1
        out["profiles"].append({
            "profile": who, "model": who.split()[0] if who.split() else who,
            "last": when.strftime("%H:%M"), "last_day": when.strftime("%Y-%m-%d"),
            # The gap runs from the latest row's *scheduled* time, which is how
            # the queue loop reads it too -- so a row queued for later today
            # pushes the next one out from there, and the anchor is a time that
            # has not happened yet. Calling that "last post" would be a lie.
            "ahead": when > local_now,
            "next": "now" if state == "ready" else nxt.strftime("%H:%M"),
            "seconds": 0.0 if state == "ready" else max(0.0, (nxt - local_now).total_seconds()),
            "today": posted, "cap": cap, "state": state, "blocked": reason,
        })
    # Soonest first, and a profile that could post this second before one that
    # cannot: the top of this list is what the next posting tick will consider.
    # A blocked profile sorts last whatever its clock says -- its next possible
    # time is not a time, and letting it sort by seconds put profiles nothing
    # will launch at the head of a list read as "what happens next".
    out["profiles"].sort(key=lambda row: (row["state"] == "blocked",
                                          row["state"] == "capped",
                                          row["seconds"], row["profile"]))
    return out


def _schedules_for_outlook(airtable):
    """The per-model schedules `posting_outlook` needs for each model's daily cap,
    off the same cached read the schedule table uses."""
    return _slow("model_inputs", lambda: _model_inputs(airtable))["schedules"]


def _seconds_to_slot(times, local_now) -> float:
    """Seconds until the next of `times`, wrapping to tomorrow once all have gone."""
    from adb_bot.automation.queue_runner import parse_slot_times

    slots = parse_slot_times(times)
    if not slots:
        return 0.0
    for slot in slots:
        moment = local_now.replace(hour=slot.hour, minute=slot.minute,
                                   second=0, microsecond=0)
        if moment > local_now:
            return (moment - local_now).total_seconds()
    first = local_now.replace(hour=slots[0].hour, minute=slots[0].minute,
                              second=0, microsecond=0) + timedelta(days=1)
    return (first - local_now).total_seconds()


# The same parse the completion rule uses, under the name a dozen callers in
# this module already reach for. Two copies of it is how the page and the rule
# that retires a profile end up disagreeing about a timestamp, which is exactly
# the class of drift `warmup_completion` exists to end.
_parse_airtable_dt = warmup_completion.parse_run_at


def _aliased_folder(model_key: str) -> str:
    """The raw folder whose contents belong to `model_key`, when it is not named
    after that model. "" when the folder and the model agree."""
    from adb_bot.automation.spoof_pipeline import raw_folder_model_aliases

    for folder, model in raw_folder_model_aliases().items():
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


#: Why an account is not warming up, worst first. The order matters: the page
#: shows one reason per account and it must be the one a person acts on, which
#: is the *first* gate the planner hits, not the last thing that happens to be
#: wrong. Mirrors the guard order in `airtable_planner.plan_from_airtable`.
WARMUP_BLOCKERS = ("needs human verification", "automation mode paused",
                   "lifecycle stage Paused", "lifecycle stage Banned",
                   "no MLX API ID on linked profile", "no creation date")


def _warmup_targets_profiles() -> bool:
    """Whether the scheduled warm-up actually targets the tagged profiles.

    `run_loop warmup` defaults to `--targets accounts`, and on this fleet the
    Accounts table is 11 paused rows. Without the flag the loop fires hourly,
    plans nothing, and exits 0 -- so every watchdog and every timer reads as
    healthy while 45 profiles are never touched. That is the failure this page
    exists to catch, and it is invisible in every other section.
    """
    try:
        out = subprocess.run(
            ["systemctl", "show", schedule_spec.unit_name("warmup", "service"),
             "--property=ExecStart"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return True                    # unreadable: do not cry wolf
    return "--targets" in out and "profiles" in out


def mlx_inventory() -> list:
    """The MultiLogin mobile-profile list, or [] if it cannot be read.

    The only source for the `Created` tag the warm-up population is defined by.
    Two paged HTTP calls and a token, so it is memoised by the caller rather
    than made on every render, and a failure is an empty list -- the warm-up
    table then says it could not read MultiLogin, and the rest of the page is
    untouched.
    """
    import contextlib
    import io

    try:
        from adb_bot.automation.run_loop import _mlx_token
        from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient

        token = _mlx_token(None)
        if not token:
            return []
        # The client narrates its HTTP calls to stdout; on the report server
        # that is the journal, once every three minutes, saying nothing anyone
        # reads it for.
        with contextlib.redirect_stdout(io.StringIO()):
            return MultiloginMobileListClient(token).list_mobile_profiles() or []
    except Exception:
        return []


def mlx_folders() -> dict:
    """``{folder_id: folder name}`` for the mobile workspace, or {} on failure.

    The folder is how MultiLogin groups phones by model, and it is the grouping
    the client's own UI shows -- so it is the one the Profiles tab counts by.
    `serial_name` cannot stand in: 46 of these phones are called "Blank (NN)"
    and belong to no model at all by name, while sitting in a model's folder.

    Memoised by the caller like `mlx_inventory`: one HTTP call, and folders
    change when somebody makes one.
    """
    import contextlib
    import io

    try:
        from adb_bot.automation.run_loop import _mlx_token
        from adb_bot.clients.multilogin.folders import MultiloginFolderClient

        token = _mlx_token(None)
        if not token:
            return {}
        with contextlib.redirect_stdout(io.StringIO()):
            folders = MultiloginFolderClient(token).list_mobile_folders() or []
        return {str(f.get("folder_id")): str(f.get("name") or "").strip()
                for f in folders if f.get("folder_id")}
    except Exception:
        return {}


def warmup_progress(airtable, mlx_items=None, timers=None, now=None) -> dict:
    """Every profile on warm-up: which day, how its last run went, when next.

    A different question from `warmup_status`, which asks whether a profile
    *could* run. This one is the campaign: 45 profiles moving through a
    multi-day plan an hour at a time, where the thing you need to see is a
    profile that has stopped moving.

    The population is the MLX `Created` tag, which is why this reaches
    MultiLogin -- the tag exists nowhere else, and an Airtable-only reading
    would show the handful of profiles already started and none of the ones
    waiting. That call is memoised: the tag changes when a person edits it, not
    every five minutes.

    Progress comes from the Run Log, one row per flow run. `Warm-up Started`
    gives the day; the Run Log gives whether each day's run actually landed,
    which are different facts -- a profile advances a day at midnight whether
    or not last night's run worked, so day number alone will happily report a
    profile as "day 4" having never completed a single run.

    Which is why "finished" is `warmup_completion`'s answer and not this
    module's. Calling a profile finished because the calendar ran past the plan
    is what left 41 phones counted `finished` here while the hand-off list that
    asks a person for their bio and picture showed nobody and the planner had
    already retired them. A profile out of plan days that has *not* completed
    them is `stalled` now -- a named state at the top of the table, rather than
    a green count nothing acts on.
    """
    from adb_bot.automation import lifecycle, warmup_targets
    from adb_bot.clients import airtable as at

    now = now or datetime.now()
    out = {"profiles": [], "plan_days": 0, "finish_day": 0, "plan_warning": "",
           "next_run": "", "last_run": "",
           "timer_stopped": True, "account_driven": False, "error": "",
           "counts": {"ok": 0, "failed": 0, "running": 0, "never": 0,
                      "stalled": 0, "finished": 0}}

    # The scheduled loop's own clock, not a recomputation of it: "when does this
    # next run" is a systemd question, and the timer table already answered it.
    for row in timers or []:
        if row.get("loop") == "warmup":
            out.update(next_run=row.get("next") or "", last_run=row.get("last") or "",
                       timer_stopped=bool(row.get("stopped")))

    try:
        rows = airtable.warmup_profiles_by_serial()
        if rows is None:
            out["error"] = ("Profiles (Cloning) has no 'Warm-up Started' field — "
                            "the profile warm-up counts its days from there.")
            return out
        plan_by_day = airtable.warmup_plan_by_day() or {}
        log = airtable.warmup_run_log()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    # Two different questions, and conflating them is what put 41 phones in
    # limbo. `plan_days` is how long the plan is, for display ("day 3 of 4").
    # `finish_day` is the last day that actually has to be *completed* -- a plan
    # whose final day asks only for a reel (which `warmup_targets` strips, since
    # reels belong to the Posting Queue) finishes on the day before it.
    out["plan_days"] = max(plan_by_day) if plan_by_day else lifecycle.WARMUP_DAYS
    out["finish_day"] = warmup_completion.finish_day(plan_by_day)
    if not plan_by_day:
        # A plan of unknown length must not retire anybody: falling back to the
        # built-in length here would call profiles finished against a schedule
        # nobody chose. `finish_day` is 0, so nothing is; this says why.
        out["plan_warning"] = ("The Warmup Plan table could not be read, so no "
                               "profile is being called finished.")
    targets, _skipped = warmup_targets.collect_warmup_targets(
        mlx_items or [], rows, today=now.date())

    # Run Log history, keyed the way the runner writes it. A legacy row carries
    # only the name and cannot be pinned to one twin, so it is kept under the
    # bare name and used only when the serial-qualified history is empty --
    # showing it against every twin would invent runs none of them made.
    by_serial, by_name = warmup_completion.history_by_key(log)

    # Only a name two profiles share is ambiguous. Legacy history under a name
    # that is unique in the tagged set belongs to exactly one profile, and
    # captioning it "this may be your twin's" would be a warning about a twin
    # that does not exist.
    shared = {name for name, count in
              Counter(t.name for t in targets).items() if count > 1}

    for target in targets:
        history = by_serial.get(target.serial_no) or []
        legacy = by_name.get(target.name) or []
        ambiguous = not history and bool(legacy) and target.name in shared
        if not history:
            history = legacy

        # "How is this profile's warm-up going" is a question about the warm-up
        # *activity*, not about every row the runner has ever written for it.
        # The plan asks for a picture on day 2 and a bio on day 3, and neither
        # can run without a photo or a bio somebody has to supply -- the base
        # holds 52 `update_profile_picture` rows and not one is Done. Reading
        # `last` off the whole history would therefore show every healthy
        # profile as "last run failed" on its day 2 and day 3, and publish
        # `Warm-up Last Result = Skipped` for a profile whose scroll went fine.
        # Those rows are what the hand-off tab is for; they are not this pill.
        activity = [h for h in history
                    if not h.get("flow")
                    or h["flow"] in warmup_completion.WARMUP_ACTIVITY_FLOWS]
        last = activity[0] if activity else None
        done = sum(1 for h in activity if h["result"] == at.RESULT_DONE)

        # How much of the plan this profile has actually *completed*, which is
        # what the MLX "Warmup Day N Done" tags mean and is not the same number
        # as `day`. A profile advances a day at midnight whether or not the
        # night's run worked, so a phone that has failed since day 1 reads
        # "day 4, day 1 done" -- and that gap is the whole signal.
        started = warmup_targets._parse_date(target.started)
        day_done = warmup_completion.day_done(history, started, plan_by_day)

        finished = warmup_completion.is_finished(day_done, out["finish_day"])
        # Past the end of the plan without having completed it. This is the
        # state that had no name: 41 phones sat here reading "finished" on this
        # tab and appearing on no worklist, because the calendar running out was
        # taken for the work being done. Nothing else will move them.
        stalled = (not finished and out["finish_day"] > 0
                   and target.day > out["finish_day"])

        if finished:
            state = "finished"
        elif stalled:
            state = "stalled"
        elif last is None:
            state = "never"
        elif last["result"] == at.RESULT_RUNNING:
            state = "running"
        elif last["result"] == at.RESULT_DONE:
            state = "ok"
        else:
            state = "failed"
        out["counts"][state] = out["counts"].get(state, 0) + 1

        out["profiles"].append({
            "name": target.name,
            "serial": target.serial_no,
            "launch_id": target.launch_id,
            # Carried so `warmup_state` can write this row back without
            # resolving the whole Profiles table a second time.
            "record_id": target.record_id,
            "day": target.day,
            "day_done": day_done,
            "started": target.started or "",
            "runs_done": done,
            "runs_logged": len(activity),
            "last_at": _local_stamp(last["at"]) if last else "",
            # The raw Airtable stamp as well as the display one: `warmup_state`
            # writes this into a dateTime field, which will not take the
            # localised "2026-08-08 08:50" the page shows.
            "last_at_iso": (last["at"] if last else ""),
            "last_result": last["result"] if last else "",
            "last_notes": (last["notes"] if last else "")[:200],
            "state": state,
            "ambiguous": ambiguous,
        })

    # Problems first, then the ones furthest through the plan: a page read at a
    # glance should open on the profile that has stopped moving. `stalled`
    # outranks even `failed` -- a failed run may well succeed tonight, whereas a
    # stalled profile is out of plan days and nothing will retry it.
    order = {"stalled": 0, "failed": 1, "never": 2, "running": 3, "ok": 4,
             "finished": 5}
    out["profiles"].sort(key=lambda p: (order.get(p["state"], 9), -p["day"],
                                        p["name"].lower(), p["serial"]))
    out["waiting"] = warmup_waiting(mlx_items or [], rows,
                                    in_warmup={t.serial_no for t in targets})
    return out


# Tags that mean a profile is somewhere other than the warm-up queue -- past it,
# or taken out of service. A profile carrying one of these is not a candidate,
# and listing all 87 of them as "waiting" would bury the handful that are.
LIVE_TAGS = ("active / posting", "ready for posting", "banned / dead")


def warmup_waiting(mlx_items, profiles_by_serial, in_warmup=None) -> list:
    """Profiles that look like warm-up candidates but are not in the population.

    New phones are cloned in batches days before anyone works through them, and
    the only thing that puts one on warm-up is a person adding the `Created`
    tag in MultiLogin. That is the right gate -- a phone tagged `gmail` has an
    email account and no Instagram, and warming it up would drive an empty app
    -- but it is an invisible one: nothing anywhere said "these 17 phones exist
    and are not being warmed up", so a batch could sit untagged indefinitely
    with every dashboard reading green.

    Each row carries the tag it does have, which is what says whose turn it is,
    and the reason column mirrors `collect_warmup_targets`' own refusals -- the
    two must agree or this list tells people to do the wrong thing.
    """
    from adb_bot.automation import warmup_targets
    from adb_bot.automation.mlx_sync import normalize_mlx_item

    in_warmup = in_warmup or set()
    waiting = []
    for item in mlx_items or []:
        profile = normalize_mlx_item(item)
        if profile is None or profile.serial_no in in_warmup:
            continue
        tags = [str(t) for t in (profile.tags or ())]
        if any(t.lower() in LIVE_TAGS for t in tags):
            continue

        # Whether the phone already carries the tag decides which half of
        # `collect_warmup_targets` refused it, and the two halves ask for
        # opposite things. Saying "not Created" to a phone that *is* tagged
        # Created -- five of them on 2026-08-16, every one of them flagged --
        # sends somebody to add a tag that is already there and hides the flag
        # that is the actual blocker.
        tagged = warmup_targets.has_tag(tags)

        row = (profiles_by_serial or {}).get(profile.serial_no)
        if row is None:
            reason = "no Profiles (Cloning) row yet — the mlx-sync loop runs at 23:30"
        elif (row.get("status") or "") == "Inactive":
            # Somebody parked it. Not waiting on anything.
            continue
        elif not row.get("api_id"):
            reason = "no MLX API ID on the Airtable row — nothing can launch it"
        elif tagged and row.get("needs_human"):
            reason = ("tagged Created, but flagged in Airtable (Needs Human Check) — "
                      "warm-up skips a flagged phone; clear the flag and it joins")
        elif tagged:
            # Tagged, has a row, launchable, unflagged: nothing here refuses it,
            # so it is between ticks rather than held back. Worth a line anyway
            # -- if it says this for a day, the warm-up loop is not running.
            reason = "tagged Created — due to join on the next warm-up tick"
        elif tags:
            reason = f"tagged {', '.join(tags)}, not Created"
        else:
            reason = "no tag at all"

        waiting.append({
            "name": row.get("name") if row else profile.name,
            "serial": profile.serial_no,
            "tags": ", ".join(tags),
            "created": (profile.created_at or "")[:10],
            "reason": reason,
        })

    # Newest first: a batch cloned this week is the one somebody is working
    # through, and the phones from June are a decision already taken.
    waiting.sort(key=lambda p: (p["created"], p["name"] or ""), reverse=True)
    return waiting


def _local_stamp(value) -> str:
    """An Airtable UTC ISO stamp as the server's own wall clock, to the minute.

    Every other time on this page is the server's, and a UTC stamp sitting in a
    column next to them reads as a clock that is two hours out rather than as a
    different timezone.
    """
    stamp = _parse_airtable_dt(value)
    if stamp is None:
        return str(value or "")[:16].replace("T", " ")
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def warmup_status(airtable, now=None) -> dict:
    """Who is warming up, who cannot, and what the plan says for each day.

    Deliberately re-derives the planner's decision rather than reading a result
    the warm-up loop wrote: the loop only logs what it *skipped this tick*, so a
    reason never reaches Airtable and nothing on the box remembers it an hour
    later. Re-deriving keeps the page honest about a fleet that runs hourly.

    The gate order below is `airtable_planner`'s, and must stay that way. An
    account paused *and* undated is reported as paused, because un-pausing it is
    the first thing a person would do and the date question only exists after.
    """
    # Imported here, like every other Airtable-touching collector in this file:
    # the module is imported by the loops too, and the page is not worth making
    # them pay for at import time.
    from adb_bot.clients import airtable as at
    from adb_bot.automation import lifecycle

    def _parse_iso_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except Exception:
            return None

    now = now or datetime.now()
    today = now.date()
    out = {"plan": [], "accounts": [], "plan_days": 0, "error": "",
           "counts": {"running": 0, "blocked": 0, "finished": 0, "not_started": 0}}
    try:
        plan_by_day = airtable.warmup_plan_by_day() or {}
        accounts = airtable.list_accounts()
        profiles = airtable.profile_launch_map()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    for day in sorted(plan_by_day):
        actions, _ = lifecycle.plan_actions_from_row(day, plan_by_day[day])
        out["plan"].append({"day": day, "actions": [a.label for a in actions]})
    out["plan_days"] = max(plan_by_day) if plan_by_day else 0

    for record in accounts:
        fields = record.get("fields", {}) or {}
        name = str(fields.get(at.F_ACC_NAME) or "").strip()
        if not name and not fields.get(at.F_ACC_PROFILE):
            continue  # placeholder row, same as the planner
        links = fields.get(at.F_ACC_PROFILE) or []
        info = profiles.get(links[0]) if links else None
        launch_id = (info or {}).get("launch_id")
        start = _parse_iso_date(fields.get(at.F_ACC_CREATION_DATE))
        stage = at._select_name(fields.get(at.F_ACC_LIFECYCLE_STAGE))
        mode = at._select_name(fields.get(at.F_ACC_AUTOMATION_MODE))

        blocker = ""
        if bool(fields.get(at.F_ACC_NEEDS_VERIFICATION)):
            blocker = "needs human verification"
        elif mode == at.MODE_PAUSED:
            blocker = "automation mode paused"
        elif stage in (at.STAGE_PAUSED, at.STAGE_BANNED):
            blocker = f"lifecycle stage {stage}"
        elif not launch_id:
            blocker = "no MLX API ID on linked profile"
        elif start is None:
            blocker = "no creation date"

        day = lifecycle.day_number(start, today) if start else None
        actions: list = []
        if day is not None and plan_by_day:
            planned, _ = lifecycle.plan_actions_from_table(day, plan_by_day)
            actions = [a.label for a in planned]

        # "Finished" and "not started" are not blockers -- nothing is wrong with
        # them -- but they are the difference between "flip this switch and it
        # runs" and "flip it and nothing happens", which is the whole question
        # this tab exists to answer.
        if blocker:
            state = "blocked"
        elif day is None:
            state = "blocked"
        elif day < 1:
            state = "not_started"
        elif day > out["plan_days"]:
            state = "finished"
        else:
            state = "running"
        out["counts"][state] = out["counts"].get(state, 0) + 1

        # The trap this tab exists to catch. Clearing the blocker is necessary
        # and not sufficient: day 1 is Creation Date, so an account paused since
        # June is on day 50 of a 4-day plan, and un-pausing it runs *nothing*
        # while looking exactly like success. Whoever flips the switch has to
        # know the date needs moving too, before they flip it.
        stale_date = bool(blocker) and day is not None and day > out["plan_days"]

        out["accounts"].append({
            "name": name or record.get("id"),
            "profile": (info or {}).get("name") or "",
            "stage": stage or "",
            "mode": mode or "",
            "created": str(fields.get(at.F_ACC_CREATION_DATE) or "")[:10],
            "day": day,
            "state": state,
            "blocker": blocker,
            "stale_date": stale_date,
            "actions": actions,
        })

    out["accounts"].sort(key=lambda a: (a["state"] != "running", a["name"].lower()))
    return out


def geelark_status() -> dict:
    """The Geelark account: phones, what is running, tags, proxies, ADB.

    Deliberately separate from every MultiLogin reading on this page. Geelark is
    a second cloud-phone host under evaluation, not a replacement, and merging
    the two inventories would make both unreadable -- a phone that exists in
    Geelark has no Airtable row, no model, and no posting history, so it must
    not be counted beside MLX phones that do.

    Never raises: an unconfigured or unreachable Geelark costs this tab and
    nothing else. Anything that goes wrong is reported in `error`.

    Unlike MultiLogin, Geelark *does* report money: `/pay/wallet` and
    `/pay/plan/info` give the balance, the parallel-slot count and the profile
    allowance. That matters because running out of MultiLogin minutes stops
    every launch while every log blames the server -- here the runway is
    readable before a run rather than diagnosed from failures afterwards. Both
    endpoints are heavily rate limited (the plan one to a single call a minute),
    which is why this whole function sits behind `_slow`.
    """
    out: dict = {
        "configured": False,
        "phones": [],
        "tags": [],
        "proxies": [],
        "billing": {},
        "counts": {"phones": 0, "running": 0, "stopped": 0,
                   "adb_enabled": 0, "proxies": 0, "gateways": 0},
        "error": "",
    }

    try:
        from adb_bot.clients.geelark import (
            GeelarkApiClient,
            GeelarkBillingClient,
            GeelarkPhoneClient,
            GeelarkProxyClient,
            GeelarkTagClient,
            GeelarkTransport,
            status_label,
        )
    except Exception as exc:  # pragma: no cover - import guard
        out["error"] = f"Geelark client unavailable: {exc}"
        return out

    transport = GeelarkTransport()
    if not transport.is_configured:
        # Not an error: the credentials are simply not installed on this host.
        out["error"] = ("Not configured -- set GEELARK_APP_ID and "
                        "GEELARK_API_KEY in /etc/adbbot/env.")
        return out
    out["configured"] = True

    try:
        rows = GeelarkPhoneClient(transport).list_phones()
    except Exception as exc:
        out["error"] = f"Could not read the Geelark phone list: {exc}"
        return out

    # One ADB read for the whole account, so the tab can say which phones are
    # actually reachable rather than merely running. ADB is off per phone by
    # default, and a phone with it off is invisible to the bot.
    adb_by_id: dict[str, str] = {}
    try:
        api = GeelarkApiClient(transport)
        raw = api.fetch_adb_credentials([str(row.get("id")) for row in rows])
        for profile in GeelarkApiClient.parse_profiles(raw):
            adb_by_id[profile.id] = profile.status
    except Exception as exc:
        out["error"] = f"Phones listed, but the ADB state could not be read: {exc}"

    for row in rows:
        equipment = row.get("equipmentInfo") or {}
        proxy = row.get("proxy") or {}
        phone_id = str(row.get("id"))
        adb_state = adb_by_id.get(phone_id, "unknown")
        out["phones"].append({
            "id": phone_id,
            "name": str(row.get("serialName") or ""),
            "status": status_label(row.get("status")),
            "adb": adb_state,
            "country": str(equipment.get("countryName") or ""),
            "os": str(equipment.get("osVersion") or ""),
            "device": " ".join(part for part in (
                str(equipment.get("deviceBrand") or ""),
                str(equipment.get("deviceModel") or "")) if part),
            "timezone": str(equipment.get("timeZone") or ""),
            "proxy": (f"{proxy.get('server')}:{proxy.get('port')}"
                      if proxy.get("server") else ""),
            "tags": [str(tag.get("name") or "") for tag in row.get("tags") or []],
            "group": str((row.get("group") or {}).get("name") or ""),
        })

    out["phones"].sort(key=lambda p: p["name"].lower())
    out["counts"]["phones"] = len(out["phones"])
    out["counts"]["running"] = sum(1 for p in out["phones"] if p["status"] == "started")
    out["counts"]["stopped"] = sum(1 for p in out["phones"] if p["status"] == "stopped")
    out["counts"]["adb_enabled"] = sum(1 for p in out["phones"] if p["adb"] == "active")

    try:
        out["tags"] = [
            {"name": str(tag.get("name") or ""), "id": str(tag.get("id") or "")}
            for tag in GeelarkTagClient(transport).list_tags()
        ]
    except Exception:
        # A tag read failing must not blank the phone table above it.
        out["tags"] = []

    try:
        proxy_client = GeelarkProxyClient(transport)
        clusters = proxy_client.endpoint_clusters()
        out["proxies"] = [
            {"endpoint": endpoint, "profiles": len(members)}
            for endpoint, members in sorted(clusters.items())
        ]
        out["counts"]["proxies"] = sum(p["profiles"] for p in out["proxies"])
        # Gateway hosts, deliberately NOT exit IPs -- separate ports on one host
        # commonly egress from different addresses, and Geelark never reports
        # the exit address at all. Counting hosts as IPs said "one" about four.
        out["counts"]["gateways"] = len({
            endpoint.split(":")[0] for endpoint in clusters
        })
    except Exception:
        out["proxies"] = []

    try:
        out["billing"] = GeelarkBillingClient(transport).runway()
    except Exception:
        # Money is the one reading here that is rate limited hard enough to fail
        # on its own; the inventory above is still worth showing without it.
        out["billing"] = {}

    return out


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
    # `videos` is no longer collected: the "Run by run" section that displayed it
    # was taken off the page, and it was the only reader. Building it cost a log
    # scan here and a full Spoof Variants read below on every render.
    # `video_runs` and `_section_videos` are left intact and tested, so putting
    # the section back is this call and the two lines in `render`.
    posts_today = sum(r.posts for r in runs)
    run_seconds = sum(r.seconds for r in runs if r.seconds)
    attempts = sum(r.attempts for r in runs)
    mlx_500 = sum(r.mlx_500 for r in runs)

    # MultiLogin bills mobile profiles by the minute and exposes no balance
    # anywhere -- see `mlx_minutes`. Counting what was spent is the only way the
    # page can show how close the fleet is to the cliff it fell off on
    # 2026-08-18, when the minutes ran out and every launch failed for hours.
    try:
        from adb_bot.automation import mlx_minutes
        minutes = mlx_minutes.collect(now=now)
    except Exception:
        # A missing or unreadable MLX log dir must cost the page one panel, not
        # the whole render.
        minutes = None

    data = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "day": day,
        "minutes": minutes,
        "now": running_now(),
        # Local half only: which profiles are live, which loop has each and
        # when its phone came up. The reel each posting profile is sending is
        # filled in below, once the queue rows have been read.
        "live_work": live_work(now=now),
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
        # Kept as an empty list rather than dropped: hosts and tests that read
        # `data["videos"]` should see "nothing to show", not a KeyError.
        "videos": [],
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
        "daily": {"days": [], "totals": {"posted": 0, "failed": 0, "unsettled": 0,
                                         "rate": None}, "omitted": 0, "undated": 0},
        "content": {"ready": 0, "drawable": 0, "held": 0, "by_model": {}, "held_by_model": {}},
        "needs_human": {"rows": [], "retrying": [], "profiles": [], "retired": [],
                        "error": ""},
        "handoff": {"profiles": [], "done": 0, "plan_days": 0, "finish_day": 0},
        "mlx_issues": {"warmup": [], "parked": [], "other": [], "error": "",
                       "counts": {"tagged": 0, "flagged": 0, "unflagged": 0}},
        "folders": {"folders": [], "totals": {}, "known_folders": 0, "error": ""},
        # Names of profiles whose MLX phone is gone (Issue Reason =
        # Profile Deleted From MLX). Kept as a list, not a count, so the page
        # can name them on request without a second read.
        "retired": [],
        "posts_today": {"posts": [], "by_status": {}, "by_profile": [], "total": 0,
                        "clips": 0, "day": day, "reused_clips": [],
                        "parked": None, "to_post": None, "parked_reasons": {}},
        "second_accounts": {"profiles": [], "supported": True, "error": "",
                            "counts": {"phones": 0, "usable": 0, "incomplete": 0,
                                       "posted_today": 0, "expected_today": 0}},
        "schedules": {"models": [], "timezone": "", "fallback": [], "per_model": True,
                      "error": ""},
        "outlook": {"queued": [], "profiles": [], "gap_minutes": 0, "default_cap": 0,
                    "timezone": "", "eligible_now": 0, "waiting": 0, "capped": 0,
                    "any_fixed": False, "blocked": 0, "queued_blocked": 0},
        "warmup": {"plan": [], "accounts": [], "plan_days": 0, "error": "",
                   "counts": {"running": 0, "blocked": 0, "finished": 0,
                              "not_started": 0}},
        # Every key `warmup_progress` can return, because the renderer indexes
        # some of them with [] rather than .get: a key missing from this
        # fallback is a KeyError on the whole page whenever Airtable is down,
        # which is precisely when somebody is reading it.
        "warmup_progress": {"profiles": [], "plan_days": 0, "finish_day": 0,
                            "plan_warning": "", "next_run": "",
                            "last_run": "", "timer_stopped": True,
                            "account_driven": False, "error": "",
                            "counts": {"ok": 0, "failed": 0, "running": 0,
                                       "never": 0, "stalled": 0, "finished": 0}},
        # Seeded with every key `geelark_status` can return, for the same reason
        # as `warmup_progress` above: a key missing here is a KeyError on the
        # whole page exactly when Geelark is unreachable.
        "geelark": {"configured": False, "phones": [], "tags": [], "proxies": [],
                    "billing": {},
                    "counts": {"phones": 0, "running": 0, "stopped": 0,
                               "adb_enabled": 0, "proxies": 0, "gateways": 0},
                    "error": ""},
        "airtable_error": "",
    }

    # Outside the Airtable block below on purpose, and behind `_slow`: Geelark is
    # a third-party HTTP read that has nothing to do with Airtable, so neither an
    # Airtable outage nor a Geelark one may blank the other. It reports its own
    # failures, which is what `_slow` requires.
    data["geelark"] = _slow("geelark", geelark_status)

    if airtable is not None:
        # Outside the block below on purpose: this one reports its own failures
        # in place (`spoof.error`) and must not be skipped because an unrelated
        # Airtable call above it raised. The local half -- what is on the
        # encoder right now -- is already collected either way.
        data["spoof"].update(spoof_queue(airtable))
        # Its own try, outside the block below: the warm-up fleet is a different
        # set of records from the posting queue, and a posting-side Airtable
        # failure must not blank a tab that could still answer its question.
        data["warmup"] = _slow("warmup", lambda: warmup_status(airtable, now))
        # Its own try for the same reason as `warmup`: the campaign table reads
        # a table and a service the rest of the page does not touch, and a
        # posting-side failure must not blank the one tab that answers a
        # different question. `data["timers"]` is already collected above, so
        # "when does it next run" costs nothing extra.
        data["warmup_progress"] = _slow("warmup_progress", lambda: warmup_progress(
            airtable, mlx_items=_slow("mlx_inventory", mlx_inventory),
            timers=data["timers"], now=now))
        # The loop can only see the tagged profiles when it is told to target
        # them. Read from the unit rather than assumed, because the failure it
        # catches is silent: the account-driven planner finds nothing, exits 0,
        # and the watchdog sees a loop that ran.
        data["warmup_progress"]["account_driven"] = not _warmup_targets_profiles()
        try:
            # One listing serves both: the day's rows, and which variants every
            # row (of any age) has already claimed.
            rows = airtable.list_queue_rows()
            # Filled from `profile_overview` below, and empty if that read
            # fails: the outlook then reads as it did before, which is wrong but
            # no worse, rather than the schedules tab going down with it.
            blocked_profiles: dict = {}
            # Same reason as `blocked_profiles`: read off `profile_overview`
            # below, and empty if that read fails.
            second_untracked: list = []
            # Its own try, inside this one. Naming the in-flight reels is the
            # only thing on the page that needs Profiles and Spoof Variants, so
            # it is two table reads that nothing else depends on -- and out here
            # a failure in them would blank the queue, the day history, the
            # worklist and the schedules for the sake of one column. It fails
            # alone instead; `_section_live_work` says why the column is empty.
            # And only when something is actually posting: the two reads are
            # ~700 records on this base, and the answer for an idle fleet is
            # "nothing", which needs no reading at all. Most renders are idle.
            try:
                if any(r["loop"] == "posting" for r in data["live_work"]):
                    annotate_live_reels(data["live_work"], rows,
                                        profiles=airtable.profile_launch_map(),
                                        variants=airtable.variants_by_id())
            except Exception:
                pass
            data["queue"] = queue_today(airtable, day, rows=rows)
            # Same listing again: the history is every row Airtable still holds,
            # which is exactly what was just fetched for today.
            #
            # The profile map splits each day's unsettled rows into parked and
            # to-post. Its own try: this table showed a success rate for months
            # before it had the split, and one missing column on Profiles
            # (Cloning) must cost the new columns rather than the whole section
            # -- the same failure that `annotate_live_reels` caused once here.
            try:
                daily_profiles = airtable.profile_launch_map()
            except Exception:
                daily_profiles = None
            data["daily"] = daily_success(rows, profiles_by_recid=daily_profiles)
            data["content"] = content_stock(airtable, claimed=claimed_variant_ids(rows))
            data["needs_human"] = needs_human(airtable)
            # `daily_profiles` again, deliberately: it was just read for the
            # day history, and the two tabs splitting the same rows by two
            # reads of the same map is how they end up disagreeing mid-refresh.
            # It is None when that read failed, which this handles.
            data["posts_today"] = todays_posts(
                rows, day, variants_fn=airtable.variants_by_id, now=now,
                profiles_by_recid=daily_profiles)
            # Its own try. These two tabs are the only readers of
            # `profile_overview`, and they sit ahead of the schedules and the
            # outlook in this block -- so without it, one missing column on
            # Profiles (Cloning) would blank four sections that never touch it.
            # Placing a new read early and letting it take the rest down is
            # exactly how `annotate_live_reels` broke this block once already.
            try:
                # One read serving both people-facing tabs, so they cannot
                # disagree about the same profile mid-refresh.
                overview = airtable.profile_overview()
                # Read here and used by the outlook further down: the same
                # listing already says which profiles the posting loop refuses,
                # and asking twice would be a second pass over Profiles.
                #
                # Built from the FULL listing, deliberately before the retired
                # rows are dropped below. A retired phone's queue rows outlive
                # it, still carry a due time, and the outlook's arithmetic would
                # call them imminent -- so dropping the profile from this map is
                # how the dead rows would come back reading as "going out on the
                # next tick", which is the exact misreading the map exists to
                # prevent.
                blocked_profiles = profiles_blocked_from_posting(overview)
                # Phones MultiLogin no longer has come out here, once, so every
                # panel below is spared them and none can disagree about whether
                # a retired phone counts. They are reported as a number on the
                # Profiles tab rather than dropped in silence.
                overview, data["retired"] = drop_retired(overview)
                # The same memoised inventory the two panels below use, so
                # naming each phone's folder on the hand-off list costs no
                # extra call. Both readers answer {} / [] on failure rather
                # than raising, which is what keeps this safe to put first:
                # a MultiLogin outage drops one column, not the worklist.
                folder_of = folder_by_serial(
                    _slow("mlx_inventory", mlx_inventory),
                    _slow("mlx_folders", mlx_folders))
                data["handoff"] = handoff_queue(
                    overview, data["warmup_progress"], folder_of=folder_of)
                # Same listing and the same memoised inventory `folders` uses
                # below, so the hand-tagged worklist costs no extra call.
                data["mlx_issues"] = mlx_only_issues(
                    overview, mlx_items=_slow("mlx_inventory", mlx_inventory))
                # Same listing, same memoised inventory: the second-account tag
                # is read from the phones already fetched for the line above.
                second_untracked = second_account_untracked(
                    overview, mlx_items=_slow("mlx_inventory", mlx_inventory))
                data["folders"] = folder_breakdown(
                    overview, mlx_items=_slow("mlx_inventory", mlx_inventory),
                    folder_names=_slow("mlx_folders", mlx_folders),
                    warmup_progress=data["warmup_progress"])
            except Exception as exc:
                data["folders"]["error"] = f"{type(exc).__name__}: {exc}"
            # Reuses the same listing: which of a two-account phone's accounts
            # got rows today is already in it.
            data["second_accounts"] = second_accounts(airtable, rows=rows, day=day)
            data["second_accounts"]["untracked"] = second_untracked
            # After `content`: the schedule table reads its per-model stock from
            # it, and a schedule with no stock beside it is half the answer.
            data["schedules"] = model_schedules(airtable, content=data["content"], now=now)
            # Reuses the listing above rather than asking again -- and the
            # schedules it needs are the ones just read, not a second copy.
            data["outlook"] = posting_outlook(
                rows, schedules=_schedules_for_outlook(airtable), now=now,
                grid=_slow("queue_grid", queue_grid), blocked=blocked_profiles)
        except Exception as exc:
            # A dashboard that 500s because Airtable is having a moment is worse
            # than one that says so and still shows everything local.
            data["airtable_error"] = f"{type(exc).__name__}: {exc}"

    _cache.update(at=time.time(), data=data)
    return data


def invalidate_cache() -> None:
    _cache.update(at=0.0, data=None)
    _slow_cache.clear()
