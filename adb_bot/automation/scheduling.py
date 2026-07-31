"""One scheduling API, whichever OS the bot is running on.

The UI and `doctor` call this; it picks Windows Task Scheduler
(`scheduler_admin`) or systemd timers (`systemd_admin`) underneath. Both
backends expose the same install/remove/run_now/query_state surface, so nothing
above this module needs a platform branch.
"""

from __future__ import annotations

from adb_bot.automation import scheduler_admin, systemd_admin
from adb_bot.automation.schedule_spec import (  # noqa: F401  (re-exported for callers)
    DEFAULT_INTERVALS,
    LOOPS,
)

WINDOWS = "Windows Task Scheduler"
SYSTEMD = "systemd timers"


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
    module = backend()
    return {loop: None for loop in LOOPS} if module is None else module.list_status()
