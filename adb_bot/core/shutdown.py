"""Cleanup that survives the process being killed (``systemctl stop``, Ctrl-C).

Everything that holds a resource for the length of a run releases it on the way
out: `ProfileLocks` via its context manager, the phone via
`workflow._guarantee_profile_closed`. Both of those are ordinary Python -- a
``finally`` and a watchdog timer -- and neither runs when the process is killed
rather than finishing. So on 2026-08-04 a ``systemctl stop adbbot-posting``
left behind:

- **every profile lock the run held.** Locks only expire on their 45-minute TTL,
  so the *next* posting run found all its profiles "busy in another loop" and
  did nothing at all. That was ~17 minutes of dead time that looked exactly like
  a hang (TODO 3.2).
- **every phone that was open.** ~150 MB of WebKitWebProcess each, leaking until
  the OOM killer arrived -- the precise failure the close guarantee was written
  to prevent, minus the one exit path it cannot see (TODO 3.3).

A signal is the only notice we get, so this module keeps a process-wide register
of both, and `install_signal_handlers` drains it on SIGTERM/SIGINT.

Two decisions worth stating, because both are trade-offs:

**Locks are released before phones are closed.** Releasing a lock is an
``unlink`` on a local file: it cannot block, cannot fail slowly, and cannot be
interrupted by a network. Closing a phone is an HTTP call to MultiLogin, which
demonstrably hangs. If the order were reversed, a hanging close would spend the
whole budget and the locks -- the more expensive failure, because it wedges the
*next* run for 45 minutes -- would never be released at all. Locks first makes
3.2 unconditional and gives the phones whatever budget remains.

**Closing phones gets a bounded, shared budget** (`PHONE_CLOSE_BUDGET_SECONDS`).
``systemctl stop`` allows ``DefaultTimeoutStopSec`` (90s stock) before it
escalates to SIGKILL, and a handler that overran it would be killed mid-cleanup,
which is worse than not starting. Every close is started on its own daemon
thread so ten phones close in parallel and one hung close cannot eat another's
time; the budget is a deadline on the whole batch, not per phone. Daemon threads
also mean a close still in flight can never keep the interpreter alive.

The handler re-raises the signal with the default disposition once it is done,
so the process dies *of the signal systemd sent it* rather than of some invented
exit code -- which is what systemd records as a clean stop.

**No lock is taken while draining.** Signal handlers run in the main thread, and
that thread also registers (a lock acquired in `ProfileLocks.__enter__`). If the
drain waited on a mutex the main thread already held, the handler would deadlock
and the unit would hang until SIGKILL. Registration is serialized; draining uses
only single C-level list/dict operations, which the GIL already makes atomic.
The worst case is a registration racing the drain and being missed -- irrelevant
in a process that is about to die.
"""

from __future__ import annotations

import itertools
import os
import signal
import threading
import time

# Wall-clock ceiling for closing *all* still-open phones. Chosen well inside
# systemd's 90s stop timeout: the point is to be reliably finished, not to be
# thorough. Ten parallel closes against a healthy MultiLogin agent take ~1s.
PHONE_CLOSE_BUDGET_SECONDS = 15.0

# Signals worth handling. SIGTERM is what `systemctl stop` sends; SIGINT is
# Ctrl-C on a hand-run loop, which leaks exactly the same way.
DEFAULT_SIGNALS = ("SIGTERM", "SIGINT")

_registry_lock = threading.Lock()
_lock_holders: list = []          # ProfileLocks instances this process owns
_open_profiles: dict = {}         # token -> (profile_id, close_callable)
_tokens = itertools.count(1)
_installed: dict = {}             # signum -> previous handler
_ran = False                      # the drain is once-per-process


# --- registration ------------------------------------------------------------

def register_locks(holder):
    """Note that `holder` (a `ProfileLocks`) holds locks for this process.

    Only registered holders are released, which is what keeps a stop of the
    posting loop from stomping the locks warmup or recheck are holding -- those
    live in different processes with their own registries.
    """
    with _registry_lock:
        if not any(h is holder for h in _lock_holders):
            _lock_holders.append(holder)
    return holder


def unregister_locks(holder) -> None:
    with _registry_lock:
        for index, existing in enumerate(_lock_holders):
            if existing is holder:
                del _lock_holders[index]
                return


def register_open_profile(profile_id, close) -> int:
    """Note that `profile_id`'s phone is open, and how to close it.

    `close` is called with no arguments and must be safe to call twice -- the
    workflow's own close path may well have run first.
    """
    token = next(_tokens)
    with _registry_lock:
        _open_profiles[token] = (str(profile_id), close)
    return token


def unregister_open_profile(token) -> None:
    with _registry_lock:
        _open_profiles.pop(token, None)


def open_profile_ids() -> list:
    """Which phones this process currently believes are open (for logging)."""
    return [profile_id for profile_id, _ in list(_open_profiles.values())]


# --- draining ----------------------------------------------------------------

