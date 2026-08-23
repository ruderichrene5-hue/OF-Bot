"""How many MultiLogin phone-minutes this fleet has burned, and whether that is
about to stop it.

**MultiLogin will not tell us the balance.** Confirmed 2026-08-18: there is no
API route that reports it -- `/workspace/quota`, `/user/plan`,
`/mobile_profiles/quota` and a dozen similar all answer `403 FORBIDDEN_REQUEST`,
which is MLX's "no such route" (`/user/workspaces` and `/workspace/folders`
answer 200 on the same token, so it is not permissions), and the local launcher
404s the same guesses. The dashboard is the only place the number exists.

So this counts what we *spent* instead, from MultiLogin's own launcher log,
which writes both ends of every session:

    mobile profile '<id>' started
    mobileProcessMeta for profile <id> finished successfully.

Subtracting that from a configured allowance is the closest thing to a balance
available without a person opening the dashboard. It is deliberately an
**under**count: a session the log never closes contributes nothing rather than a
guessed duration, and is reported separately, because inventing minutes here
would turn a warning into a false alarm.

Why it matters: on 2026-08-18 the minutes ran out at 16:59 and the whole fleet
stopped launching for hours. Nothing noticed. Every layer reported it as a
server fault -- the bot said `42002 profile is not running`, the launcher said
`501`, and neither mentioned quota -- so the loops spent three hours retrying a
wall. That is the second alert here: if every profile in consecutive runs failed
to launch, something systemic is wrong whatever the cause.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

# Where MultiLogin writes its launcher logs. One file per day.
DEFAULT_LOG_DIR = Path("/root/mlx/logs")
LOG_GLOB = "launcher_*.log"

_START = re.compile(r"^(\S+)\s+info\s+\S+\s+mobile profile '(\d+)' started")
_FINISH = re.compile(
    r"^(\S+)\s+info\s+\S+\s+mobileProcessMeta for profile (\d+) finished")

# The 501 the cloud returns once the workspace cannot start profiles. Not itself
# proof of an empty balance -- MLX sends it for its own faults too -- but it is
# the line that separates "the phones are unhealthy" from "nothing is starting
# at all", so it is worth carrying into the alert.
_START_REFUSED = re.compile(r"start profiles for wsID \S+ returned (\d+) http status")

# Warn below this many minutes remaining. Roughly a day's fleet usage at the
# 2026-08 rate (~2,000 minutes/day across ~184 profiles), which is the point at
# which topping up is still a decision rather than an outage.
DEFAULT_WARN_BELOW = 800

# Consecutive loop runs in which *every* launch failed before saying so. One is
# ordinary -- a single bad phone, a run that found nothing to do. Two in a row
# with attempts in both is the shape of a fleet-wide stop.
DEFAULT_FAIL_TICKS = 2

# Where the last alert is remembered, so a state that persists for hours is
# reported once rather than every tick.
STATE_FILE = Path.home() / ".adb_bot" / "mlx_minutes.json"

# Once warned, stay quiet about the same condition for this long. Long enough
# that a four-hourly check does not repeat itself, short enough that a problem
# left unfixed overnight is raised again in the morning.
REALERT_HOURS = 24


@dataclass
class DayUsage:
    """One day's billable phone time."""

    day: str
    sessions: int = 0
    minutes: float = 0.0
    unclosed: int = 0            # started, never closed in the log


@dataclass
class MinutesReport:
    """Everything the alert and the dashboard need."""

    by_day: list = field(default_factory=list)      # DayUsage, oldest first
    period_start: str = ""
    period_minutes: float = 0.0
    period_sessions: int = 0
    allowance: int | None = None
    remaining: float | None = None
    today_minutes: float = 0.0
    unclosed: int = 0
    warn_below: int = DEFAULT_WARN_BELOW

    @property
    def low(self) -> bool:
        """Whether the balance is worth warning about.

        False when no allowance is configured -- an unknown balance is not a low
        one, and crying wolf on every tick of an unconfigured install is how an
        alert gets muted.
        """
        return self.remaining is not None and self.remaining < self.warn_below

    @property
    def used_pct(self) -> float | None:
        if not self.allowance:
            return None
        return min(100.0, 100.0 * self.period_minutes / self.allowance)


