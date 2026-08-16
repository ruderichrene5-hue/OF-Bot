"""Alert when a scheduled loop stops producing while work is due.

Why this is a separate module from `heartbeat.py`
-------------------------------------------------
`ProfileHeartbeat` guards one phone inside one flow: it lives in memory for the
duration of a single `run_profile_workflow`, latches on the first failure, and
its whole output is a `should_stop` boolean. Nothing about it survives the
process, and each scheduled loop is its own short-lived process (`run_loop.py`
runs one loop once and exits). "Nothing was produced for thirty minutes" cannot
be observed from inside a run that lasts four -- it is a statement about a
*sequence* of runs, so it needs state on disk and a different lifetime. Bolting
it onto the heartbeat would have meant one class with two clocks, two scopes and
two meanings of "stopped". They stay separate.

The distinction that matters: IDLE vs STALLED
---------------------------------------------
Every silent failure on the night of 2026-08-04 -- a dead MultiLogin agent,
profile locks left behind by `systemctl stop`, a queue nobody filled -- looked
exactly like a quiet night in the logs. Both produce zero posts. The difference
is whether anything was *owed*:

    due == 0                   -> IDLE     (genuinely quiet; never an alert)
    due > 0, produced > 0      -> OK
    due > 0, produced == 0,
      for less than the grace  -> WAITING  (one bad tick is not an outage)
    due > 0, produced == 0,
      for longer than that     -> STALLED  (alert)

So every observation carries both numbers, and both come from bookkeeping that
already exists -- the Posting Queue and the post ledger for posting, the queue /
pipeline / recheck runners' own reports for the rest. Nothing here counts
anything that was not already being counted.

Alert storms
------------
An alert fires on the *transition* into STALLED, and then at most once per
:data:`DEFAULT_RENOTIFY_SECONDS` (one hour) while it persists -- so a loop that
is down all night produces roughly one line an hour, not one per tick. It clears
the moment production resumes, with a matching "recovered" notice so whoever saw
the alert also sees it end.

Where alerts land
-----------------
This deployment has no notification channel: `/etc/adbbot/env` carries
credentials only (MultiLogin, Airtable, Drive, spoofer paths) and there is no
Slack/webhook/SMTP configuration anywhere in the project. Rather than add an
external dependency, alerts go to the three places a human here already looks:

1. a loud `ERROR` line in the loop's own log, plus a dedicated `logs/alerts.log`
   that holds nothing else, so `tail -f logs/alerts.log` is the whole story;
2. a structured state file per loop under ``<app data>/watchdog/``, plus an
   append-only ``loop_alerts.jsonl`` history -- machine-readable, and what
   `doctor` reads to surface a live stall;
3. optionally an Airtable **Run Log** row (the existing table every other runner
   writes its outcomes to), if the caller hands over a client.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from adb_bot.automation import schedule_spec
from adb_bot.config.settings import get_app_data_dir

# --- states ------------------------------------------------------------------
STATE_IDLE = "idle"          # nothing was due -- quiet is correct
STATE_OK = "ok"              # work was due and something came out
STATE_WAITING = "waiting"    # work is due, nothing yet, still inside the grace
STATE_STALLED = "stalled"    # work is due and nothing has come out for too long
STATE_UNHEALTHY = "unhealthy"  # a health probe (doctor) is failing checks

# States that mean "a human should look". `stalled` is about production, and
# `unhealthy` is about the setup the production depends on -- different
# questions, same answer for anyone reading a status line.
BAD_STATES = (STATE_STALLED, STATE_UNHEALTHY)

# --- alert kinds -------------------------------------------------------------
ALERT_STALLED = "stalled"        # first time a loop trips
ALERT_REMINDER = "reminder"      # still stalled, one re-notify interval later
ALERT_RECOVERED = "recovered"    # production resumed
ALERT_CLEARED = "cleared"        # the work stopped being due before it was done
ALERT_UNHEALTHY = "unhealthy"    # a health probe started failing, or failed anew
ALERT_HEALTHY = "healthy"        # every check passing again

# How long a loop may produce nothing while work is due before it is stalled.
# Derived from the loop's own cadence so the two cannot drift apart: three
# missed ticks, never less than half an hour. (posting runs every 5 min -> the
# 30-minute floor; queue/recheck every 15 -> 45 min; pipeline every 30 -> 90.)
MIN_STALL_AFTER_SECONDS = 30 * 60
TICKS_BEFORE_STALL = 3

# While a loop stays stalled, re-notify at most this often. Documented interval:
# one hour. Long enough that a night-long outage is ~8 lines, short enough that
# a stall started at 23:00 is still being reported at 02:00.
DEFAULT_RENOTIFY_SECONDS = 60 * 60

STATE_DIRNAME = "watchdog"
HISTORY_FILENAME = "loop_alerts.jsonl"
ALERT_LOG_FILE = "logs/alerts.log"

# What to look at first, per loop. An alert that only says "it stopped" makes
# the reader start from scratch every time; these are the three things that
# actually went wrong last night.
HINTS = {
    "posting": ("check the MultiLogin agent is up (ss -lntp | grep 45001), then stale "
                "profile locks (run_loop doctor), then logs/loop_posting.log"),
    "queue": ("the queue creates rows from Ready Spoof Variants -- check the pipeline "
              "loop is producing variants and logs/loop_queue.log"),
    "pipeline": ("check the spoofer (SPOOFER_PYTHON / SPOOFER_ROOT), disk space, and "
                 "logs/loop_pipeline.log"),
    "recheck": ("rows are parked in Verifying and not resolving -- check profiles can "
                "launch and that a baseline post count was captured (run_loop recheck "
                "without --apply lists them)"),
    "doctor": ("the failing check names say what to look at; full report and remedies "
               "from `run_loop doctor`, history in logs/loop_doctor.log"),
    "issue-tags": ("the flagged profiles are not being mirrored into MultiLogin, so "
                   "nobody sees them in the workspace -- check the MLX token "
                   "(MULTILOGIN_TOKEN in /etc/adbbot/env), that /tag/search still "
                   "returns an 'Issue' tag, and logs/loop_issue-tags.log"),
}


def stall_after_seconds(loop: str) -> float:
    """The grace period for one loop, from its scheduled cadence."""
    minutes = schedule_spec.RECOMMENDED_INTERVALS.get(
        loop, schedule_spec.FALLBACK_INTERVAL_MIN)
    return float(max(MIN_STALL_AFTER_SECONDS, TICKS_BEFORE_STALL * minutes * 60))


@dataclass
class LoopState:
    """What we remember about one loop between runs. Written as JSON."""

    loop: str = ""
    state: str = STATE_IDLE
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_due: int = 0
    last_produced: int = 0
    last_produced_at: float = 0.0    # 0 = has never produced anything yet
    stalled_since: float = 0.0       # 0 = not currently stalled OR unhealthy
    last_alert_at: float = 0.0
    alerts_sent: int = 0
    detail: str = ""
    # Which checks were failing at the last health observation, sorted and
    # joined. Only `observe_health` writes it. It exists so a *new* failure is
    # reported at once instead of hiding behind an older one for a whole
    # re-notify hour -- "MLX agent down" must not mask "Airtable auth rejected".
    health_signature: str = ""

    @property
    def stalled(self) -> bool:
        """Currently in a bad state -- stalled production, or a failing probe.

        One flag covers both because everything that reads it (the status line,
        `stalled_loops`, the re-notify budget) asks the same question: does a
        human need to look? `state` still says which of the two it is.
        """
        return bool(self.stalled_since)

    @property
    def unhealthy(self) -> bool:
        return self.state == STATE_UNHEALTHY and bool(self.stalled_since)


@dataclass
class Alert:
    """One thing worth telling a human about."""

    loop: str
    kind: str
    state: str
    due: int = 0
    produced: int = 0
    quiet_seconds: float = 0.0
    at: float = 0.0
    detail: str = ""
    hint: str = ""

    @property
    def severity(self) -> str:
        return "error" if self.kind in (ALERT_STALLED, ALERT_REMINDER, ALERT_UNHEALTHY) else "info"

    @property
    def message(self) -> str:
        minutes = self.quiet_seconds / 60.0
        if self.kind == ALERT_UNHEALTHY:
            head = (f"{self.loop.upper()} UNHEALTHY: {self.due} check(s) failing"
                    + (f", {self.produced} passing" if self.produced else ""))
        elif self.kind == ALERT_HEALTHY:
            head = (f"{self.loop} healthy again: all {self.produced} check(s) passing"
                    + (f" after {minutes:.0f} min failing" if minutes >= 1 else ""))
        elif self.kind == ALERT_STALLED:
            head = (f"{self.loop.upper()} STALLED: nothing produced in {minutes:.0f} min "
                    f"while {self.due} item(s) are due")
        elif self.kind == ALERT_REMINDER:
            head = (f"{self.loop.upper()} STILL STALLED: nothing produced in "
                    f"{minutes:.0f} min while {self.due} item(s) are due")
        elif self.kind == ALERT_RECOVERED:
            head = (f"{self.loop} recovered: {self.produced} item(s) produced after "
                    f"{minutes:.0f} min stalled")
        else:
            head = (f"{self.loop} no longer stalled: nothing is due any more "
                    f"(after {minutes:.0f} min with no production)")
        parts = [head]
        if self.detail:
            parts.append(self.detail)
        if self.hint and self.kind in (ALERT_STALLED, ALERT_REMINDER, ALERT_UNHEALTHY):
            parts.append(f"-> {self.hint}")
        return " | ".join(parts)


@dataclass
class Verdict:
    """The outcome of one observation, for the caller's own logging/tests."""

    loop: str
    state: str
    due: int = 0
    produced: int = 0
    quiet_seconds: float = 0.0
    alerts: list = field(default_factory=list)   # [Alert] actually delivered

    @property
    def stalled(self) -> bool:
        return self.state == STATE_STALLED

    @property
    def alerted(self) -> bool:
        return bool(self.alerts)


