"""Cross-process locks so two scheduled loops never drive the same phone at once.

Windows Task Scheduler's ``MultipleInstancesPolicy`` only stops a task from
overlapping *itself*. Nothing stops the posting loop (every ~10 min) and the
warmup loop (a few times a day) from picking the same account and both launching
its MultiLogin profile -- two ADB sessions fighting over one device, which shows
up as flaky, hard-to-diagnose failures.

Each loop therefore takes a lock per MLX profile before touching it, and skips
any profile already held by another run (it gets picked up on the next cycle).

The lock is a file created atomically with ``O_CREAT|O_EXCL`` in the app-data
directory, holding the owner's PID and timestamp. Because a crashed run can't
release its lock, locks older than `ttl_seconds` are considered stale and taken
over -- so a killed process can never wedge a profile permanently.

The same directory also carries the cross-loop ceiling on live phones (the
`slots/` subdirectory) -- see "Live-profile slots" at the bottom of this file.
"""

from __future__ import annotations

import errno
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.config.settings import (
    DEFAULT_MAX_LIVE_PROFILES,
    get_app_data_dir,
    get_saved_max_live_profiles,
)

# A profile lock outlives a normal run (launch + connect + flow + up to a 5-min
# post confirmation). Anything older than this is treated as abandoned.
DEFAULT_TTL_SECONDS = 45 * 60

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def lock_dir() -> Path:
    path = get_app_data_dir() / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lock_path(name: str) -> Path:
    return lock_dir() / f"{_SAFE_NAME.sub('_', str(name))}.lock"


def _is_stale(path: Path, ttl_seconds: int) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > ttl_seconds
    except OSError:
        return False


def acquire(name: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, owner: str = "") -> bool:
    """Take the lock for `name`. True if acquired, False if someone else holds it.

    A lock older than `ttl_seconds` is stale (its owner died) and is taken over.
    """
    path = _lock_path(name)
    payload = f"pid={os.getpid()} owner={owner} at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _is_stale(path, ttl_seconds):
            return False
        # Steal the stale lock, then retry once.
        try:
            path.unlink()
        except OSError:
            return False
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False

    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    return True


def release(name: str) -> None:
    try:
        _lock_path(name).unlink()
    except OSError:
        pass


