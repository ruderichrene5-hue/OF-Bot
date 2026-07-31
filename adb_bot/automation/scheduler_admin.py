"""Manage the bot's Windows Task Scheduler jobs from Python, so the UI can turn
each loop on/off and set its interval without anyone touching Task Scheduler by
hand.

Each loop becomes one task named ``ADBBot-<loop>`` that runs
``python -m adb_bot.automation.run_loop <loop> [--apply]`` with the repo as its
working directory. Tasks are created from a generated Task Scheduler XML (via
``schtasks /Create /XML``) rather than a ``/TR`` command string -- the XML sets
the working directory and repetition explicitly and sidesteps schtasks quoting.

The XML builders are pure and unit-tested; only install/remove/run/query shell
out. Everything is win32-guarded (`is_supported`).

This is the Windows *backend*. Callers should go through
`adb_bot.automation.scheduling`, which picks this or the systemd backend."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from adb_bot.automation.schedule_spec import (  # noqa: F401  (re-exported: callers and tests use these)
    DEFAULT_DAILY_START,
    DEFAULT_INTERVALS,
    LOOPS,
    TASK_PREFIX,
    loop_arguments,
    python_exe,
    repo_root,
    task_name,
)

_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def is_supported() -> bool:
    return sys.platform == "win32"


def _iso_minutes(interval_min: int) -> str:
    """ISO-8601 duration for a minute interval, e.g. 10 -> 'PT10M'."""
    return f"PT{max(1, int(interval_min))}M"


def _triggers_xml(loop: str, interval_min: int, start_time: str | None) -> str:
    """A daily calendar trigger. Sub-day intervals repeat all day; a whole-day
    interval fires once per N days at `start_time`."""
    interval_min = max(1, int(interval_min))
    if interval_min < 1440:
        # Repeat every interval_min minutes for a full day, every day.
        return (
            "<CalendarTrigger>"
            "<StartBoundary>2020-01-01T00:00:00</StartBoundary>"
            "<Enabled>true</Enabled>"
            f"<Repetition><Interval>{_iso_minutes(interval_min)}</Interval>"
            "<Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
            "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
            "</CalendarTrigger>"
        )
    days = max(1, interval_min // 1440)
    start = start_time or DEFAULT_DAILY_START.get(loop, "23:30")
    return (
        "<CalendarTrigger>"
        f"<StartBoundary>2020-01-01T{start}:00</StartBoundary>"
        "<Enabled>true</Enabled>"
        f"<ScheduleByDay><DaysInterval>{days}</DaysInterval></ScheduleByDay>"
        "</CalendarTrigger>"
    )


def build_task_xml(loop: str, interval_min: int, apply: bool = True,
                   python: str | None = None, working_dir: str | None = None,
                   start_time: str | None = None, run_when_logged_off: bool = False) -> str:
    """The full Task Scheduler XML for one loop. Pure -- unit-tested.

    `run_when_logged_off` runs the task as SYSTEM so it fires even with nobody
    logged on (survives RDP disconnects/reboots). Registering such a task needs
    an elevated (admin) process. Note SYSTEM has its own environment and user
    profile, so tokens must be **machine-level** env vars for it to see them.
    """
    python = python or python_exe()
    working_dir = working_dir or str(repo_root())
    arguments = loop_arguments(loop, apply)
    triggers = _triggers_xml(loop, interval_min, start_time)
    if run_when_logged_off:
        principal = ('<Principal id="Author">'
                     "<UserId>S-1-5-18</UserId>"          # LocalSystem
                     "<RunLevel>HighestAvailable</RunLevel>"
                     "</Principal>")
    else:
        principal = ('<Principal id="Author">'
                     "<LogonType>InteractiveToken</LogonType>"
                     "<RunLevel>LeastPrivilege</RunLevel>"
                     "</Principal>")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{_TASK_NS}">'
        "<RegistrationInfo>"
        f"<Description>ADB bot {loop} loop</Description>"
        "</RegistrationInfo>"
        f"<Triggers>{triggers}</Triggers>"
        f"<Principals>{principal}</Principals>"
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<Enabled>true</Enabled>"
        "<ExecutionTimeLimit>PT2H</ExecutionTimeLimit>"
        "<AllowStartOnDemand>true</AllowStartOnDemand>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "</Settings>"
        "<Actions Context=\"Author\">"
        "<Exec>"
        f"<Command>{_xml_escape(python)}</Command>"
        f"<Arguments>{_xml_escape(arguments)}</Arguments>"
        f"<WorkingDirectory>{_xml_escape(working_dir)}</WorkingDirectory>"
        "</Exec>"
        "</Actions>"
        "</Task>"
    )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --- shell-outs (win32 only) -------------------------------------------------

def _run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def install(loop: str, interval_min: int, apply: bool = True,
            run_when_logged_off: bool = False) -> tuple[bool, str]:
    """Create/replace the ADBBot-<loop> task. Returns (ok, message)."""
    if not is_supported():
        return (False, "Task Scheduler is only available on Windows.")
    xml = build_task_xml(loop, interval_min, apply=apply, run_when_logged_off=run_when_logged_off)
    # schtasks wants a Unicode XML file.
    fd = tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False)
    try:
        fd.write(xml)
        fd.close()
        proc = _run(["schtasks", "/Create", "/TN", task_name(loop), "/XML", fd.name, "/F"])
        ok = proc.returncode == 0
        message = (proc.stdout or proc.stderr or "").strip()
        if not ok and run_when_logged_off and "denied" in message.lower():
            message += " (run-when-logged-off registers as SYSTEM -- start the app as administrator)"
        return (ok, message)
    finally:
        try:
            Path(fd.name).unlink()
        except OSError:
            pass


def remove(loop: str) -> tuple[bool, str]:
    if not is_supported():
        return (False, "Task Scheduler is only available on Windows.")
    proc = _run(["schtasks", "/Delete", "/TN", task_name(loop), "/F"])
    # A missing task is treated as already-removed.
    if proc.returncode != 0 and "cannot find" in (proc.stderr or "").lower():
        return (True, "not installed")
    return (proc.returncode == 0, (proc.stdout or proc.stderr or "").strip())


def run_now(loop: str) -> tuple[bool, str]:
    if not is_supported():
        return (False, "Task Scheduler is only available on Windows.")
    proc = _run(["schtasks", "/Run", "/TN", task_name(loop)])
    return (proc.returncode == 0, (proc.stdout or proc.stderr or "").strip())


def query_state(loop: str) -> str | None:
    """The task's Status ('Ready' / 'Running' / 'Disabled'), or None if the task
    doesn't exist."""
    if not is_supported():
        return None
    proc = _run(["schtasks", "/Query", "/TN", task_name(loop), "/FO", "LIST"])
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "Installed"


def list_status() -> dict:
    """{loop: state-or-None} for all loops."""
    return {loop: query_state(loop) for loop in LOOPS}