# --- delivery ----------------------------------------------------------------

def log_sink(logger):
    """Deliver an alert as a log line. Stalls are ERROR and deliberately loud:
    this is the line somebody grepping for trouble has to be unable to miss."""

    def deliver(alert: Alert) -> None:
        if logger is None:
            return
        try:
            if alert.severity == "error":
                logger.error("*** LOOP ALERT *** %s", alert.message)
            else:
                logger.info("*** LOOP ALERT CLEARED *** %s", alert.message)
        except Exception:
            pass

    return deliver


def jsonl_sink(path):
    """Append the alert to a history file. Append-only for the same reason the
    post ledger is: a torn write costs one line, not the file."""
    target = Path(path)

    def deliver(alert: Alert) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")
        except Exception:
            # Alerting must never be the thing that breaks a loop.
            pass

    return deliver


def airtable_run_log_sink(airtable, logger=None):
    """Write the alert to the Airtable Run Log -- the table every runner already
    reports outcomes to, so this adds a surface without adding a dependency.

    No Account link: a stalled loop belongs to no single account. `create_run_log`
    already supports that (profile-driven runs use it).
    """
    from adb_bot.clients import airtable as at

    def deliver(alert: Alert) -> None:
        try:
            result = at.RESULT_FAILED if alert.severity == "error" else at.RESULT_DONE
            airtable.create_run_log(None, f"Loop watchdog / {alert.loop}",
                                    f"watchdog/{alert.loop}", result, alert.message[:1000])
        except Exception as exc:
            if logger is not None:
                try:
                    logger.warning("watchdog: could not write the alert to Airtable: %s", exc)
                except Exception:
                    pass

    return deliver