def _stamp(text: str) -> datetime:
    return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")


def log_dir(path=None) -> Path:
    return Path(path or os.environ.get("MLX_LOG_DIR") or DEFAULT_LOG_DIR)


def usage_by_day(path=None) -> list:
    """Billable minutes per day, oldest first.

    Sessions are paired per profile id. A second `started` for a profile with no
    intervening finish replaces the first: the launcher does not log the end of a
    session it lost, and carrying the older timestamp would bill the gap.
    """
    days = []
    for log in sorted(log_dir(path).glob(LOG_GLOB)):
        day = log.stem.replace("launcher_", "")
        open_at: dict = {}
        usage = DayUsage(day=day)
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _START.match(line)
            if match:
                open_at[match.group(2)] = _stamp(match.group(1))
                continue
            match = _FINISH.match(line)
            if match and match.group(2) in open_at:
                began = open_at.pop(match.group(2))
                usage.minutes += (_stamp(match.group(1)) - began).total_seconds() / 60
                usage.sessions += 1
        usage.unclosed = len(open_at)
        if usage.sessions or usage.unclosed:
            days.append(usage)
    return days


def period_start(now: date, reset_day: int) -> date:
    """The first day of the billing period `now` falls in.

    `reset_day` is a day of the month. A plan that renews on the 31st has no
    such day in most months, so the start is clamped into the month rather than
    skipped -- a period that silently failed to start would report a fleet's
    whole history as this month's usage.
    """
    reset_day = max(1, min(28, int(reset_day)))
    if now.day >= reset_day:
        return now.replace(day=reset_day)
    first = now.replace(day=1)
    previous = first - timedelta(days=1)
    return previous.replace(day=reset_day)


def collect(path=None, now=None, allowance=None, reset_day=None,
            warn_below=None) -> MinutesReport:
    """Read the logs and work out where the period stands."""
    now = now or datetime.now()
    allowance = allowance if allowance is not None else _env_int("MLX_MINUTES_ALLOWANCE")
    reset_day = reset_day if reset_day is not None else (
        _env_int("MLX_MINUTES_RESET_DAY") or 1)
    warn_below = warn_below if warn_below is not None else (
        _env_int("MLX_MINUTES_WARN_BELOW") or DEFAULT_WARN_BELOW)

    days = usage_by_day(path)
    start = period_start(now.date(), reset_day)
    start_key = start.strftime("%Y%m%d")
    today_key = now.strftime("%Y%m%d")

    in_period = [d for d in days if d.day >= start_key]
    report = MinutesReport(
        by_day=days,
        period_start=start.isoformat(),
        period_minutes=sum(d.minutes for d in in_period),
        period_sessions=sum(d.sessions for d in in_period),
        allowance=allowance,
        today_minutes=sum(d.minutes for d in days if d.day == today_key),
        unclosed=sum(d.unclosed for d in in_period),
        warn_below=warn_below,
    )
    if allowance:
        report.remaining = max(0.0, allowance - report.period_minutes)
    return report


def _env_int(name: str):
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def failing_runs(runs, ticks: int = DEFAULT_FAIL_TICKS) -> list:
    """The trailing runs in which every launch attempt failed, if there are
    `ticks` of them in a row.

    A run with no attempts is not a failing run -- it is a run with nothing to
    do, and counting it would fire this alert every quiet night. Returns [] when
    the condition is not met, so the caller can treat it as a boolean and still
    have the evidence to put in the message.
    """
    if ticks <= 0:
        return []
    attempted = [r for r in runs if getattr(r, "attempts", 0) > 0]
    tail = attempted[-ticks:]
    if len(tail) < ticks:
        return []
    return tail if all(getattr(r, "ok", 0) == 0 for r in tail) else []