def is_locked(name: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    path = _lock_path(name)
    return path.exists() and not _is_stale(path, ttl_seconds)


@dataclass
class ProfileLocks:
    """Acquire locks for a set of profiles; use as a context manager so they are
    always released, even if the run raises.

        with ProfileLocks(owner="posting") as locks:
            usable = locks.acquire_all(launch_ids)   # skips busy ones
            ...
    """

    owner: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    held: list = field(default_factory=list)
    busy: list = field(default_factory=list)

    def acquire_all(self, names) -> list:
        """Lock every name we can; returns the ones acquired. Names held by
        another run land in `self.busy`."""
        for name in names:
            if acquire(name, self.ttl_seconds, self.owner):
                self.held.append(name)
            else:
                self.busy.append(name)
        return list(self.held)

    def release_all(self) -> None:
        for name in self.held:
            release(name)
        self.held.clear()

    def __enter__(self):
        # Also hand this holder to the process-wide shutdown register, so a
        # `systemctl stop` releases these locks instead of leaving them to their
        # 45-minute TTL -- which is what made the *next* run find every profile
        # busy and do nothing (TODO 3.2). Only holders registered by *this*
        # process are released, so stopping posting never touches the locks
        # warmup or recheck hold. Imported here to keep this module free of
        # import-time dependencies.
        from adb_bot.core import shutdown

        shutdown.register_locks(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        from adb_bot.core import shutdown

        shutdown.unregister_locks(self)
        self.release_all()
        return False


# --- Live-profile slots: the cross-loop ceiling -------------------------------
#
# `MAX_CONCURRENT_PROFILES` (batching.py) is applied by each loop on its own, so
# posting (10) + warmup (10) + recheck (1) could have 21 phones open at once with
# nothing coordinating them. On 2026-08-04 that emptied the box twice. This is
# the ceiling that actually counts across loops.
#
# Mechanism -- deliberately the same one the profile locks above already use, not
# a second invention: N numbered slot files in `locks/slots/`, each created with
# ``O_CREAT|O_EXCL``. The *number of slot names* IS the ceiling, so nothing ever
# reads-then-increments a counter and no race can produce an N+1st holder: two
# processes racing for `slot_003` mean one creates it and the other moves on to
# `slot_004`. Counting files in a directory would have exactly the TOCTOU hole
# this avoids.
#
# Self-healing, in two layers, because one OOM kill must not shrink the ceiling
# forever:
#   1. the holder's pid is written into the file, and a slot whose pid is gone is
#      reclaimed immediately -- precisely the OOM / `kill -9` case;
#   2. a slot older than `ttl_seconds` is reclaimed regardless, covering a pid
#      that is alive but wedged (and a recycled pid).
# A slot file that cannot be read, or carries no pid, is NOT reclaimed before its
# TTL: that is the half-written state of a claim in flight (between the O_EXCL
# create and the write), and stealing it would put two phones on one slot. So
# "unreadable" means "held" -- fail-safe, because the requirement is to prefer
# blocking a launch over letting unlimited phones open.
#
# Wait or skip? **Skip.** Every caller is a timer-driven loop (posting every
# 5 min, recheck every 15, warmup hourly), so a profile that cannot get a slot is
# simply not launched this tick and is picked up on the next one -- exactly what
# already happens when another loop holds its profile lock. Waiting would pin a
# worker thread, an Airtable plan and a profile lock for an unbounded time to
# achieve nothing that a tick later would not. `wait_seconds` exists for the one
# caller where silently skipping is user-hostile (the UI, where a person picked
# those profiles by hand) and is always bounded, so nothing can deadlock on it.

# Same abandonment horizon as a profile lock. It is only the backstop: the pid
# check reclaims a killed holder's slot immediately, so the TTL only has to cover
# a process that is alive and stuck -- and it must stay comfortably longer than
# one profile's work (`MAX_PROFILE_OPEN_SECONDS` is 7 min per flow, and a profile
# with several queue items runs them one after another).
SLOT_TTL_SECONDS = DEFAULT_TTL_SECONDS

_SLOT_SUFFIX = ".slot"
_slot_registry: dict = {}
_slot_registry_lock = threading.Lock()


@dataclass(frozen=True)
class Slot:
    """One held place in the global ceiling. `token` proves ownership, so a slot
    that has since been reclaimed and re-taken is never released by us."""

    index: int
    path: Path
    token: str
    owner: str = ""


def slot_dir() -> Path:
    path = lock_dir() / "slots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def max_live_profiles(requested=None) -> int:
    """The effective cross-loop ceiling: an explicit request, else the saved
    setting / ADBBOT_MAX_LIVE_PROFILES, else the default. Never below 1."""
    if requested is None:
        try:
            return max(1, get_saved_max_live_profiles())
        except Exception:                        # noqa: BLE001 - config must never deny a launch
            return DEFAULT_MAX_LIVE_PROFILES
    try:
        return max(1, int(requested))
    except (TypeError, ValueError):
        return DEFAULT_MAX_LIVE_PROFILES


def _slot_path(directory: Path, index: int) -> Path:
    return directory / f"slot_{index:03d}{_SLOT_SUFFIX}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True                              # EPERM: alive, just not ours
    return True


def _read_slot_pid(path: Path):
    """The pid recorded in a slot file, or None if it cannot be established."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for token in text.split():
        if token.startswith("pid="):
            try:
                return int(token[4:])
            except ValueError:
                return None
    return None


def _slot_is_reclaimable(path: Path, ttl_seconds: int) -> bool:
    pid = _read_slot_pid(path)
    if pid is not None and not _pid_alive(pid):
        return True          # the holder was killed (OOM, kill -9): take it back
    return _is_stale(path, ttl_seconds)


def _claim_slot(directory: Path, index: int, token: str, owner: str, ttl_seconds: int):
    path = _slot_path(directory, index)
    payload = (f"pid={os.getpid()} token={token} owner={owner} "
               f"at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _slot_is_reclaimable(path, ttl_seconds):
            return None
        try:
            path.unlink()
        except OSError:
            return None                          # someone beat us to it
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
    except OSError:
        return None
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    slot = Slot(index=index, path=path, token=token, owner=owner)
    with _slot_registry_lock:
        _slot_registry[str(path)] = slot
    _register_slots_for_shutdown()
    return slot


def acquire_slot(owner: str = "", ceiling=None, ttl_seconds: int = SLOT_TTL_SECONDS,
                 wait_seconds: float = 0.0, poll_seconds: float = 1.0):
    """Take one of the global live-phone slots, or return None.

    None means "the box is already running as many phones as it is allowed to" --
    the caller must NOT launch. `wait_seconds` polls for up to that long first; it
    is always bounded, so no caller can wait forever.

    Fail-safe: if the slot directory cannot be used at all, this returns None
    (deny) rather than assuming there is room.
    """
    try:
        directory = slot_dir()
    except OSError:
        return None
    cap = max_live_profiles(ceiling)
    token = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    while True:
        for index in range(cap):
            slot = _claim_slot(directory, index, token, owner, ttl_seconds)
            if slot is not None:
                return slot
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.05, float(poll_seconds or 0.05)))


def release_slot(slot) -> None:
    """Give a slot back. Safe to call twice, and safe on a slot that has since
    been reclaimed by someone else -- the token check means we only ever delete a
    file we still own."""
    if slot is None:
        return
    with _slot_registry_lock:
        _slot_registry.pop(str(slot.path), None)
    _release_slot_file(slot)


def _release_slot_file(slot) -> bool:
    try:
        if f"token={slot.token}" not in slot.path.read_text():
            return False                         # reclaimed since: not ours
        slot.path.unlink()
        return True
    except OSError:
        return False


def release_all_slots() -> int:
    """Release every slot this process holds; returns how many were freed.

    Never raises, so it is safe to call from a signal handler or atexit. The
    shutdown register calls this automatically (see `_register_slots_for_shutdown`),
    so a SIGTERM frees this process's slots as well as its profile locks.
    """
    with _slot_registry_lock:
        slots = list(_slot_registry.values())
        _slot_registry.clear()
    return sum(1 for slot in slots if _release_slot_file(slot))


def held_slots() -> list:
    """The slots this process currently holds (for diagnostics and tests)."""
    with _slot_registry_lock:
        return list(_slot_registry.values())


def live_profile_count(ttl_seconds: int = SLOT_TTL_SECONDS) -> int:
    """How many phones the box believes are open right now, across every loop.
    Reclaimable (dead-owner / expired) slots are not counted."""
    try:
        paths = list(slot_dir().glob(f"*{_SLOT_SUFFIX}"))
    except OSError:
        return 0
    return sum(1 for path in paths if not _slot_is_reclaimable(path, ttl_seconds))


class _SlotShutdownHolder:
    """Adapter that lets `core.shutdown`'s SIGTERM drain release slots without
    that module having to know they exist: it releases anything registered with
    a `release_all()`, which is exactly what this provides."""

    @property
    def held(self) -> list:
        return [f"live-slot {slot.index} ({slot.owner or 'run'})" for slot in held_slots()]

    def release_all(self) -> None:
        release_all_slots()


_SLOT_SHUTDOWN_HOLDER = _SlotShutdownHolder()


def _register_slots_for_shutdown() -> None:
    """Hand the process's slots to the shutdown register the first time one is
    taken. Imported lazily to keep this module free of import-time dependencies,
    and best-effort: failing to register must never stop a launch."""
    try:
        from adb_bot.core import shutdown

        shutdown.register_locks(_SLOT_SHUTDOWN_HOLDER)
    except Exception:                            # noqa: BLE001
        pass


@contextmanager
def live_profile_slot(owner: str = "", ceiling=None, ttl_seconds: int = SLOT_TTL_SECONDS,
                      wait_seconds: float = 0.0):
    """Hold a global slot for the lifetime of one phone.

        with live_profile_slot(owner="posting") as slot:
            if slot is None:
                return          # over the ceiling -- skip; the next tick retries
            launch(); work(); shutdown()

    Yields None rather than raising, so each loop decides what "no room" means
    for itself, and always releases on the way out (exceptions included).
    """
    slot = acquire_slot(owner=owner, ceiling=ceiling, ttl_seconds=ttl_seconds,
                        wait_seconds=wait_seconds)
    try:
        yield slot
    finally:
        release_slot(slot)