def alert_logger():
    """A logger whose file holds alerts and nothing else, so `tail -f
    logs/alerts.log` is a complete picture of what needs attention."""
    from adb_bot.core.logger import get_logger

    return get_logger("adbbot.alerts", log_file=ALERT_LOG_FILE)


# --- the watchdog ------------------------------------------------------------

class LoopWatchdog:
    """Persistent per-loop production monitor.

    One state file per loop rather than one shared file: loops are separate
    processes on separate timers, and a shared document would need locking to
    survive two of them finishing at the same moment. A loop never runs
    concurrently with itself (systemd oneshot + timer), so per-loop files need no
    lock at all, and each write is atomic (temp file + replace).
    """

    def __init__(self, state_dir=None, sinks=None, logger=None,
                 renotify_seconds: float = DEFAULT_RENOTIFY_SECONDS,
                 history_path=None, now=None) -> None:
        self.state_dir = Path(state_dir) if state_dir else (get_app_data_dir() / STATE_DIRNAME)
        self.renotify_seconds = float(renotify_seconds)
        self._now = now or time.time
        self._logger = logger
        if sinks is None:
            history = (Path(history_path) if history_path
                       else self.state_dir.parent / HISTORY_FILENAME)
            sinks = [log_sink(logger), jsonl_sink(history)]
        self.sinks = list(sinks)

    # -- state -----------------------------------------------------------

    def _path(self, loop: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(loop))
        return self.state_dir / f"{safe}.json"

    def state_for(self, loop: str) -> LoopState:
        """The stored state, or a blank one. Read fresh every time -- another
        process wrote the previous tick."""
        try:
            with self._path(loop).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            known = {f for f in LoopState.__dataclass_fields__}
            return LoopState(**{k: v for k, v in data.items() if k in known})
        except Exception:
            # Missing, or corrupted by a torn write / hand edit. Starting over
            # only costs the grace period once.
            return LoopState(loop=str(loop))

    def _save(self, state: LoopState) -> bool:
        path = self._path(state.loop)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
            return True
        except Exception:
            return False

    def production_since(self, loop: str, now=None) -> float:
        """The timestamp a caller should measure "produced" from: the last time
        this loop was observed. A caller with a timestamped source (the post
        ledger) counts what landed after this; one that only knows its own run
        passes its own tally and this is unused.

        Falls back to one grace period ago when the loop has never been seen, so
        a first observation still looks at a sensible span rather than all of
        history.
        """
        state = self.state_for(loop)
        if state.last_seen:
            return state.last_seen
        return (now or self._now()) - stall_after_seconds(loop)

    # -- the ruling ------------------------------------------------------

    def observe(self, loop: str, due, produced, detail: str = "", now=None,
                grace_seconds=None) -> Verdict:
        """Record one tick of a loop and decide whether to alert.

        `due` is how much work was owed, `produced` is how much came out since
        the previous observation. Both are counts from sources that already
        exist; see the `observe_*` helpers below for where each loop's come from.
        """
        now = float(now if now is not None else self._now())
        due = int(due or 0)
        produced = int(produced or 0)
        grace = float(grace_seconds if grace_seconds is not None else stall_after_seconds(loop))

        state = self.state_for(loop)
        state.loop = str(loop)
        if not state.first_seen:
            state.first_seen = now
        state.last_seen = now
        state.last_due = due
        state.last_produced = produced
        state.detail = detail

        alerts: list = []
        # Quiet time is measured from the last thing this loop produced -- or,
        # if it has never produced anything, from when we started watching. That
        # start-up allowance is what stops a freshly deployed server (or a pruned
        # ledger) from alerting before it has had a chance to do anything.
        since = state.last_produced_at or state.first_seen
        quiet = max(0.0, now - since)

        if produced > 0:
            state.last_produced_at = now
            if state.stalled:
                alerts.append(self._alert(state, ALERT_RECOVERED, STATE_OK, due, produced,
                                          now - state.stalled_since, detail, now))
                self._clear(state)
            state.state = STATE_OK
            quiet = 0.0
        elif due <= 0:
            # Genuinely quiet. Not an alert, ever -- this is the whole point of
            # the module. A stall that ends here ended because the work stopped
            # being owed (rows failed out, the slot passed), not because anything
            # was produced, so it is reported as CLEARED rather than RECOVERED.
            if state.stalled:
                alerts.append(self._alert(state, ALERT_CLEARED, STATE_IDLE, due, produced,
                                          now - state.stalled_since, detail, now))
                self._clear(state)
            state.state = STATE_IDLE
        elif quiet >= grace:
            state.state = STATE_STALLED
            if not state.stalled:
                state.stalled_since = now
                alerts.append(self._alert(state, ALERT_STALLED, STATE_STALLED, due, produced,
                                          quiet, detail, now))
                state.last_alert_at = now
                state.alerts_sent += 1
            elif now - state.last_alert_at >= self.renotify_seconds:
                alerts.append(self._alert(state, ALERT_REMINDER, STATE_STALLED, due, produced,
                                          quiet, detail, now))
                state.last_alert_at = now
                state.alerts_sent += 1
            # else: still stalled, still inside the re-notify interval -- the
            # state file records it, but nobody is told again.
        else:
            state.state = STATE_WAITING

        self._save(state)
        verdict = Verdict(loop=str(loop), state=state.state, due=due, produced=produced,
                          quiet_seconds=quiet, alerts=alerts)
        for alert in alerts:
            self._deliver(alert)
        return verdict

    def observe_health(self, loop: str, failures, checked: int = 0, detail: str = "",
                       now=None) -> Verdict:
        """Record one run of a health probe (`doctor`) and decide whether to alert.

        The production side of this module asks "was work due, did any come
        out?". A probe cannot be phrased that way -- nothing is *owed*, checks
        simply pass or fail -- so it gets its own entry point rather than a
        contrived due/produced mapping. Everything after the decision is shared:
        the same state file, the same sinks, the same re-notify budget.

        `failures` is the failing check names. Alerts fire on the transition into
        failing, whenever the *set* of failing checks changes, and once per
        re-notify interval while it persists.
        """
        now = float(now if now is not None else self._now())
        failures = [str(f) for f in (failures or [])]
        checked = int(checked or 0)
        passing = max(0, checked - len(failures))
        signature = "|".join(sorted(failures))

        state = self.state_for(loop)
        state.loop = str(loop)
        if not state.first_seen:
            state.first_seen = now
        state.last_seen = now
        state.last_due = len(failures)
        state.last_produced = passing
        state.detail = detail

        alerts: list = []
        if not failures:
            state.last_produced_at = now
            if state.stalled:
                alerts.append(self._alert(state, ALERT_HEALTHY, STATE_OK, 0, passing,
                                          now - state.stalled_since, detail, now))
                self._clear(state)
            state.state = STATE_OK
            state.health_signature = ""
        else:
            changed = signature != state.health_signature
            state.health_signature = signature
            state.state = STATE_UNHEALTHY
            if not state.stalled:
                state.stalled_since = now
                alerts.append(self._alert(state, ALERT_UNHEALTHY, STATE_UNHEALTHY,
                                          len(failures), passing, 0.0, detail, now))
                state.last_alert_at = now
                state.alerts_sent += 1
            elif changed or now - state.last_alert_at >= self.renotify_seconds:
                # A changed signature re-alerts immediately and deliberately: it
                # is new information, and waiting out the hour would hide it.
                kind = ALERT_UNHEALTHY if changed else ALERT_REMINDER
                alerts.append(self._alert(state, kind, STATE_UNHEALTHY, len(failures),
                                          passing, now - state.stalled_since, detail, now))
                state.last_alert_at = now
                state.alerts_sent += 1

        self._save(state)
        verdict = Verdict(loop=str(loop), state=state.state, due=len(failures),
                          produced=passing,
                          quiet_seconds=(now - state.stalled_since) if state.stalled else 0.0,
                          alerts=alerts)
        for alert in alerts:
            self._deliver(alert)
        return verdict

    def _clear(self, state: LoopState) -> None:
        state.stalled_since = 0.0
        state.last_alert_at = 0.0
        state.alerts_sent = 0
        state.health_signature = ""

    def _alert(self, state: LoopState, kind: str, new_state: str, due: int, produced: int,
               quiet: float, detail: str, now: float) -> Alert:
        return Alert(loop=state.loop, kind=kind, state=new_state, due=due, produced=produced,
                     quiet_seconds=max(0.0, quiet), at=now, detail=detail,
                     hint=HINTS.get(state.loop, ""))

    def _deliver(self, alert: Alert) -> None:
        for sink in self.sinks:
            try:
                sink(alert)
            except Exception:
                # One broken sink must not swallow the others, and no sink may
                # take a loop down.
                pass

    # -- reading it back -------------------------------------------------

    def snapshot(self) -> dict:
        """loop -> LoopState for everything being watched."""
        out: dict = {}
        try:
            paths = sorted(self.state_dir.glob("*.json"))
        except Exception:
            return out
        for path in paths:
            state = self.state_for(path.stem)
            if state.last_seen:
                out[state.loop or path.stem] = state
        return out

    def stalled_loops(self) -> list:
        return [s for s in self.snapshot().values() if s.stalled]


