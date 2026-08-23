"""Stop the fleet when MultiLogin runs out of proxy traffic or minutes, and say
so on Telegram.

Twice in two days the fleet spent hours launching phones into a wall. On
2026-08-18 the *minutes* ran out; on 2026-08-20 and again on 2026-08-21 the
*proxy traffic* did. Both look identical from inside the bot -- phones that
never reach `device` state -- and neither says why, so the loops keep paying for
launches that cannot work and profiles collect `Retries Exhausted` tags for a
fault that was never theirs. `mlx_minutes` warns about minutes; nothing watched
proxy, and nothing ever *stopped* anything.

This is the thing that stops it.

**Why a live probe and not the API.** There is no route that reports either
balance. `/proxy/traffic`, `/user/quota`, `/billing/usage` and a dozen more all
answer `403 FORBIDDEN_REQUEST` on a token that gets `200` from
`/user/workspaces`, so it is absence rather than permissions -- re-confirmed
2026-08-21. The dashboard is the only place the numbers exist. What *is*
available is the gateway itself: when the traffic allowance is empty
`gate.multilogin.com` answers **HTTP 402 Payment Required** to every request. A
402 is unambiguous, account-wide and instant, which makes it a far better
trigger than anything derived from logs.

**Why not 45010.** MLX repackages the dead gateway as error `45010` inside a
`200 OK` body, which is why no log ever said "out of traffic". But 45010 is a
generic proxy fault -- on 2026-08-21 it also carried
`Outbound IP in China not supported` -- so counting 45010 would stop the fleet
for one misrouted profile. 45010 is context for the alert, never the trigger.

**Telling the two outages apart.** They have different signatures in the
launcher log, and the alert has to name the right one or the top-up goes to the
wrong product:

    out of minutes  ->  floods of `start profiles ... returned 501 http status`
                        (1,030 of them on 2026-08-18)
    out of proxy    ->  floods of 45010 + the gateway answering 402
                        (only 20 x 501 across 2026-08-20 and 08-21)

**About the 2 GB warning.** Nothing reports gigabytes, so remaining traffic is
an *estimate*: phone-minutes burned since the last top-up times a rate. The
rate calibrates itself -- every time the gateway goes 402 the cycle that just
ended is measured, and `allowance / minutes_burned` becomes the new rate. Until
`MLX_PROXY_GB_ALLOWANCE` is set there is no number to subtract from and the GB
figure stays silent, exactly as `mlx_minutes` treats an unset minute allowance:
an unknown balance is not a low one.

That would leave a fresh install with no early warning at all, so there is a
fallback that needs no configuration: the length of the *previous* cycle is
measured every time the gateway goes 402, and being 85% of the way through what
the last top-up bought is worth saying out loud. Coarser than gigabytes -- a
quiet cycle and a busy one buy the same minutes but spend different traffic --
so the GB figure wins whenever it exists, and the message names its basis.

Config (normally `/etc/adbbot/env`):

    MLX_PROXY_GB_ALLOWANCE    GB in the current top-up. Unset -> fall back to
                              comparing against the last cycle's length.
    MLX_PROXY_GB_WARN_BELOW   warn at or below this many GB left. Default 2.
    MLX_PROXY_GB_PER_MINUTE   override the self-calibrated burn rate.
    MLX_PROXY_CYCLE_WARN_FRACTION
                              fallback threshold, as a fraction of the last
                              cycle's phone-minutes. Default 0.85.
    ADBBOT_GUARD_TIMERS       units to stop. Default posting, recheck, warmup.
    ADBBOT_GUARD_AUTORESUME   "1" to re-enable them when the gateway recovers.
                              Off by default: resuming spends money and posts
                              to live accounts, which is a person's decision.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from adb_bot.automation import mlx_minutes

# The loops that launch phones, and so the only ones worth stopping. The guard
# itself must never be in here -- a checker that switches itself off cannot
# notice the recovery, and the outage becomes permanent.
DEFAULT_TIMERS = ("adbbot-posting.timer", "adbbot-recheck.timer",
                  "adbbot-warmup.timer")

# How many profiles to send through the gateway per tick. Enough that one odd
# sticky session cannot speak for the account, few enough to be nearly free:
# each probe is a single small GET, and when traffic *is* live those bytes come
# out of the allowance this is meant to protect.
PROBE_SAMPLES = 4

# A 402 from two independent sticky sessions, with nothing succeeding, is the
# account being empty. One alone is not: sessions die individually, and a fleet
# stopped by a single bad exit is worse than one that waits a tick.
MIN_402_TO_STOP = 2

# Where the probe goes. Small, plain HTTP, no TLS handshake to pay for, and it
# answers with the exit IP -- which is worth logging, because "which country did
# we actually come out of" has been wrong before.
PROBE_URL = "http://ip-api.com/json"
PROBE_TIMEOUT = 25

# Only `http`-type proxies can be probed at all: the box has no PySocks, so the
# 193 socks5 profiles cannot be dialled. The allowance is shared across every
# profile in the workspace, so the 23 http ones report on all of them.
PROBE_TYPE = "http"

# HTTP 501s from `start profiles` inside the trailing window that mean the
# minutes, not the phones, are gone. 2026-08-18 produced 1,030 in a day; a
# healthy day produces a handful, so this sits far above noise and far below a
# real outage.
MINUTES_501_WINDOW_MINUTES = 90
MINUTES_501_THRESHOLD = 25

DEFAULT_GB_WARN_BELOW = 2.0

# Fallback early warning for when nobody has typed a GB figure in.
#
# The GB estimate needs `MLX_PROXY_GB_ALLOWANCE`, which only exists on the
# MultiLogin dashboard -- so on a fresh install the low-traffic warning is
# silent and the first thing anyone hears is the fleet stopping. But the
# previous cycle's length *is* measured, every time the gateway goes 402, and
# "you are 85% of the way through what the last top-up bought" is a real
# warning that needs no numbers from anyone.
#
# It is coarser than the GB figure -- a quiet cycle and a busy one buy the same
# minutes but spend different traffic -- so the GB estimate wins whenever it is
# available, and the message says which basis it used.
DEFAULT_CYCLE_WARN_FRACTION = 0.85

STATE_FILE = Path.home() / ".adb_bot" / "mlx_guard.json"

# Which Telegram category each alert belongs to, for `ADBBOT_TELEGRAM_ALERTS`.
# Proxy and minutes are separate categories because they are separate products
# with separate top-ups: somebody who wants to hear about one may not want to
# hear about the other.
ALERT_CATEGORIES = {
    "proxy_out": "proxy",
    "proxy_low": "proxy",
    "proxy_back": "proxy",
    "minutes_out": "minutes",
}


@dataclass
class Probe:
    """What the gateway said this tick."""

    status: str = "unknown"          # ok | exhausted | unknown
    ok: int = 0
    paid: int = 0                    # HTTP 402 -- the allowance is empty
    errors: int = 0
    sampled: int = 0
    exits: list = field(default_factory=list)
    detail: str = ""

    @property
    def exhausted(self) -> bool:
        return self.status == "exhausted"


@dataclass
class GuardReport:
    probe: Probe = field(default_factory=Probe)
    minutes_out: bool = False
    refusals_501: int = 0
    gb_left: float | None = None
    gb_allowance: float | None = None
    gb_low: bool = False
    burn_rate: float | None = None
    minutes_this_cycle: float = 0.0
    last_cycle_minutes: float | None = None
    cycle_fraction: float | None = None     # how far through the last cycle
    low_basis: str = ""                     # "gb" | "cycle" -- what warned
    stopped: list = field(default_factory=list)
    resumed: list = field(default_factory=list)
    sent: list = field(default_factory=list)

    def summary(self) -> str:
        gb = "unknown" if self.gb_left is None else f"{self.gb_left:.1f}GB"
        return (f"gateway={self.probe.status} minutes_out={self.minutes_out} "
                f"gb_left={gb} stopped={len(self.stopped)} sent={self.sent}")


# --- the gateway probe -----------------------------------------------------

def probe_gateway(profiles=None, samples: int = PROBE_SAMPLES,
                  token: str = "", logger=None) -> Probe:
    """Ask the proxy gateway whether the account can still buy traffic.

    Never raises: a probe that throws would take the guard down with it, and a
    guard that is down is exactly the state this exists to prevent. Anything it
    cannot establish comes back as `unknown`, which stops nothing.
    """
    import requests

    probe = Probe()
    try:
        if profiles is None:
            from adb_bot.clients.multilogin.mobile_list import (
                MultiloginMobileListClient)
            token = token or os.environ.get("MULTILOGIN_TOKEN", "")
            if not token:
                probe.detail = "no MULTILOGIN_TOKEN"
                return probe
            profiles = MultiloginMobileListClient(token).list_mobile_profiles()
    except Exception as exc:                                   # noqa: BLE001
        probe.detail = f"could not list profiles ({type(exc).__name__}: {exc})"[:200]
        return probe

    usable = [p for p in (profiles or [])
              if (p.get("proxy") or {}).get("type") == PROBE_TYPE
              and (p.get("proxy") or {}).get("username")]
    if not usable:
        probe.detail = f"no {PROBE_TYPE}-type proxies to probe"
        return probe

    for item in usable[:samples]:
        proxy = item["proxy"]
        url = (f"http://{proxy['username']}:{proxy['password']}"
               f"@{proxy['server']}:{proxy['port']}")
        probe.sampled += 1
        try:
            response = requests.get(PROBE_URL, proxies={"http": url},
                                    timeout=PROBE_TIMEOUT)
        except Exception as exc:                               # noqa: BLE001
            probe.errors += 1
            _log(logger, "info", "mlx_guard: probe %s errored (%s)",
                 item.get("serial_name", "?"), type(exc).__name__)
            continue
        if response.status_code == 402:
            probe.paid += 1
        elif response.status_code == 200:
            probe.ok += 1
            try:
                body = response.json()
                probe.exits.append(f"{body.get('city')}/{body.get('query')}")
            except Exception:                                  # noqa: BLE001
                pass
        else:
            probe.errors += 1

    # An account that can still serve one request is not empty, whatever else
    # failed -- so a single success outranks any number of 402s.
    if probe.ok:
        probe.status = "ok"
    elif probe.paid >= MIN_402_TO_STOP:
        probe.status = "exhausted"
    probe.detail = (f"{probe.sampled} probed: {probe.ok} ok, "
                    f"{probe.paid} x 402, {probe.errors} error")
    return probe


# --- the minutes signal ----------------------------------------------------

def refusals_501(window_minutes: int = MINUTES_501_WINDOW_MINUTES,
                 now=None, path=None) -> int:
    """How many times the cloud refused to start profiles with a 501, recently.

    Read from the tail of today's launcher log rather than the whole day, so a
    morning outage that was already topped up does not keep the fleet stopped
    all afternoon.
    """
    now = now or datetime.now()
    log = mlx_minutes.log_dir(path) / f"launcher_{now.strftime('%Y%m%d')}.log"
    cutoff = now - timedelta(minutes=window_minutes)
    count = 0
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        match = mlx_minutes._START_REFUSED.search(line)
        if not match or match.group(1) != "501":
            continue
        try:
            when = datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if when >= cutoff:
            count += 1
    return count


# --- the gigabyte estimate -------------------------------------------------

def minutes_since(when, now=None, path=None) -> float:
    """Billable phone-minutes since `when`.

    Sessions are paired the same way `mlx_minutes` pairs them, but clipped to
    the window: a session that straddles the top-up contributes only the part
    after it. Deliberately an undercount -- a session the launcher never closed
    contributes nothing rather than a guess.
    """
    now = now or datetime.now()
    total = 0.0
    day = when.date()
    while day <= now.date():
        log = mlx_minutes.log_dir(path) / f"launcher_{day.strftime('%Y%m%d')}.log"
        open_at: dict = {}
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError:
            day += timedelta(days=1)
            continue
        for line in lines:
            match = mlx_minutes._START.match(line)
            if match:
                open_at[match.group(2)] = mlx_minutes._stamp(match.group(1))
                continue
            match = mlx_minutes._FINISH.match(line)
            if match and match.group(2) in open_at:
                began = open_at.pop(match.group(2))
                ended = mlx_minutes._stamp(match.group(1))
                began = max(began, when)
                if ended > began:
                    total += (ended - began).total_seconds() / 60
        day += timedelta(days=1)
    return total


def _env_float(name: str):
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def estimate_gb(state: dict, now=None, path=None):
    """(gb_left, allowance, burn_rate, minutes_this_cycle).

    `gb_left` is None whenever it would be a guess dressed as a number: no
    allowance configured, no known top-up to count from, or no rate yet.
    """
    now = now or datetime.now()
    allowance = _env_float("MLX_PROXY_GB_ALLOWANCE")
    rate = _env_float("MLX_PROXY_GB_PER_MINUTE") or state.get("gb_per_minute")

    started = state.get("topped_up_at")
    if not started:
        return None, allowance, rate, 0.0
    try:
        since = datetime.fromisoformat(started)
    except ValueError:
        return None, allowance, rate, 0.0

    minutes = minutes_since(since, now=now, path=path)
    if allowance is None or not rate:
        return None, allowance, rate, minutes
    return max(0.0, allowance - minutes * rate), allowance, rate, minutes


def calibrate(state: dict, minutes_this_cycle: float):
    """Turn a cycle that just ended into a GB-per-minute rate.

    Called only on the 200 -> 402 edge, when the allowance is known to have gone
    from full to empty over exactly `minutes_this_cycle` of phone time. Each
    outage therefore makes the next warning more accurate, which is the one good
    thing about having had several.
    """
    allowance = _env_float("MLX_PROXY_GB_ALLOWANCE")
    if not allowance or minutes_this_cycle <= 0:
        return None
    return allowance / minutes_this_cycle


# --- stopping and resuming -------------------------------------------------

def guard_timers() -> list:
    raw = (os.environ.get("ADBBOT_GUARD_TIMERS") or "").strip()
    return raw.split() if raw else list(DEFAULT_TIMERS)


def _systemctl(*args, logger=None) -> bool:
    binary = shutil.which("systemctl")
    if not binary:
        _log(logger, "warning", "mlx_guard: no systemctl on PATH")
        return False
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True,
                              timeout=60)
    except Exception as exc:                                   # noqa: BLE001
        _log(logger, "warning", "mlx_guard: systemctl %s failed (%s)",
             " ".join(args), exc)
        return False
    if done.returncode != 0:
        _log(logger, "warning", "mlx_guard: systemctl %s -> %s %s",
             " ".join(args), done.returncode, (done.stderr or "").strip()[:200])
    return done.returncode == 0


def _is_enabled(unit: str) -> str:
    binary = shutil.which("systemctl") or "systemctl"
    try:
        done = subprocess.run([binary, "is-enabled", unit],
                              capture_output=True, text=True, timeout=30)
    except Exception:                                          # noqa: BLE001
        return "unknown"
    return (done.stdout or "").strip()


def stop_burners(dry_run: bool = False, logger=None) -> list:
    """Disable every launching loop. Returns the ones that were still enabled.

    Idempotent on purpose: this runs every tick for as long as the outage lasts,
    and only the units it actually changed are reported, so the alert does not
    claim to have stopped an already-stopped fleet.
    """
    changed = []
    for unit in guard_timers():
        if _is_enabled(unit) != "enabled":
            continue
        changed.append(unit)
        if not dry_run:
            _systemctl("disable", "--now", unit, logger=logger)
            # The oneshot behind the timer may be mid-run with phones open.
            # Stopping it leaves it `failed` on SIGTERM, which is cosmetic but
            # blocks a clean start later, so clear it here rather than leaving
            # the next person to find it.
            service = unit.replace(".timer", ".service")
            _systemctl("stop", service, logger=logger)
            _systemctl("reset-failed", service, logger=logger)
    return changed


def resume_burners(units, dry_run: bool = False, logger=None) -> list:
    """Re-enable exactly the units the guard stopped, and nothing else.

    Only ever called with the list this guard recorded when it stopped them --
    never `DEFAULT_TIMERS` -- so a loop somebody switched off deliberately
    (warm-up has been off since 2026-08-20) stays off.
    """
    back = []
    for unit in units or []:
        back.append(unit)
        if not dry_run:
            _systemctl("enable", "--now", unit, logger=logger)
    return back


# --- the messages ----------------------------------------------------------

def _units(units) -> str:
    return ", ".join(u.replace("adbbot-", "").replace(".timer", "")
                     for u in units) or "nothing (already stopped)"


def proxy_out_message(report: GuardReport) -> str:
    lines = [
        "\U0001f6d1 <b>Out of MultiLogin proxy traffic</b>",
        f"The gateway answered <b>402 Payment Required</b> "
        f"({report.probe.detail}).",
        f"Stopped: <b>{_units(report.stopped)}</b>.",
        "",
        "Every launch from here fails as <code>45010 Proxy connection "
        "failed</code> with nothing saying why, and profiles pick up "
        "<code>Retries Exhausted</code> tags for a fault that is not theirs.",
        "Top up <b>proxy traffic</b> in the MultiLogin dashboard.",
    ]
    if report.minutes_this_cycle:
        lines.insert(3, f"This top-up lasted "
                        f"{report.minutes_this_cycle:,.0f} phone-minutes.")
    return "\n".join(lines)


def proxy_low_message(report: GuardReport) -> str:
    """Telegram HTML for the running-low warning.

    Two shapes, because the warning has two possible bases and saying which one
    it used is the difference between a number somebody can act on and a number
    they have to come and ask about.
    """
    lines = ["⚠️ <b>MultiLogin proxy traffic is running low</b>"]
    if report.low_basis == "gb":
        rate = (f"~{report.burn_rate * 1024:.0f} MB/minute" if report.burn_rate
                else "an unknown rate")
        lines += [
            f"About <b>{report.gb_left:.1f} GB</b> left of "
            f"{report.gb_allowance:,.0f} GB.",
            f"Burned {report.minutes_this_cycle:,.0f} phone-minutes this top-up "
            f"at {rate}.",
            "",
            "This is an <b>estimate</b> -- MultiLogin exposes no traffic API, "
            "so it is phone-minutes times a rate calibrated on the last outage.",
        ]
    else:
        lines += [
            f"This top-up has burned <b>{report.minutes_this_cycle:,.0f}</b> "
            f"phone-minutes -- <b>{report.cycle_fraction * 100:.0f}%</b> of the "
            f"{report.last_cycle_minutes:,.0f} the last one lasted before it "
            f"ran dry.",
            "",
            "No GB figure is configured, so this compares against the previous "
            "cycle rather than the allowance. It is coarser -- a quiet cycle "
            "and a busy one buy the same minutes but spend different traffic. "
            "Set <code>MLX_PROXY_GB_ALLOWANCE</code> in "
            "<code>/etc/adbbot/env</code> for the real number.",
        ]
    lines += ["Check the dashboard before topping up.",
              "When it runs out the fleet stops itself and you get a second "
              "message."]
    return "\n".join(lines)


def proxy_back_message(report: GuardReport) -> str:
    lines = [
        "✅ <b>MultiLogin proxy traffic is back</b>",
        f"The gateway is answering again ({report.probe.detail}).",
    ]
    if report.probe.exits:
        lines.append(f"Exits: {', '.join(report.probe.exits[:3])}.")
    if report.resumed:
        lines.append(f"Restarted: <b>{_units(report.resumed)}</b>.")
    else:
        lines.append("The loops are still <b>stopped</b> -- restart them with "
                     "<code>systemctl enable --now adbbot-posting.timer "
                     "adbbot-recheck.timer</code>.")
    return "\n".join(lines)


def minutes_out_message(report: GuardReport) -> str:
    return "\n".join([
        "\U0001f6d1 <b>Out of MultiLogin minutes</b>",
        f"MultiLogin refused to start profiles <b>{report.refusals_501}</b> "
        f"times with HTTP 501 in the last "
        f"{MINUTES_501_WINDOW_MINUTES} minutes, and the proxy gateway is fine "
        f"-- so this is phone time, not traffic.",
        f"Stopped: <b>{_units(report.stopped)}</b>.",
        "",
        "Top up <b>minutes</b> in the MultiLogin dashboard.",
    ])


# --- the loop --------------------------------------------------------------

def run_check(logger=None, notifier=None, now=None, state_path=None,
              profiles=None, dry_run: bool = False) -> GuardReport:
    """One tick. Never raises."""
    now = now or datetime.now()
    report = GuardReport()
    state = mlx_minutes._load_state(state_path or STATE_FILE)
    alerts_state = state.setdefault("alerts", {})
    was = state.get("gateway", "unknown")

    report.probe = probe_gateway(profiles=profiles, logger=logger)
    _log(logger, "info", "mlx_guard: gateway %s (%s)",
         report.probe.status, report.probe.detail)

    (report.gb_left, report.gb_allowance, report.burn_rate,
     report.minutes_this_cycle) = estimate_gb(state, now=now)

    # Minutes only count as exhausted while the gateway is *not* the problem.
    # Both outages stop every launch, and blaming the wrong one sends the top-up
    # to the wrong product.
    if not report.probe.exhausted:
        report.refusals_501 = refusals_501(now=now)
        report.minutes_out = report.refusals_501 >= MINUTES_501_THRESHOLD

    alerts = []

    if report.probe.exhausted:
        if was != "exhausted":
            # The 200 -> 402 edge: the cycle that just ended is a measurement.
            rate = calibrate(state, report.minutes_this_cycle)
            if rate:
                state["gb_per_minute"] = rate
                report.burn_rate = rate
            state["last_cycle_minutes"] = report.minutes_this_cycle
        state["gateway"] = "exhausted"
        report.stopped = stop_burners(dry_run=dry_run, logger=logger)
        if report.stopped:
            state["stopped_by_guard"] = sorted(
                set(state.get("stopped_by_guard") or []) | set(report.stopped))
        if mlx_minutes.should_alert("proxy_out", True, now=now,
                                    state=alerts_state):
            alerts.append(("proxy_out", proxy_out_message(report)))
    else:
        mlx_minutes.should_alert("proxy_out", False, now=now, state=alerts_state)
        if report.probe.status == "ok":
            if was == "exhausted":
                # Recovery. A fresh top-up starts a fresh cycle, so the minute
                # counter restarts here rather than at any wall-clock boundary.
                state["topped_up_at"] = now.isoformat()
                # Re-read the estimate against the *new* cycle before anything
                # judges it. The figure above was measured from the top-up that
                # just ran dry, so it is ~0 -- and leaving it would fire
                # "traffic is nearly gone" in the same tick as "traffic is
                # back", which is the sort of contradiction that gets an alert
                # channel muted.
                (report.gb_left, report.gb_allowance, report.burn_rate,
                 report.minutes_this_cycle) = estimate_gb(state, now=now)
                if (os.environ.get("ADBBOT_GUARD_AUTORESUME") or "").strip() == "1":
                    report.resumed = resume_burners(
                        state.get("stopped_by_guard"), dry_run=dry_run,
                        logger=logger)
                    if not dry_run:
                        state["stopped_by_guard"] = []
                alerts.append(("proxy_back", proxy_back_message(report)))
            elif not state.get("topped_up_at"):
                # First healthy tick on a box that has never seen one. Start
                # counting from now; the estimate is only ever as old as this.
                state["topped_up_at"] = now.isoformat()
            state["gateway"] = "ok"

    report.last_cycle_minutes = state.get("last_cycle_minutes")
    if report.last_cycle_minutes:
        report.cycle_fraction = (report.minutes_this_cycle
                                 / report.last_cycle_minutes)
    if report.gb_left is not None:
        # A real GB figure beats the proxy for one every time.
        report.gb_low = report.gb_left <= (_env_float("MLX_PROXY_GB_WARN_BELOW")
                                           or DEFAULT_GB_WARN_BELOW)
        report.low_basis = "gb"
    elif report.cycle_fraction is not None:
        report.gb_low = report.cycle_fraction >= (
            _env_float("MLX_PROXY_CYCLE_WARN_FRACTION")
            or DEFAULT_CYCLE_WARN_FRACTION)
        report.low_basis = "cycle"

    if mlx_minutes.should_alert("proxy_low",
                                report.gb_low and not report.probe.exhausted,
                                now=now, state=alerts_state):
        alerts.append(("proxy_low", proxy_low_message(report)))

    if report.minutes_out:
        report.stopped = report.stopped or stop_burners(dry_run=dry_run,
                                                        logger=logger)
        if report.stopped:
            state["stopped_by_guard"] = sorted(
                set(state.get("stopped_by_guard") or []) | set(report.stopped))
    if mlx_minutes.should_alert("minutes_out", report.minutes_out, now=now,
                                state=alerts_state):
        alerts.append(("minutes_out", minutes_out_message(report)))

    if not dry_run:
        mlx_minutes._save_state(state, state_path or STATE_FILE)

    if dry_run:
        report.sent = [name for name, _ in alerts]
        return report

    if alerts and notifier is None:
        from adb_bot.clients.telegram import TelegramNotifier
        notifier = TelegramNotifier()
    for name, body in alerts:
        category = ALERT_CATEGORIES.get(name, "")
        if notifier is not None and not notifier.allows(category):
            # Asked before sending so a deliberately narrowed channel does not
            # show up as a delivery failure in the log or in `report.sent`.
            _log(logger, "info", "mlx_guard: %s alert suppressed (%s not in "
                 "ADBBOT_TELEGRAM_ALERTS)", name, category)
            continue
        if notifier is not None and notifier.send(body, logger=logger,
                                                  category=category):
            report.sent.append(name)
        else:
            _log(logger, "warning", "mlx_guard: could not send %s alert", name)
    return report


def _log(logger, level: str, message: str, *args) -> None:
    if logger is not None:
        getattr(logger, level)(message, *args)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Stop the fleet when MultiLogin proxy traffic or minutes "
                    "run out, and say so on Telegram.")
    parser.add_argument("--dry-run", action="store_true",
                        help="probe and report, but stop nothing and send nothing")
    args = parser.parse_args(argv)

    from adb_bot.core.logger import get_logger
    report = run_check(logger=get_logger("adb_bot"), dry_run=args.dry_run)
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
