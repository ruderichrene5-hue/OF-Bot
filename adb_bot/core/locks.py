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
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

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
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release_all()
        return False