# --- where each loop's "due" and "produced" come from ------------------------

def ledger_production_since(ledger, since: float) -> int:
    """How many reels were actually sent after `since`, per the post ledger.

    The ledger is the honest measure of posting production: it is written the
    instant Share is tapped, before verification and independently of Airtable,
    so it counts what the phones really did. A run that launched nothing (dead
    agent), locked nothing (stale locks) or had nothing to do produces no
    entries at all -- which is exactly the signal wanted here.
    """
    try:
        records = ledger.load().values()
    except Exception:
        return 0
    return sum(1 for r in records if (r.shared_at or 0.0) > float(since))


def due_posting_rows(airtable, now=None) -> int:
    """Posting Queue rows still Pending whose slot has passed.

    Read *after* the posting run on purpose: what matters is unfinished due
    work. A healthy run leaves none behind (the rows moved to Posted/Verifying),
    a stalled one leaves them all, and an empty queue reads zero either way.
    """
    from datetime import datetime

    from adb_bot.automation.posting_planner import _is_due
    from adb_bot.clients import airtable as at

    moment = now or datetime.now()
    rows = airtable.list_pending_posts() or []
    return sum(1 for row in rows
               if _is_due((row.get("fields", {}) or {}).get(at.F_PQ_SCHEDULED), moment))