def _log(logger, level: str, *args) -> None:
    if logger is None:
        return
    try:
        getattr(logger, level)(*args)
    except Exception:
        pass


def _close_one(profile_id, close, logger) -> None:
    try:
        close()
    except Exception as exc:
        # One phone that will not close must not cost the others their budget,
        # and must not abort the drain.
        _log(logger, "warning", "shutdown: failed to close profile %s: %s", profile_id, exc)


def _close_phones(entries, budget_seconds, logger) -> None:
    if not entries:
        return
    deadline = time.monotonic() + max(0.0, float(budget_seconds))
    _log(logger, "info", "shutdown: closing %s open phone(s) within %.0fs: %s",
         len(entries), budget_seconds, ", ".join(pid for pid, _ in entries))

    threads = []
    for profile_id, close in entries:
        thread = threading.Thread(target=_close_one, args=(profile_id, close, logger),
                                  name=f"shutdown-close-{profile_id}", daemon=True)
        thread.start()
        threads.append((profile_id, thread))

    for profile_id, thread in threads:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(remaining)
        if thread.is_alive():
            _log(logger, "warning",
                 "shutdown: profile %s did not close within the %.0fs budget; leaving it to "
                 "MultiLogin", profile_id, budget_seconds)


def run_cleanup(reason: str = "", logger=None,
                budget_seconds: float = PHONE_CLOSE_BUDGET_SECONDS) -> bool:
    """Release this process's locks, then close its phones. Returns True the
    first time and False every time after -- a second SIGTERM (or an impatient
    second Ctrl-C) must not re-enter the drain.

    Deliberately takes no lock: see the module docstring.
    """
    global _ran
    if _ran:
        return False
    _ran = True

    holders = list(_lock_holders)
    _lock_holders.clear()
    entries = list(_open_profiles.values())
    _open_profiles.clear()

    _log(logger, "warning", "shutdown: %s -- releasing %s lock holder(s), closing %s phone(s)",
         reason or "terminating", len(holders), len(entries))

    # 1) Locks first. Instant, local, and the failure this fixes is the one that
    #    costs the *next* run 45 minutes.
    for holder in holders:
        try:
            held = list(getattr(holder, "held", []) or [])
            holder.release_all()
            if held:
                _log(logger, "info", "shutdown: released %s profile lock(s): %s",
                     len(held), ", ".join(str(name) for name in held))
        except Exception as exc:
            _log(logger, "warning", "shutdown: failed to release locks: %s", exc)

    # 2) Phones, on whatever budget we said we would spend.
    _close_phones(entries, budget_seconds, logger)
    _log(logger, "info", "shutdown: cleanup done")
    return True


# --- signal handling ---------------------------------------------------------

def _reraise(signum: int) -> None:
    """Die of the signal we were sent, with the default disposition restored.

    Not ``sys.exit``: a oneshot unit that exits 143 is recorded as *failed*,
    while a unit that terminates on the SIGTERM systemd itself sent is recorded
    as a clean stop. Re-raising also keeps Ctrl-C behaving like Ctrl-C.
    """
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (ValueError, OSError):
        pass
    os.kill(os.getpid(), signum)


def handle_signal(signum, frame=None, logger=None,
                  budget_seconds: float = PHONE_CLOSE_BUDGET_SECONDS) -> None:
    """The handler itself. Drains once, then re-raises so the caller sees a
    normal termination."""
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    try:
        run_cleanup(reason=f"received {name}", logger=logger, budget_seconds=budget_seconds)
    finally:
        _reraise(signum)


def install_signal_handlers(logger=None, signal_names=DEFAULT_SIGNALS,
                            budget_seconds: float = PHONE_CLOSE_BUDGET_SECONDS) -> list:
    """Install the handler for SIGTERM/SIGINT. Returns the signums installed.

    Safe to call from anywhere: signals can only be installed from the main
    thread of the main interpreter, and a context where that is not true (a UI
    worker, a test) simply gets no handler rather than an exception.
    """
    installed = []
    for name in signal_names:
        signum = getattr(signal, name, None)
        if signum is None:
            continue

        def _handler(sig, frame, _budget=budget_seconds):
            handle_signal(sig, frame, logger=logger, budget_seconds=_budget)

        try:
            previous = signal.signal(signum, _handler)
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or the platform will not let us. Nothing to
            # do about it, and it must not stop the loop from running.
            continue
        _installed[int(signum)] = previous
        installed.append(int(signum))
    if installed:
        _log(logger, "info", "shutdown: cleanup handler installed for %s",
             ", ".join(sorted(_name_of(s) for s in installed)))
    return installed


def _name_of(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except Exception:
        return str(signum)


def reset(restore_handlers: bool = False) -> None:
    """Forget everything. For tests -- a process only ever drains once, so a
    test that has drained needs the flag cleared before the next one."""
    global _ran
    _ran = False
    with _registry_lock:
        _lock_holders.clear()
        _open_profiles.clear()
    if restore_handlers:
        for signum, previous in list(_installed.items()):
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):
                pass
    _installed.clear()
