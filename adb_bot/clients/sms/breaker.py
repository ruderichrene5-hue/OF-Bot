"""The circuit breaker that routes around a burned number pool.

The rule, as specified:

- Count consecutive failed SMS requests. A code that arrives resets the count to
  zero; a request that produces no code inside 45 seconds refunds the number and
  adds one.
- At `MAX_CONSECUTIVE_FAILURES` (10) in a row, log a warning, put that provider
  in a 30-minute cooldown, switch to the other one, and reset the count.

Three implementation decisions that the specification does not spell out but
that decide whether it works here:

**The counter is on disk, not in a global.** The bot is not one long-lived
process: each loop is a separate systemd invocation that starts, drives a batch
of phones, and exits (see `core/locks.py` for the same constraint on profile
locks). A module-level `consecutive_failures` would reset to 0 on every run, so
a pool that fails twice per run would never reach 10 and the breaker would never
fire -- the exact outage it exists to survive. State therefore lives in a small
JSON file in the app-data directory, guarded by the same lock primitive the rest
of the codebase uses so two loops cannot lose each other's increments.

**Which provider is active is derived from the cooldowns, not stored.** Storing
an `active` field means storing something that can disagree with the cooldowns
after a crash, and it needs a second mechanism to switch *back* once 30 minutes
have passed. Instead the active provider is simply "the first one in preference
order that is not cooling down", which gives the specified behaviour -- trip
SMSPool, get 5sim; 30 minutes later, get SMSPool back -- with one source of
truth and no timer to fire.

**If every provider is cooling down, work continues anyway.** Refusing to rent a
number because both pools recently misbehaved would turn a degraded service into
a stopped one, and the phones waiting on verification would just pile up. The
provider whose cooldown ends soonest is used, and the fact is logged.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir
from adb_bot.core import locks

# --- policy constants ---------------------------------------------------------
MAX_CONSECUTIVE_FAILURES = 10       # trip after this many failures in a row
CODE_WAIT_SECONDS = 45              # how long one number gets to deliver a code
COOLDOWN_SECONDS = 30 * 60          # how long a tripped provider is left alone
POLL_INTERVAL_SECONDS = 3           # gap between check calls inside the wait

# The warning text is fixed by the specification -- keep it greppable.
SWITCH_WARNING = ("Primary provider unreachable/failing. "
                  "Switching to Secondary Provider.")

_STATE_FILENAME = "sms_breaker.json"
_LOCK_NAME = "sms_breaker_state"
_LOCK_TTL_SECONDS = 30              # a state update is milliseconds; 30s is a crash
_LOCK_WAIT_SECONDS = 5.0


@dataclass
class BreakerState:
    """What survives between runs."""

    consecutive_failures: int = 0
    # provider name -> unix timestamp at which it may be used again
    cooldowns: dict = field(default_factory=dict)
    # purely informational, for the dashboard and the logs
    last_switch_at: float | None = None
    last_failure_at: float | None = None
    updated_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "consecutive_failures": int(self.consecutive_failures),
            "cooldowns": {str(k): float(v) for k, v in (self.cooldowns or {}).items()},
            "last_switch_at": self.last_switch_at,
            "last_failure_at": self.last_failure_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw) -> "BreakerState":
        """Build from parsed JSON, tolerating anything the file might hold.

        A half-written or hand-edited state file must degrade to "no failures
        recorded, nothing cooling down" rather than crash the run -- losing the
        counter costs one delayed switch, crashing costs the whole batch.
        """
        if not isinstance(raw, dict):
            return cls()
        cooldowns = {}
        for name, until in (raw.get("cooldowns") or {}).items():
            try:
                cooldowns[str(name)] = float(until)
            except (TypeError, ValueError):
                continue
        try:
            failures = max(0, int(raw.get("consecutive_failures") or 0))
        except (TypeError, ValueError):
            failures = 0
        return cls(
            consecutive_failures=failures,
            cooldowns=cooldowns,
            last_switch_at=_as_float(raw.get("last_switch_at")),
            last_failure_at=_as_float(raw.get("last_failure_at")),
            updated_at=_as_float(raw.get("updated_at")),
        )

    # --- queries --------------------------------------------------------------
    def cooling_until(self, provider: str, now: float) -> float | None:
        """Timestamp this provider is blocked until, or None if it is usable."""
        until = self.cooldowns.get(provider)
        if until is None:
            return None
        return until if float(until) > now else None

    def is_cooling(self, provider: str, now: float) -> bool:
        return self.cooling_until(provider, now) is not None


def state_path() -> Path:
    return get_app_data_dir() / _STATE_FILENAME


class BreakerStore:
    """Load/save `BreakerState`, and mutate it under a cross-process lock."""

    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self.path = Path(path) if path else state_path()
        self._clock = clock

    def load(self) -> BreakerState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return BreakerState()
        return BreakerState.from_dict(raw)

    def save(self, state: BreakerState) -> bool:
        """Write the state atomically, so a crash cannot leave a truncated file."""
        state.updated_at = self._clock()
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

    @contextmanager
    def mutate(self):
        """Read-modify-write the state with other processes locked out.

        Yields the state; whatever the block leaves on it is saved on exit. If
        the lock cannot be taken within `_LOCK_WAIT_SECONDS` the update still
        goes ahead -- a delayed switch is a much smaller problem than a loop that
        blocks forever on a lock some crashed run left behind.
        """
        held = self._acquire_lock()
        try:
            state = self.load()
            yield state
            self.save(state)
        finally:
            if held:
                locks.release(_LOCK_NAME)

    def _acquire_lock(self) -> bool:
        # Deliberately wall-clock, not `self._clock`: waiting for another process
        # to drop a lock takes real time, and an injected test clock (which only
        # advances when the code under test sleeps) would never reach the
        # deadline and would spin here forever.
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            if locks.acquire(_LOCK_NAME, ttl_seconds=_LOCK_TTL_SECONDS,
                             owner="sms_breaker"):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