def observe_posting(watchdog: LoopWatchdog, airtable, ledger=None, now=None,
                    logger=None) -> Verdict | None:
    """Tick the posting loop. Returns None when the observation could not be
    taken (an Airtable read failed) -- our own blindness is not the loop's
    stall, and the loop logs that failure itself.
    """
    from adb_bot.automation import post_ledger

    store = ledger if ledger is not None else post_ledger.PostLedger()
    since = watchdog.production_since("posting", now=now)
    try:
        due = due_posting_rows(airtable)
    except Exception as exc:
        if logger is not None:
            logger.warning("watchdog: could not count due Posting Queue rows: %s", exc)
        return None
    produced = ledger_production_since(store, since)
    return watchdog.observe("posting", due, produced, now=now,
                            detail=f"{produced} share(s) in the ledger since the last tick")


# The two reasons queue_runner gives a target for having no row: it is out of
# Ready variants, or its variants are held by a row already in flight. Both mean
# "a slot came round for this target and produced nothing", which is due work.
# Other skips (Ready variants belonging to an *ineligible* target) are not: no
# row was ever owed for them, and counting those would have the queue read as
# permanently due whenever a paused account holds content.
QUEUE_STARVED_MARKERS = ("no unused Ready Spoof Variant",
                         "belong to an existing")


