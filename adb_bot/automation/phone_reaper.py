"""Find and close phones nothing owns any more.

A leaked phone is one MultiLogin still has open for a profile no loop is
working on. They accumulate whenever a run dies in a way the close guarantee
cannot cover -- `systemctl stop`, an OOM kill, a power cut -- and nothing else
in the system will ever reap them: the workflow that would have closed them is
gone, and the profile lock has long since expired.

They matter for two reasons, and RAM is the lesser one. A stale phone stays
*running in MultiLogin's cloud*, which counts against the plan's phone limit and
keeps a session alive on an Instagram account for hours with nobody driving it.

**How a phone is identified.** Each `phone_launcher_linux_amd64` process carries
its profile id in argv as ``-p <profile_id>``. That is what makes a clean
shutdown possible -- ask MultiLogin to stop that profile, rather than killing a
process and leaving its cloud phone running.

The profile's *name* is a separate question, and the launcher does not answer it
with a flag. It once passed ``-n <profile name>``; the launcher this box runs
does not, and the name reaches the process only inside the ``-u`` console URL, as
its ``envName`` query parameter. Reading the name from ``-n`` alone therefore
left every live phone anonymous -- which is only cosmetic here in the reaper's
log line, but is what the dashboard's "Live right now" table shows as the
Profile column, so it filled with the word "unknown". Both spellings are read,
newest launcher first, so this keeps working whichever one is installed.

**What counts as an orphan.** Age over `DEFAULT_MIN_AGE_SECONDS` *and* no
profile lock held for it. Both conditions, deliberately:

- The lock is the authoritative "a loop is using this right now" signal, but a
  loop that died leaves no lock, so the lock alone would reap a phone the moment
  a run crashed mid-workflow -- while the phone might still be finishing an
  upload.
- Age alone would eventually reap a legitimately slow workflow.

The default age is the profile lock's own TTL (45 min). Nothing legitimate holds
a phone that long: the longest real post measured on this box is under 10
minutes.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

from adb_bot.core import locks

PHONE_PROCESS = "phone_launcher_linux"

# A phone younger than this is never touched, however it looks. Matches the
# profile-lock TTL so the two cannot disagree about what "abandoned" means.
DEFAULT_MIN_AGE_SECONDS = locks.DEFAULT_TTL_SECONDS

# After asking MultiLogin to shut a profile down, how long to let it exit before
# falling back to signalling the process.
SHUTDOWN_GRACE_SECONDS = 20.0

_PROFILE_ARG = re.compile(r"\x00-p\x00(?P<id>\d+)\x00")
_NAME_ARG = re.compile(r"\x00-n\x00(?P<name>[^\x00]+)\x00")
# The name as the current launcher carries it: a query parameter of the `-u`
# console URL. Stopping at `&` and at the NUL that ends the argument matters --
# `envName` is not the last parameter, and a name is allowed to contain spaces
# ("Kathi 9"), which is exactly what a greedier pattern would swallow the rest
# of the URL on.
_ENV_NAME_ARG = re.compile(r"[?&]envName=(?P<name>[^&\x00]*)")


def _name_from_cmdline(cmdline: str) -> str:
    """The profile's display name, from whichever place the launcher put it."""
    match = _NAME_ARG.search(cmdline)
    if match:
        return match.group("name").strip()
    match = _ENV_NAME_ARG.search(cmdline)
    if not match:
        return ""
    # Percent-decoded, not plus-decoded: this launcher writes the name into the
    # URL raw -- spaces arrive as spaces -- so treating `+` as a space would
    # corrupt any name that genuinely contains one.
    return unquote(match.group("name")).strip()


@dataclass
class Phone:
    pid: int
    profile_id: str = ""
    name: str = ""
    age_seconds: float = 0.0
    rss_mb: float = 0.0

    @property
    def label(self) -> str:
        return self.name or self.profile_id or f"pid {self.pid}"


@dataclass
class ReapReport:
    scanned: int = 0
    orphans: list = field(default_factory=list)      # [Phone]
    shut_down: list = field(default_factory=list)    # [Phone] closed via the API
    killed: list = field(default_factory=list)       # [Phone] signalled as fallback
    survived: list = field(default_factory=list)     # [Phone] still there afterwards
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        return (f"scanned={self.scanned} orphans={len(self.orphans)} "
                f"shutdown={len(self.shut_down)} killed={len(self.killed)} "
                f"survived={len(self.survived)} errors={len(self.errors)}")


