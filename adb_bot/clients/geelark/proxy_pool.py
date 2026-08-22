"""Leasing Geelark's mobile proxies exclusively, one phone session at a time.

There are far fewer proxy ports than phones -- four here, against a fleet in
the hundreds -- and all four sit behind a rotation link that only *moves* the
IP, never reports it (see `ip_rotation.py`). Two problems follow from that:

* **Two phones must never run on the same port at once.** Instagram would see
  the same exit address from two different devices in the same minute, and
  rotating the port out from under a phone that is still using it hands that
  phone a stale reading of its own IP.
* **A port must not be rotated twice in quick succession.** The vendor refuses
  a second call with HTTP 400 (body ``ERROR``) if it lands within about a
  minute of the last one -- a real cooldown, not a broken link (see
  `ProxyRotator.rotate`'s docstring). Two phones starting a few seconds apart
  on the same port would trip it.

So a proxy port is a **lease**, not a bag of strings to pick from. This reuses
the exact file-lock mechanics already proven for the MLX fleet's live-phone
ceiling (`adb_bot.core.locks`: atomic ``O_CREAT|O_EXCL`` create, pid-liveness
reclaim, TTL backstop) with the port number standing in for a slot index.

Concurrency here is a *consequence* of leasing, not a separate number to keep
in sync: at most ``len(ports)`` phones can hold a lease at once. Buy a fifth
proxy and the ceiling becomes 5 with no config change anywhere. This is
deliberately **not** ``ADBBOT_MAX_LIVE_PROFILES`` -- that setting gates the
unrelated, already-live MLX fleet (posting/verification/recovery/warmup, all
loops, currently up to 20 concurrent phones) and must not be repurposed for a
migration branch that has not posted anything yet.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from adb_bot.core.locks import lock_dir

_LEASE_SUFFIX = ".proxylease"
_COOLDOWN_FILE = "geelark_proxy_rotations.json"

# Generous on purpose: covers one whole phone session (readiness measurements
# put a Geelark phone at 2h20m+ continuous, far longer than an MLX cloud
# phone's ~15 min). A crashed holder is reclaimed immediately via the
# pid-liveness check regardless of this TTL; it only backstops a holder that
# is alive and stuck.
DEFAULT_LEASE_TTL_SECONDS = 20 * 60

# The vendor's own cooldown measured at "about a minute" (a second rotation
# ~60s after the first was refused). Margin added rather than cutting it
# close, since the failure mode of waiting a few extra seconds is nothing and
# the failure mode of not waiting long enough is a 400 that burns the retry.
ROTATE_COOLDOWN_SECONDS = 65

_registry: dict[str, "ProxyLease"] = {}
_registry_lock = threading.Lock()


@dataclass(frozen=True)
class ProxyLease:
    """One held proxy port. `token` proves ownership, so a lease that has
    since been reclaimed and re-taken by someone else is never released by us."""

    port: int
    path: Path
    token: str
    owner: str = ""


def _pool_dir() -> Path:
    path = lock_dir() / "geelark_proxies"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lease_path(directory: Path, port: int) -> Path:
    return directory / f"port_{port}{_LEASE_SUFFIX}"


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


def _read_lease_pid(path: Path):
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


def _is_stale(path: Path, ttl_seconds: int) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > ttl_seconds


def _reclaimable(path: Path, ttl_seconds: int) -> bool:
    pid = _read_lease_pid(path)
    if pid is not None and not _pid_alive(pid):
        return True                              # holder was killed: take it back
    return _is_stale(path, ttl_seconds)


def _claim(directory: Path, port: int, token: str, owner: str, ttl_seconds: int):
    path = _lease_path(directory, port)
    payload = (f"pid={os.getpid()} token={token} owner={owner} "
               f"at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _reclaimable(path, ttl_seconds):
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
    lease = ProxyLease(port=port, path=path, token=token, owner=owner)
    with _registry_lock:
        _registry[str(path)] = lease
    return lease


def acquire_proxy(ports: list[int], owner: str = "",
                   ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
                   wait_seconds: float = 0.0, poll_seconds: float = 1.0):
    """Lease one free port from `ports`, or return None.

    None means every configured proxy is in use right now -- the caller must
    NOT start a phone on any of them this cycle, exactly like
    `locks.acquire_slot` returning None for the MLX fleet. `wait_seconds`
    polls for up to that long first; it is always bounded, so no caller can
    wait forever.

    Fail-safe: if the lease directory cannot be used at all, this returns
    None (deny) rather than assuming a port is free.
    """
    if not ports:
        return None
    try:
        directory = _pool_dir()
    except OSError:
        return None
    token = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    while True:
        for port in ports:
            lease = _claim(directory, port, token, owner, ttl_seconds)
            if lease is not None:
                return lease
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.05, float(poll_seconds or 0.05)))


def release_proxy(lease) -> None:
    """Give a port back. Safe to call twice, and safe on a lease that has
    since been reclaimed by someone else -- the token check means this only
    ever deletes a file it still owns."""
    if lease is None:
        return
    with _registry_lock:
        _registry.pop(str(lease.path), None)
    try:
        if f"token={lease.token}" not in lease.path.read_text():
            return                                # reclaimed since: not ours
        lease.path.unlink()
    except OSError:
        pass


def release_all_proxies() -> int:
    """Release every lease this process holds; returns how many were freed.

    Never raises, so it is safe from a signal handler or atexit, matching
    `locks.release_all_slots`.
    """
    with _registry_lock:
        leases = list(_registry.values())
        _registry.clear()
    freed = 0
    for lease in leases:
        try:
            if f"token={lease.token}" in lease.path.read_text():
                lease.path.unlink()
                freed += 1
        except OSError:
            pass
    return freed


def held_ports() -> list[int]:
    """Ports this process currently holds (diagnostics/tests)."""
    with _registry_lock:
        return [lease.port for lease in _registry.values()]


# --- Rotation cooldown ---------------------------------------------------
#
# Leasing a port stops two phones from using it *concurrently*; it says
# nothing about two phones starting back-to-back on the same port a few
# seconds apart, which still trips the vendor's own cooldown. So the last
# successful rotation time is tracked per port here, in one small JSON file --
# a whole lock-directory per timestamp would be overkill for four numbers
# that change at most a few times an hour.

def _cooldown_path() -> Path:
    return _pool_dir() / _COOLDOWN_FILE


def _read_cooldowns() -> dict[str, float]:
    try:
        return json.loads(_cooldown_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cooldowns(data: dict[str, float]) -> None:
    path = _cooldown_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass                                      # best-effort: vendor 400 is the backstop


def record_rotation(port: int, when: float | None = None) -> None:
    """Note that `port` was just rotated, for `seconds_until_rotatable`."""
    data = _read_cooldowns()
    data[str(port)] = when if when is not None else time.time()
    _write_cooldowns(data)


def seconds_until_rotatable(port: int,
                             cooldown_seconds: int = ROTATE_COOLDOWN_SECONDS) -> float:
    """How much longer `port` must wait before rotating again, 0 if free now.

    Never raises: a missing or corrupt cooldown file reads as "never
    rotated", i.e. free to rotate -- the vendor's own 400/ERROR response is
    still the backstop if this ever under-counts.
    """
    last = _read_cooldowns().get(str(port))
    if last is None:
        return 0.0
    remaining = cooldown_seconds - (time.time() - float(last))
    return max(0.0, remaining)


def wait_for_cooldown(port: int, cooldown_seconds: int = ROTATE_COOLDOWN_SECONDS,
                       poll_seconds: float = 1.0) -> float:
    """Block until `port`'s rotation cooldown has cleared. Returns seconds waited."""
    waited = 0.0
    remaining = seconds_until_rotatable(port, cooldown_seconds)
    while remaining > 0:
        sleep_for = min(poll_seconds, remaining)
        time.sleep(sleep_for)
        waited += sleep_for
        remaining = seconds_until_rotatable(port, cooldown_seconds)
    return waited


def prepare_proxy_for_session(rotator, owner: str = "",
                               wait_for_lease_seconds: float = 60.0,
                               cooldown_seconds: int = ROTATE_COOLDOWN_SECONDS):
    """The full per-session sequence, composing all three pieces:

    1. lease a free port (never share one between two live phones),
    2. wait out that port's rotation cooldown if it was just used,
    3. rotate it and actively verify the new exit IP took
       (`ProxyRotator.rotate_and_verify` -- polls, does not sleep blindly).

    Returns `(lease, rotation)` on success. Returns `(None, None)` if every
    proxy is in use -- the caller must not start a phone this cycle. The
    caller owns the lease until the phone session ends and must call
    `release_proxy(lease)` then; holding it for the session's whole duration
    is what stops a second phone from being handed the same port meanwhile.
    """
    ports = rotator.rotatable_ports()
    lease = acquire_proxy(ports, owner=owner, wait_seconds=wait_for_lease_seconds)
    if lease is None:
        return None, None
    wait_for_cooldown(lease.port, cooldown_seconds)
    rotation = rotator.rotate_and_verify(lease.port)
    record_rotation(lease.port)
    return lease, rotation