def observe_queue(watchdog: LoopWatchdog, report, now=None) -> Verdict:
    """Tick the queue loop from the report `run_queue_slots` already returns.

    Due = the rows it planned. Produced = rows actually written. Before the
    first slot of the day nothing is due, so the quiet night reads IDLE.

    **Starved targets are deliberately not "due"** (fixed 2026-08-05, after this
    alerted for real). The queue cannot write a row without a Ready variant, so
    a target with no content is work it is *unable* to do, not work it failed to
    do -- counting it made the loop permanently STALLED because 45 unused "Blank
    (N)" staging profiles and one model with no raw videos will never have
    content. An alert that can only be cleared by deleting rows in Airtable is
    an alert people learn to ignore.

    Running dry is still worth knowing, so it stays in `detail`, and the report
    page shows the Ready-variant stock directly. What this loop is judged on is
    the thing it controls: rows it decided to write, actually written.
    """
    starved = sum(1 for _, reason in (report.skipped or [])
                  if any(marker in str(reason) for marker in QUEUE_STARVED_MARKERS))
    planned = len(report.planned or [])
    produced = int(report.rows_created or 0)
    return watchdog.observe("queue", planned, produced, now=now,
                            detail=f"{planned} planned, {starved} target(s) "
                                   f"with an unserved slot and no content")