def _read_cmdline(pid: int) -> str:
    with open(f"/proc/{pid}/cmdline", "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _process_age(pid: int) -> float:
    """Seconds since the process started, from its own stat file."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().decode("utf-8", "replace").rsplit(") ", 1)[1].split()
        starttime_ticks = int(fields[19])                 # field 22, 1-indexed
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
        return max(0.0, uptime - starttime_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return 0.0


def _rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        return 0.0


def list_phones() -> list:
    """Every live phone process, with the profile it belongs to."""
    try:
        out = subprocess.run(["pgrep", "-f", PHONE_PROCESS],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []

    phones = []
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        try:
            cmdline = _read_cmdline(pid)
        except OSError:
            continue                                   # exited while we looked
        profile = _PROFILE_ARG.search(cmdline)
        phones.append(Phone(
            pid=pid,
            profile_id=profile.group("id") if profile else "",
            name=_name_from_cmdline(cmdline),
            age_seconds=_process_age(pid),
            rss_mb=_rss_mb(pid),
        ))
    return phones


def find_orphans(phones=None, min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
                 ttl_seconds: int = locks.DEFAULT_TTL_SECONDS) -> list:
    """Phones old enough to be abandoned that no loop holds a lock for."""
    orphans = []
    for phone in (phones if phones is not None else list_phones()):
        if phone.age_seconds < min_age_seconds:
            continue
        # A phone whose profile we cannot identify still counts once it is old
        # enough -- an unidentifiable phone is exactly what a broken launch
        # leaves behind, and it is the case most likely to be missed.
        if phone.profile_id:
            try:
                if locks.is_locked(phone.profile_id, ttl_seconds=ttl_seconds):
                    continue
            except Exception:
                continue                               # unsure: leave it alone
        orphans.append(phone)
    return orphans


def _still_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap(shutdown_client=None, logger=None, dry_run: bool = True,
         min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
         grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> ReapReport:
    """Close every orphaned phone. Clean shutdown first, signal as a fallback."""
    report = ReapReport()
    phones = list_phones()
    report.scanned = len(phones)
    report.orphans = find_orphans(phones, min_age_seconds=min_age_seconds)

    def log(level, message, *args):
        if logger:
            getattr(logger, level)(message, *args)

    if not report.orphans:
        log("info", "reaper: %d phone(s) live, none orphaned", report.scanned)
        return report

    for phone in report.orphans:
        log("info", "reaper: orphan %s (pid %d, age %.0f min, %.0f MB)",
            phone.label, phone.pid, phone.age_seconds / 60, phone.rss_mb)

    if dry_run:
        log("info", "reaper: [DRY-RUN] would close %d phone(s)", len(report.orphans))
        return report

    # One API call for all of them: MultiLogin takes a list, and asking once is
    # both faster and less likely to trip their rate limits than N calls.
    identified = [p for p in report.orphans if p.profile_id]
    if identified and shutdown_client is not None:
        try:
            shutdown_client.shutdown_profiles([p.profile_id for p in identified])
            time.sleep(grace_seconds)
            for phone in identified:
                if not _still_alive(phone.pid):
                    report.shut_down.append(phone)
        except Exception as exc:
            report.errors.append(f"shutdown API: {type(exc).__name__}: {exc}")
            log("warning", "reaper: shutdown API failed (%s); falling back to signals", exc)

    # Anything the API did not close -- unidentifiable, or it ignored us.
    closed = {p.pid for p in report.shut_down}
    for phone in report.orphans:
        if phone.pid in closed or not _still_alive(phone.pid):
            continue
        try:
            os.kill(phone.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as exc:
            report.errors.append(f"pid {phone.pid}: {type(exc).__name__}: {exc}")
            continue
        deadline = time.time() + 10
        while time.time() < deadline and _still_alive(phone.pid):
            time.sleep(0.5)
        if _still_alive(phone.pid):
            try:
                os.kill(phone.pid, signal.SIGKILL)
                time.sleep(1.0)
            except Exception as exc:
                report.errors.append(f"pid {phone.pid} SIGKILL: {type(exc).__name__}: {exc}")
        if _still_alive(phone.pid):
            report.survived.append(phone)
            log("warning", "reaper: %s (pid %d) survived SIGKILL", phone.label, phone.pid)
        else:
            report.killed.append(phone)

    log("info", "reaper: %s", report.summary())
    return report
