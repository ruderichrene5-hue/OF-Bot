"""One scheduling API, whichever OS the bot is running on.

The UI and `doctor` call this; it picks Windows Task Scheduler
(`scheduler_admin`) or systemd timers (`systemd_admin`) underneath. Both
backends expose the same install/remove/run_now/query_state surface, so nothing
above this module needs a platform branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adb_bot.automation import scheduler_admin, systemd_admin
from adb_bot.automation.schedule_spec import (  # noqa: F401  (re-exported for callers)
    DEFAULT_INTERVALS,
    FALLBACK_INTERVAL_MIN,
    LOOPS,
    PLANNED_LOOPS,
    RECOMMENDED_INTERVALS,
    RECOMMENDED_LOOPS,
)

WINDOWS = "Windows Task Scheduler"
SYSTEMD = "systemd timers"

# States that mean the timer will actually fire. Anything else is installed but
# inert, which looks fine in `systemctl list-units` and is the reason a loop can
# be "installed" and still never run.
LIVE_STATES = ("Ready", "Running")


def backend():
    """The active backend module, or None when this machine can't schedule."""
    if scheduler_admin.is_supported():
        return scheduler_admin
    if systemd_admin.is_supported():
        return systemd_admin
    return None


def backend_name() -> str:
    module = backend()
    if module is scheduler_admin:
        return WINDOWS
    if module is systemd_admin:
        return SYSTEMD
    return "none"


def is_supported() -> bool:
    return backend() is not None


def unavailable_reason() -> str:
    """Why scheduling is unavailable here -- shown in the UI and by `doctor`."""
    import sys
    if sys.platform.startswith("linux"):
        return ("systemd not available (no systemctl, or PID 1 isn't systemd). "
                "Install the units manually with deploy/systemd/install_units.sh.")
    return (f"No supported scheduler on {sys.platform}. Loops can still be run by hand "
            "with `python -m adb_bot.automation.run_loop <loop>`.")


def install(loop: str, interval_min: int, apply: bool = True,
            run_when_logged_off: bool = False) -> tuple[bool, str]:
    module = backend()
    if module is None:
        return (False, unavailable_reason())
    return module.install(loop, interval_min, apply=apply,
                          run_when_logged_off=run_when_logged_off)


def remove(loop: str) -> tuple[bool, str]:
    module = backend()
    if module is None:
        return (False, unavailable_reason())
    return module.remove(loop)


def run_now(loop: str) -> tuple[bool, str]:
    module = backend()
    if module is None:
        return (False, unavailable_reason())
    return module.run_now(loop)


def query_state(loop: str) -> str | None:
    module = backend()
    return None if module is None else module.query_state(loop)


def list_status() -> dict:
    """{loop: state-or-None} for every loop we expect to be scheduled.

    The backends only enumerate `schedule_spec.LOOPS`, so a loop that reached
    the CLI before it reached the spec (queue/retry, as they land) is asked for
    individually rather than silently reported as missing.
    """
    module = backend()
    if module is None:
        return {loop: None for loop in LOOPS}
    status = dict(module.list_status())
    for loop in installable_loops():
        if loop not in status:
            status[loop] = module.query_state(loop)
    return status


# --- the recommended set -----------------------------------------------------

def recommended_interval(loop: str) -> int:
    """Minutes between runs for `loop`.

    Falls back rather than raising: a loop added to the CLI before anyone agrees
    a cadence should still be installable, just conservatively.
    """
    return int(RECOMMENDED_INTERVALS.get(loop, FALLBACK_INTERVAL_MIN))


def cli_loops() -> tuple[str, ...]:
    """The loops `run_loop` can actually run, read from the CLI itself.

    Asked at call time so a loop landing in run_loop.py needs no edit here. The
    import is lazy and guarded because run_loop pulls in the Airtable/device
    stack, which may not be importable on the box doing the installing.
    """
    try:
        from adb_bot.automation.run_loop import LOOPS as commands
        return tuple(commands)
    except Exception:
        return tuple(LOOPS)