def observe_pipeline(watchdog: LoopWatchdog, report, now=None) -> Verdict:
    """Tick the spoof pipeline from its own report.

    Due = raw videos this run picked up (already-processed ones are skipped
    upstream, so this is genuinely new work). Produced = variants encoded. No
    new raw video is not a failure -- that is the "check raw video stock" job on
    somebody's list, and it reads IDLE.

    **An unroutable clip is due work** (added 2026-08-10). A raw folder whose
    model has no target contributes nothing to `processed_videos` and nothing to
    `variants_created`, so a folder full of fresh clips going nowhere read as
    IDLE -- "no new raw video" -- which is the opposite of what happened. It
    means a model was set up in Drive and never finished in Airtable/MultiLogin,
    and finishing it clears the alert.

    Two corrections to how that was first described (2026-08-10, review):

    * It does **not** make "a folder of clips going nowhere read STALLED".
      `LoopWatchdog.observe` short-circuits to STATE_OK the moment `produced >
      0`, so unroutable clips only move the state while the WHOLE run encodes
      zero variants. On a fleet that is spoofing for anyone at all, the effect is
      the detail line below, not a state change -- which is the intended
      loudness for "one model is half onboarded".
    * It is therefore *mostly* not the permanent condition `observe_queue`'s
      starved targets were (reverted 2026-08-05) -- but not never. A **parked**
      model (Status Inactive on every profile, a documented ops action) whose
      clips stay in Drive is unroutable for as long as it is parked, so a quiet
      run that produces nothing at all reads STALLED until somebody moves the
      clips out. Known and accepted while it stays a detail line; if that ever
      pages anyone, the fix is for the pipeline to report parked-model skips as
      their own reason and for this to stop counting them as due -- doctor's
      `raw_folder_model_parked` finding already makes the distinction.
    """
    from adb_bot.automation.spoof_pipeline import UNROUTABLE_SKIP_MARKERS

    unroutable = sum(1 for _, reason in (report.skipped or [])
                     if any(marker in str(reason) for marker in UNROUTABLE_SKIP_MARKERS))
    due = len(report.processed_videos or []) + unroutable
    produced = int(report.variants_created or 0)
    detail = f"{len(report.processed_videos or [])} raw video(s) picked up, {produced} variant(s) encoded"
    if unroutable:
        detail += (f", {unroutable} clip(s) under a model with no profile to spoof for "
                   f"(see the 'pipeline: skipped' lines for which model)")
    return watchdog.observe("pipeline", due, produced, now=now, detail=detail)


