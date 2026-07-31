"""What the scheduled loops are, independent of how any OS schedules them.

A leaf module so both backends (Windows Task Scheduler in `scheduler_admin`,
systemd timers in `systemd_admin`) and the `scheduling` façade can share it
without importing each other.
"""

from __future__ import annotations

import sys
from pathlib import Path

TASK_PREFIX = "ADBBot-"
UNIT_PREFIX = "adbbot-"

LOOPS = ("posting", "warmup", "pipeline", "mlx-sync", "cleanup")

# Sensible starting cadence (minutes). The UI can override per loop.
DEFAULT_INTERVALS = {"posting": 10, "pipeline": 20, "warmup": 360, "mlx-sync": 1440, "cleanup": 1440}

# For a daily task (interval a whole number of days) we need a start time.
DEFAULT_DAILY_START = {"mlx-sync": "23:30", "warmup": "08:00", "cleanup": "04:00"}

# A loop that overruns this is considered wedged and is killed, so the next
# cycle gets a clean start. Matches the Windows ExecutionTimeLimit of PT2H.
MAX_RUNTIME_SECONDS = 2 * 60 * 60

DESCRIPTIONS = {
    "posting": "ADB bot posting loop (Posting Queue -> IG)",
    "warmup": "ADB bot warmup loop (lifecycle Day 1-4)",
    "pipeline": "ADB bot spoofing pipeline (Drive/raw -> Spoof Variants)",
    "mlx-sync": "ADB bot MultiLogin->Airtable profile sync",
    "cleanup": "ADB bot cleanup loop (old used media)",
}


def task_name(loop: str) -> str:
    """Windows Task Scheduler name."""
    return f"{TASK_PREFIX}{loop}"


def unit_name(loop: str, kind: str = "service") -> str:
    """systemd unit name. `kind` is 'service' or 'timer'."""
    return f"{UNIT_PREFIX}{loop}.{kind}"


def repo_root() -> Path:
    # adb_bot/automation/schedule_spec.py -> repo root is two parents up from adb_bot.
    return Path(__file__).resolve().parents[2]


def python_exe() -> str:
    """Prefer the project venv's interpreter; fall back to the current one.

    The venv layout differs by platform: Windows puts the interpreter in
    `Scripts\\python.exe`, POSIX in `bin/python`.
    """
    root = repo_root()
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def loop_arguments(loop: str, apply: bool = True) -> str:
    """The argument string handed to the interpreter for one loop."""
    return f"-m adb_bot.automation.run_loop {loop}" + (" --apply" if apply else "")


def description(loop: str) -> str:
    return DESCRIPTIONS.get(loop, f"ADB bot {loop} loop")