def installable_loops() -> tuple[str, ...]:
    """Recommended loops the CLI knows today, in recommended order.

    `queue` and `retry` are in the recommended set before they exist. Installing
    a timer for them now would produce a unit that fails on every tick and
    buries the real errors in the journal, so they are held back -- re-running
    the installer picks them up once they land.
    """
    known = cli_loops()
    return tuple(loop for loop in RECOMMENDED_LOOPS if loop in known)


def pending_loops() -> tuple[str, ...]:
    """Recommended loops the CLI does not know yet (see `installable_loops`)."""
    known = cli_loops()
    return tuple(loop for loop in RECOMMENDED_LOOPS if loop not in known)


@dataclass
class TimerReport:
    """Which scheduled loops are live, and which are not -- structured so
    `doctor` (and the UI) can say *which* instead of "no loops installed"."""

    backend: str = "none"
    supported: bool = False
    live: dict = field(default_factory=dict)     # loop -> "Ready"/"Running"
    stopped: dict = field(default_factory=dict)  # loop -> state, installed but inert
    missing: tuple = ()                          # installable, but no timer at all
    pending: tuple = ()                          # not a CLI command yet
    extra: dict = field(default_factory=dict)    # installed outside the recommended set

    @property
    def ok(self) -> bool:
        # `pending` is not a problem: those loops do not exist yet.
        return self.supported and not self.missing and not self.stopped

    @property
    def installer(self) -> str:
        return ("deploy/scheduler/install_tasks.ps1" if self.backend == WINDOWS
                else "sudo deploy/systemd/install_units.sh --apply")

    def summary(self) -> str:
        expected = len(self.live) + len(self.stopped) + len(self.missing)
        parts = [f"{self.backend}: {len(self.live)}/{expected} loops scheduled"]
        if self.live:
            parts.append("live: " + ", ".join(f"{k}={v}" for k, v in self.live.items()))
        if self.stopped:
            parts.append("installed but not running: "
                         + ", ".join(f"{k}={v}" for k, v in self.stopped.items()))
        if self.missing:
            parts.append("missing: " + ", ".join(self.missing))
        if self.pending:
            parts.append("not wired yet: " + ", ".join(self.pending))
        if self.extra:
            parts.append("unrecognised: " + ", ".join(self.extra))
        return "; ".join(parts)

    def hint(self) -> str:
        if not self.supported:
            return unavailable_reason()
        if self.missing:
            return (f"Install the missing loops with `{self.installer}` (or enable them in the "
                    "app's Scheduler window). Until then those loops only run when a human "
                    "types the command.")
        if self.stopped:
            return (f"Timer units exist but are disabled: re-enable with `{self.installer}`, "
                    "or check `systemctl list-timers 'adbbot-*' --all`.")
        return ""


def timer_report(status: dict | None = None) -> TimerReport:
    """Installed-vs-missing view of the recommended loops.

    `status` ({loop: state-or-None}) can be injected; by default it comes from
    the active backend. One call, so `doctor` stays a one-liner.
    """
    if not is_supported():
        return TimerReport(backend=backend_name(), supported=False,
                           missing=installable_loops(), pending=pending_loops())
    if status is None:
        status = list_status()
    expected = installable_loops()
    live, stopped, missing = {}, {}, []
    for loop in expected:
        state = status.get(loop)
        if state is None:
            missing.append(loop)
        elif state in LIVE_STATES:
            live[loop] = state
        else:
            stopped[loop] = state
    # A timer for something outside the recommended set is worth naming: it is
    # usually a renamed loop whose old unit is still firing.
    extra = {loop: state for loop, state in status.items()
             if state is not None and loop not in RECOMMENDED_LOOPS}
    return TimerReport(backend=backend_name(), supported=True, live=live, stopped=stopped,
                       missing=tuple(missing), pending=pending_loops(), extra=extra)
