"""Manage the bot's scheduled loops as systemd timers, so the Linux server gets
the same on/off + interval control the Windows box has via Task Scheduler.

Each loop becomes a pair: ``adbbot-<loop>.service`` (a ``Type=oneshot`` unit that
runs the loop once) plus ``adbbot-<loop>.timer`` that fires it. That split is
what makes the systemd mapping faithful:

- **Overlap** — systemd refuses to start a service that is still active, so a
  timer tick during a long run is dropped. That is exactly Task Scheduler's
  ``MultipleInstancesPolicy=IgnoreNew``. The per-profile locks in `core.locks`
  still handle *cross-loop* collisions, which neither scheduler prevents.
- **Missed runs** — ``Persistent=true`` runs a job whose window was missed while
  the box was down, matching ``StartWhenAvailable``.
- **Runaway runs** — ``RuntimeMaxSec`` matches ``ExecutionTimeLimit``.
- **Logged off** — a *system* unit runs regardless of who is logged in, so the
  Windows "run when logged off" toggle has no equivalent switch here; it is
  simply always true. It is accepted and ignored for API symmetry.

Credentials are the one real difference. A systemd service inherits nothing from
your shell, so ``MULTILOGIN_TOKEN`` / ``AIRTABLE_TOKEN`` must come from an
``EnvironmentFile``. That is the direct analogue of the machine-level
environment variables the Windows runbook insists on, and the same trap: a loop
that works when you run it by hand fails under the timer because the token is
only in your profile. `ENV_FILE` is where we point it.

The unit builders are pure and unit-tested; only install/remove/run/query shell
out to systemctl.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from adb_bot.automation.schedule_spec import (
    DEFAULT_DAILY_START,
    DEFAULT_INTERVALS,
    LOOPS,
    MAX_RUNTIME_SECONDS,
    UNIT_PREFIX,
    description,
    loop_arguments,
    python_exe,
    repo_root,
    unit_name,
)

# System-wide units: they run at boot, with no user session, which is what an
# unattended server wants.
UNIT_DIR = Path("/etc/systemd/system")

# Optional env file holding the tokens. The leading '-' in the unit makes it
# optional, so a missing file is not a startup failure -- the loop will just
# report a missing token, which `doctor` explains.
ENV_FILE = "/etc/adbbot/env"


def is_supported() -> bool:
    """systemd present and usable. Checks for systemctl *and* that PID 1 is
    systemd, so a container without an init system reports False rather than
    failing at install time."""
    if not sys.platform.startswith("linux"):
        return False
    if not shutil.which("systemctl"):
        return False
    return Path("/run/systemd/system").is_dir()


# --- unit builders (pure) ----------------------------------------------------

def build_service_unit(loop: str, apply: bool = True, python: str | None = None,
                       working_dir: str | None = None, user: str | None = None,
                       env_file: str = ENV_FILE) -> str:
    """The ``adbbot-<loop>.service`` unit text."""
    python = python or python_exe()
    working_dir = working_dir or str(repo_root())
    lines = [
        "[Unit]",
        f"Description={description(loop)}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"WorkingDirectory={working_dir}",
        f"ExecStart={python} {loop_arguments(loop, apply)}",
        f"RuntimeMaxSec={MAX_RUNTIME_SECONDS}",
        "StandardOutput=journal",
        "StandardError=journal",
    ]
    if env_file:
        # '-' => tolerate the file not existing.
        lines.append(f"EnvironmentFile=-{env_file}")
    if user:
        lines.append(f"User={user}")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def build_timer_unit(loop: str, interval_min: int, start_time: str | None = None) -> str:
    """The ``adbbot-<loop>.timer`` unit text.

    Sub-day intervals repeat every N minutes from the last run; a one-day
    interval fires at a wall-clock time; a multi-day interval repeats every N
    days.
    """
    interval_min = max(1, int(interval_min))
    lines = [
        "[Unit]",
        f"Description={description(loop)} (timer)",
        "",
        "[Timer]",
        f"Unit={unit_name(loop, 'service')}",
    ]
    if interval_min < 1440:
        # Measured from when the service last became active, so a long run
        # pushes the next tick out rather than stacking one up behind it.
        # OnBootSec restarts the cadence after a reboot.
        lines.append("OnBootSec=5min")
        lines.append(f"OnUnitActiveSec={interval_min}min")
    elif interval_min == 1440:
        start = start_time or DEFAULT_DAILY_START.get(loop, "23:30")
        lines.append(f"OnCalendar=*-*-* {start}:00")
        # Catches up a daily run the box was down for -- StartWhenAvailable's
        # equivalent. systemd only honours this on OnCalendar timers, which is
        # why it is not set on the interval-based ones above.
        lines.append("Persistent=true")
    else:
        lines.append("OnBootSec=15min")
        lines.append(f"OnUnitActiveSec={max(1, interval_min // 1440)}d")
    lines += [
        "AccuracySec=30s",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    return "\n".join(lines)


# --- shell-outs (linux only) -------------------------------------------------

def _run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", *args])


def _unsupported() -> tuple[bool, str]:
    return (False, "systemd is not available on this machine.")


def _write_unit(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o644)


def install(loop: str, interval_min: int, apply: bool = True,
            run_when_logged_off: bool = False, unit_dir: Path | None = None,
            user: str | None = None) -> tuple[bool, str]:
    """Write + enable the timer for one loop. Returns (ok, message).

    `run_when_logged_off` is accepted for API parity with the Windows backend
    and ignored: system units always run with nobody logged in.
    """
    if not is_supported():
        return _unsupported()
    directory = Path(unit_dir) if unit_dir else UNIT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _write_unit(directory / unit_name(loop, "service"),
                    build_service_unit(loop, apply=apply, user=user))
        _write_unit(directory / unit_name(loop, "timer"),
                    build_timer_unit(loop, interval_min))
    except PermissionError:
        return (False, f"cannot write to {directory} -- run as root (sudo).")
    except OSError as exc:
        return (False, str(exc))

    reload_proc = _systemctl("daemon-reload")
    if reload_proc.returncode != 0:
        return (False, (reload_proc.stderr or "daemon-reload failed").strip())

    enable_proc = _systemctl("enable", "--now", unit_name(loop, "timer"))
    ok = enable_proc.returncode == 0
    return (ok, (enable_proc.stdout or enable_proc.stderr or "").strip())


def remove(loop: str, unit_dir: Path | None = None) -> tuple[bool, str]:
    if not is_supported():
        return _unsupported()
    directory = Path(unit_dir) if unit_dir else UNIT_DIR
    timer = unit_name(loop, "timer")
    # A timer that was never installed is treated as already-removed.
    if not (directory / timer).exists():
        return (True, "not installed")
    disable_proc = _systemctl("disable", "--now", timer)
    try:
        (directory / timer).unlink(missing_ok=True)
        (directory / unit_name(loop, "service")).unlink(missing_ok=True)
    except PermissionError:
        return (False, f"cannot remove units from {directory} -- run as root (sudo).")
    except OSError as exc:
        return (False, str(exc))
    _systemctl("daemon-reload")
    return (True, (disable_proc.stdout or disable_proc.stderr or "").strip())


def run_now(loop: str) -> tuple[bool, str]:
    """Trigger the loop's service immediately, independently of its timer."""
    if not is_supported():
        return _unsupported()
    # --no-block: a oneshot `systemctl start` otherwise blocks until the whole
    # loop finishes, which would freeze the UI for minutes.
    proc = _systemctl("start", "--no-block", unit_name(loop, "service"))
    return (proc.returncode == 0, (proc.stdout or proc.stderr or "").strip())


def query_state(loop: str) -> str | None:
    """'Running' / 'Ready' / 'Disabled', or None when not installed.

    Deliberately mirrors the words the Windows backend returns so the UI's
    status column reads the same on both platforms.
    """
    if not is_supported():
        return None
    timer = unit_name(loop, "timer")
    enabled = _systemctl("is-enabled", timer)
    state = (enabled.stdout or enabled.stderr or "").strip()
    if not state or "No such file" in state or state == "not-found":
        return None
    service_active = (_systemctl("is-active", unit_name(loop, "service")).stdout or "").strip()
    if service_active in ("active", "activating"):
        return "Running"
    if state in ("enabled", "enabled-runtime", "static"):
        return "Ready"
    return "Disabled"


def list_status() -> dict:
    """{loop: state-or-None} for all loops."""
    return {loop: query_state(loop) for loop in LOOPS}