def refusal_counts(path=None, day: str = "") -> dict:
    """How many times the cloud refused to start profiles, by HTTP status.

    Context for the alert rather than a trigger: it is what tells whoever reads
    it that the phones were never reached, so the answer is the account and not
    the fleet.
    """
    day = day or datetime.now().strftime("%Y%m%d")
    log = log_dir(path) / f"launcher_{day}.log"
    counts: dict = {}
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return counts
    for match in _START_REFUSED.finditer(text):
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


# --- alert state -----------------------------------------------------------

def _load_state(path=None) -> dict:
    path = Path(path or STATE_FILE)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, path=None) -> None:
    path = Path(path or STATE_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError:
        pass


def should_alert(key: str, active: bool, now=None, state=None,
                 realert_hours: int = REALERT_HOURS) -> bool:
    """Whether to send the `key` alert now, given it is currently `active`.

    Alerts on the way into a state, and again only after `realert_hours` while it
    persists. A condition that clears resets, so the next occurrence is reported
    immediately. Without this, an outage like 2026-08-18's would have sent the
    same message every four hours for as long as it lasted.
    """
    now = now or datetime.now()
    state = state if state is not None else _load_state()
    last = state.get(key)
    if not active:
        state.pop(key, None)
        return False
    if not last:
        state[key] = now.isoformat()
        return True
    try:
        since = datetime.fromisoformat(last)
    except ValueError:
        state[key] = now.isoformat()
        return True
    if (now - since) >= timedelta(hours=realert_hours):
        state[key] = now.isoformat()
        return True
    return False


# --- the messages ----------------------------------------------------------

def minutes_message(report: MinutesReport) -> str:
    """Telegram HTML for the low-balance warning."""
    lines = [
        "⚠️ <b>MultiLogin minutes are low</b>",
        f"About <b>{report.remaining:,.0f}</b> minutes left "
        f"of {report.allowance:,} (warn below {report.warn_below:,}).",
        f"Used <b>{report.period_minutes:,.0f}</b> since {report.period_start} "
        f"across {report.period_sessions:,} sessions.",
        f"Today so far: {report.today_minutes:,.0f} minutes.",
        "",
        "When these run out <b>every launch fails</b> and nothing says why -- "
        "the bot reports <code>42002 profile is not running</code> and the "
        "launcher a bare <code>501</code>. Top up in the MultiLogin dashboard.",
    ]
    if report.unclosed:
        lines.insert(4, f"({report.unclosed} session(s) the log never closed are "
                        f"not counted, so the real figure is a little higher.)")
    return "\n".join(lines)


def failure_message(tail, refusals: dict) -> str:
    """Telegram HTML for the everything-is-failing warning."""
    attempts = sum(getattr(r, "attempts", 0) for r in tail)
    lines = [
        "🚨 <b>No profile is launching</b>",
        f"The last <b>{len(tail)}</b> posting runs attempted "
        f"<b>{attempts}</b> launches and none succeeded.",
    ]
    if refusals:
        detail = ", ".join(f"{n}× HTTP {code}"
                           for code, n in sorted(refusals.items()))
        lines.append(f"MultiLogin refused to start profiles today: {detail}.")
        lines.append("A <code>501</code> here is usually an <b>empty minute "
                     "balance</b> -- check the MultiLogin dashboard first.")
    else:
        lines.append("No cloud refusals logged, so this is more likely the "
                     "phones or the launcher than the account.")
    return "\n".join(lines)


# --- the loop --------------------------------------------------------------

def run_check(logger=None, notifier=None, now=None, state_path=None,
              runs=None, dry_run: bool = False) -> dict:
    """One tick: warn if the balance is low, or if nothing is launching.

    Returns what it found either way, so the caller can log it and the tests can
    assert on it without a chat server. Never raises -- this runs on a timer,
    and an alerting path that can crash is one that stops alerting.
    """
    now = now or datetime.now()
    out: dict = {"sent": []}
    try:
        report = collect(now=now)
    except Exception as exc:                                  # noqa: BLE001
        _log(logger, "warning", "mlx_minutes: could not read usage (%s)", exc)
        return {"sent": [], "error": str(exc)[:200]}

    out["period_minutes"] = round(report.period_minutes)
    out["remaining"] = None if report.remaining is None else round(report.remaining)
    out["allowance"] = report.allowance
    out["low"] = report.low

    if report.allowance is None:
        # Said once per tick at info: the counter still works and still shows on
        # the dashboard, but the threshold cannot fire without a number to
        # subtract from, and silence there would look like "the balance is fine".
        _log(logger, "info",
             "mlx_minutes: %.0f minutes used since %s; set "
             "MLX_MINUTES_ALLOWANCE to enable the low-balance warning",
             report.period_minutes, report.period_start)
    else:
        _log(logger, "info", "mlx_minutes: ~%.0f of %d minutes left since %s",
             report.remaining, report.allowance, report.period_start)

    if runs is None:
        runs = _posting_runs(logger)
    tail = failing_runs(runs)
    out["all_failing"] = bool(tail)

    state = _load_state(state_path)
    alerts = []
    if should_alert("low_minutes", report.low, now=now, state=state):
        alerts.append(("low_minutes", minutes_message(report)))
    if should_alert("all_failing", bool(tail), now=now, state=state):
        alerts.append(("all_failing", failure_message(tail, refusal_counts())))
    _save_state(state, state_path)

    if dry_run:
        out["would_send"] = [name for name, _ in alerts]
        return out

    if alerts and notifier is None:
        from adb_bot.clients.telegram import TelegramNotifier
        notifier = TelegramNotifier()
    for name, body in alerts:
        # `all_failing` is "nothing is launching" with no cause attached, which
        # is a different thing from the balance running out -- and since
        # 2026-08-21 `mlx-guard` says the same thing with the cause named. Its
        # own category so it can be silenced without silencing the balance
        # warning this loop exists for.
        category = "minutes" if name == "low_minutes" else "fleet"
        if notifier is not None and not notifier.allows(category):
            _log(logger, "info", "mlx_minutes: %s alert suppressed (%s not in "
                 "ADBBOT_TELEGRAM_ALERTS)", name, category)
            continue
        if notifier is not None and notifier.send(body, logger=logger,
                                                  category=category):
            out["sent"].append(name)
        else:
            _log(logger, "warning", "mlx_minutes: could not send %s alert", name)
    return out


def _posting_runs(logger=None) -> list:
    """Today's posting runs, or [] if they cannot be read.

    Imported here rather than at module scope: `report` pulls in the whole
    reporting stack, and this module is also imported by the dashboard, which
    would make that circular.
    """
    try:
        from adb_bot.automation import report as report_mod
        return report_mod.parse_posting_runs(day=datetime.now().strftime("%Y-%m-%d"))
    except Exception as exc:                                  # noqa: BLE001
        _log(logger, "warning", "mlx_minutes: could not read posting runs (%s)",
             exc)
        return []


def _log(logger, level: str, message: str, *args) -> None:
    if logger is not None:
        getattr(logger, level)(message, *args)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Report MultiLogin minute usage and warn when it runs low.")
    parser.add_argument("--dry-run", action="store_true",
                        help="work out the alerts but send nothing")
    args = parser.parse_args(argv)

    from adb_bot.core.logger import get_logger
    report = collect()
    print(f"period from {report.period_start}: "
          f"{report.period_minutes:,.0f} minutes over "
          f"{report.period_sessions:,} sessions")
    if report.allowance:
        print(f"allowance {report.allowance:,} -> about "
              f"{report.remaining:,.0f} left ({report.used_pct:.0f}% used)")
    else:
        print("no MLX_MINUTES_ALLOWANCE set -- no balance to report")
    print(f"today: {report.today_minutes:,.0f} minutes")
    for day in report.by_day[-7:]:
        print(f"  {day.day}  {day.sessions:>5} sessions  "
              f"{day.minutes:>8,.0f} min  {day.unclosed} unclosed")
    result = run_check(logger=get_logger("adb_bot"), dry_run=args.dry_run)
    print(f"\n{result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