def observe_recheck(watchdog: LoopWatchdog, tally, now=None) -> Verdict:
    """Tick the recheck loop from the tally `recheck_pending_posts` returns.

    Due = every parked row the pass looked at. Produced = the rows that actually
    left `Verifying` (posted / failed / abandoned). `unknown` is explicitly not
    production: "recheck returned unknown forever" was a real bug -- three
    passes, three unknowns, no phone ever launched -- and it is invisible to any
    counter that only asks whether the pass ran.
    """
    tally = tally or {}
    resolved = sum(int(tally.get(k, 0) or 0)
                   for k in ("posted", "failed", "abandoned", "account_absent"))
    unknown = int(tally.get("unknown", 0) or 0)
    due = resolved + unknown
    return watchdog.observe("recheck", due, resolved, now=now,
                            detail=f"{resolved} resolved, {unknown} still unknown")


ISSUE_TAGS_WATCHDOG_LOOP = "issue-tags"


def observe_issue_tags(watchdog: LoopWatchdog, report, now=None) -> Verdict:
    """Tick the issue-tag mirror from the report `sync_issue_tags` returns.

    Health, not production (`observe_health`, the way `doctor` reports). This is
    a reconciler: on a settled fleet a correct tick changes *nothing*, so the
    due/produced question has no honest answer here -- zero changes is the
    healthy steady state, and phrasing it as production would either alert on a
    quiet day or never alert at all.

    What can go wrong is enumerated instead: `report.error_kinds` carries stable
    labels (`no-mlx-client`, `empty-mlx-inventory`, `issue-tag-lookup`,
    `airtable-read`, `mlx-write`, ...) rather than error strings with profile
    names in them, so the alert fires on the *kind* of failure and does not
    re-fire every time a different profile is the one failing. A clean tick
    clears it, with the usual `recovered` notice.

    Without this the loop was invisible: nothing about a 15-minute pass that
    silently stopped mirroring flags would have reached a person, which is the
    same shape as the failure the feature exists to fix.
    """
    kinds = list(getattr(report, "error_kinds", None) or [])
    if getattr(report, "errors", None) and not kinds:
        kinds = ["error"]
    detail = "; ".join(str(e) for e in (report.errors or [])[:3]) or report.summary()
    return watchdog.observe_health(ISSUE_TAGS_WATCHDOG_LOOP, kinds,
                                   checked=int(getattr(report, "checked", 0) or 0),
                                   detail=detail, now=now)


def build_watchdog(logger=None, airtable=None) -> LoopWatchdog:
    """The watchdog the scheduled loops use: loud in the loop's own log, loud in
    `logs/alerts.log`, recorded in the state files, and -- when a client is
    handed over -- in the Airtable Run Log too.
    """
    history = get_app_data_dir() / HISTORY_FILENAME
    sinks = [log_sink(logger), log_sink(alert_logger()), jsonl_sink(history)]
    if airtable is not None:
        sinks.append(airtable_run_log_sink(airtable, logger))
    return LoopWatchdog(sinks=sinks, logger=logger)


def format_status(states) -> str:
    """One line per watched loop, for `doctor` and for a human at a prompt."""
    if not states:
        return "no loop has reported to the watchdog yet"
    lines = []
    for name in sorted(states):
        state = states[name]
        age = max(0.0, time.time() - (state.last_seen or 0.0)) / 60.0
        line = (f"{name:<10} {state.state:<8} due={state.last_due} "
                f"produced={state.last_produced} last seen {age:.0f} min ago")
        if state.stalled:
            quiet = max(0.0, time.time() - (state.last_produced_at or state.first_seen)) / 60.0
            line += f" -- STALLED, nothing produced for {quiet:.0f} min"
        lines.append(line)
    return "\n".join(lines)
