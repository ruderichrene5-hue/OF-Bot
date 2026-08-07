"""Watch the two-account phones: notice when they start posting, then keep
checking that they are posting *correctly*.

Two accounts on one phone are the one part of this bot whose failures are
quiet. Everywhere else a broken thing stops: a phone that will not launch files
an error, a clip that will not encode is reported. Here the failure modes all
look like success from a distance --

- the second account is configured and simply never scheduled: the phone posts,
  reports Active, and its row on every other table looks healthy;
- both accounts are handed the *same* spoofed clip, which is duplicate content
  of the most detectable kind, and both rows say Posted;
- the phone gets stuck on the second account, so the first account's posts go
  out on the second one -- again, both rows say Posted;
- the recheck reads the wrong account's post count, "disproves" a live post,
  and the reel is queued a second time.

None of those raise. So this module goes looking for them.

It runs in two phases, because "not working yet" and "broken" deserve very
different noise:

- **Before the first second-account post** the checks report progress through
  the chain (handles -> spoofed video -> queue row -> post) and only alert on a
  real misconfiguration, or on a stage that has been stuck for
  :data:`STUCK_STAGE_SECONDS`. A fleet that has simply not got there yet is not
  a fault.
- **After it** the correctness checks above become live, and a regression alerts
  through the same watchdog every other loop reports to.

The moment of the first post is recorded in a small state file so the phase is
stable across runs and so "when did this start working?" has an answer.

:func:`evaluate` is pure -- it takes pre-fetched rows and returns the verdict --
so every rule here is testable without Airtable, a phone, or a log file.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.clients import airtable as at
from adb_bot.config.settings import get_app_data_dir

STATE_FILENAME = "second_account_watch.json"

# How long a fleet may sit at one stage of the chain (handles known, but no
# variant / no queue row / no post) before that counts as stuck rather than
# merely early. Comfortably longer than one pipeline+queue+posting cycle -- the
# pipeline is capped at 20 variants a run and works through a backlog, so a
# fleet-wide fan-out genuinely takes a few hours.
STUCK_STAGE_SECONDS = 6 * 3600

# Once posting has started, how long the second accounts may go without a post
# before something is wrong. Longer than the widest posting gap (a flexible
# model posts at most every two hours, and a scheduled one may have a long
# overnight gap), so this fires on a real stop rather than on a quiet evening.
SILENT_SECONDS = 14 * 3600

# How far back the log is searched for the flow's own account-switch refusals.
LOG_WINDOW_SECONDS = 24 * 3600

# What the flow and the probe say when they refuse to post because they could
# not put the phone on the right account. Matched as plain substrings: these are
# the sentences those two call sites emit, and a switch that fails silently is
# the one thing this module cannot detect for itself.
SWITCH_FAILURE_MARKERS = (
    "could not prove it is signed in as",
    "does not list @",
    "gave up switching",
    "could not switch to @",
)


@dataclass
class Check:
    """One question with a yes/no answer and a sentence explaining it."""

    name: str
    ok: bool
    detail: str
    # A check that cannot be answered yet (the fleet has not got that far) is
    # neither a pass nor a failure -- reporting it as either would make the
    # first hours of a rollout unreadable.
    pending: bool = False


@dataclass
class WatchReport:
    checks: list = field(default_factory=list)
    phones: int = 0            # phones ticked as carrying two accounts
    usable: int = 0            # ...of those, with both handles known
    second_variants: int = 0   # Ready clips waiting for a second account
    second_rows: int = 0       # queue rows ever created for a second account
    second_posted: int = 0     # ...of those, confirmed Posted
    started: bool = False      # has a second account ever posted?
    first_post_at: float = 0.0
    switch_failures: int = 0

    @property
    def failures(self) -> list:
        return [c.name for c in self.checks if not c.ok and not c.pending]

    @property
    def checked(self) -> int:
        """Checks that actually returned an answer. A pending one is not a pass;
        counting it as one would report a fleet that has done nothing as fully
        healthy."""
        return len([c for c in self.checks if not c.pending])

    def summary(self) -> str:
        if not self.phones:
            return "no phone carries a second account"
        stage = ("posting" if self.started else "not posting yet")
        return (f"phones={self.phones} usable={self.usable} variants={self.second_variants} "
                f"rows={self.second_rows} posted={self.second_posted} ({stage}) "
                f"failing={len(self.failures)}/{self.checked}")


def _state_path(app_dir=None) -> Path:
    return Path(app_dir or get_app_data_dir()) / STATE_FILENAME


def load_state(app_dir=None) -> dict:
    """The watch's own memory: when it first saw the fleet, and when a second
    account first posted. Unreadable or absent reads as empty -- this file is a
    convenience, and losing it must not turn into an alert."""
    try:
        return json.loads(_state_path(app_dir).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_state(state: dict, app_dir=None) -> bool:
    try:
        path = _state_path(app_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def count_switch_failures(text: str, markers=SWITCH_FAILURE_MARKERS) -> int:
    """How many times a run refused to post because it could not switch account.

    Counted off the posting log rather than off Airtable because a refusal is
    deliberately indistinguishable from any other retryable failure once it
    reaches the queue row -- the row says "Failed - Needs Retry", which is true
    but does not say the phone is on the wrong account.
    """
    lowered = (text or "").lower()
    return sum(lowered.count(marker) for marker in markers)


def _slot_of(fields: dict) -> str:
    return at._select_name(fields.get(at.F_PQ_ACCOUNT_SLOT)) or at.SLOT_PRIMARY


def _rows_by_phone(queue_rows) -> dict:
    """{profile record id: {slot: [row fields]}} for profile-driven rows."""
    out: dict = {}
    for row in queue_rows or []:
        fields = row.get("fields") or {}
        links = fields.get(at.F_PQ_TARGET_PROFILE) or []
        if not links:
            continue
        out.setdefault(links[0], {}).setdefault(_slot_of(fields), []).append(fields)
    return out


def _latest(rows, statuses) -> float:
    """The newest Scheduled DateTime among `rows` in one of `statuses`, as epoch
    seconds; 0.0 when there is none."""
    best = 0.0
    for fields in rows:
        if at._select_name(fields.get(at.F_PQ_POST_STATUS)) not in statuses:
            continue
        stamp = _parse_epoch(fields.get(at.F_PQ_SCHEDULED))
        if stamp > best:
            best = stamp
    return best


def _parse_epoch(value) -> float:
    from datetime import datetime, timezone

    if not value:
        return 0.0
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def evaluate(profiles, queue_rows, ready_variants=None, log_text: str = "",
             state=None, now=None) -> WatchReport:
    """Rule on the fleet's two-account phones. Pure.

    - `profiles`: `AirtableClient.second_account_profiles()` (None = the base has
      no such field, which is reported as "nothing to watch", not as a fault)
    - `queue_rows`: every Posting Queue row (`list_queue_rows()`)
    - `ready_variants`: `list_ready_variants()`, for "has anything been spoofed
      for the second accounts yet?"
    - `log_text`: recent posting-log text, searched for switch refusals
    - `state`: `load_state()` -- when the watch first ran, when a second account
      first posted
    """
    now = float(now if now is not None else time.time())
    state = state or {}
    report = WatchReport()

    if profiles is None:
        report.checks.append(Check(
            "second_account_fields", True,
            "this base has no second-account fields; nothing to watch", pending=True))
        return report

    report.phones = len(profiles)
    usable = [p for p in profiles if p.get("usable")]
    report.usable = len(usable)
    if not report.phones:
        report.checks.append(Check(
            "second_account_fields", True,
            "no phone is marked as carrying a second account", pending=True))
        return report

    # --- 1. configured -----------------------------------------------------
    # The one thing that is a person's fault and a person's to fix, so it is a
    # failure from the first tick rather than something the rollout grows out of.
    incomplete = [p["name"] for p in profiles if not p.get("usable")]
    report.checks.append(Check(
        "handles_complete", not incomplete,
        (f"{len(incomplete)} phone(s) ticked as two-account but missing a handle, so only "
         f"the signed-in account posts: {', '.join(sorted(incomplete))}") if incomplete
        else f"all {report.phones} two-account phone(s) have both handles"))

    by_phone = _rows_by_phone(queue_rows)
    usable_ids = {p["record_id"] for p in usable}

    second_rows: list = []
    primary_rows: list = []
    for record_id, slots in by_phone.items():
        if record_id not in usable_ids:
            continue
        second_rows.extend(slots.get(at.SLOT_SECOND, []))
        primary_rows.extend(slots.get(at.SLOT_PRIMARY, []))
    report.second_rows = len(second_rows)
    report.second_posted = len([f for f in second_rows
                                if at._select_name(f.get(at.F_PQ_POST_STATUS))
                                == at.POST_STATUS_POSTED])

    report.second_variants = len([
        v for v in (ready_variants or [])
        if (v.get("slot") or at.SLOT_PRIMARY) == at.SLOT_SECOND
        and v.get("profile_id") in usable_ids])

    first_post_at = float(state.get("first_post_at") or 0.0)
    if report.second_posted and not first_post_at:
        first_post_at = _latest(second_rows, (at.POST_STATUS_POSTED,)) or now
    report.first_post_at = first_post_at
    report.started = bool(first_post_at)

    watching_since = float(state.get("first_seen_at") or now)
    stuck = (now - watching_since) >= STUCK_STAGE_SECONDS

    # --- 2. the chain, one stage at a time ---------------------------------
    # Reported as three separate checks rather than one because each stage has a
    # different owner: spoofing is the pipeline, rows are the queue loop, posts
    # are the phone. "Second accounts are not posting" names none of them.
    if not usable:
        report.checks.append(Check("spoofed_for_second", True,
                                   "no usable two-account phone to spoof for", pending=True))
    elif report.second_variants or report.second_rows:
        report.checks.append(Check(
            "spoofed_for_second", True,
            f"{report.second_variants} Ready clip(s) waiting for a second account"))
    else:
        report.checks.append(Check(
            "spoofed_for_second", not stuck,
            f"nothing has been spoofed for any second account in "
            f"{(now - watching_since) / 3600:.1f}h -- the pipeline is not fanning out to them",
            pending=not stuck))

    if not usable:
        report.checks.append(Check("queued_for_second", True,
                                   "no usable two-account phone to queue for", pending=True))
    elif report.second_rows:
        report.checks.append(Check("queued_for_second", True,
                                   f"{report.second_rows} queue row(s) for second accounts"))
    else:
        report.checks.append(Check(
            "queued_for_second", not stuck,
            "no second account has ever been given a queue row"
            + (" -- it has had video available and still got none" if report.second_variants
               else " (nothing spoofed for them yet)"),
            pending=not stuck or not report.second_variants))

    if report.started:
        report.checks.append(Check("second_account_posts", True,
                                   f"{report.second_posted} post(s) confirmed on second accounts"))
    elif report.second_rows:
        report.checks.append(Check(
            "second_account_posts", not stuck,
            f"{report.second_rows} row(s) queued but none has posted yet",
            pending=not stuck))
    else:
        report.checks.append(Check("second_account_posts", True,
                                   "no second account has posted yet", pending=True))

    # --- 3. correctness, once it is actually running ------------------------
    if not report.started:
        return report

    # 3a. The expensive mistake: one clip on both accounts of one phone. Each
    # account gets its own encode precisely so this cannot happen, and if it
    # does, both rows still say Posted -- Instagram is the only thing that
    # notices, and it notices by suppressing the reach we are paying for.
    shared: list = []
    for record_id, slots in by_phone.items():
        if record_id not in usable_ids:
            continue
        primary_ids = {v for f in slots.get(at.SLOT_PRIMARY, [])
                       for v in (f.get(at.F_PQ_SPOOF_VARIANT) or [])}
        second_ids = {v for f in slots.get(at.SLOT_SECOND, [])
                      for v in (f.get(at.F_PQ_SPOOF_VARIANT) or [])}
        if primary_ids & second_ids:
            shared.append(record_id)
    report.checks.append(Check(
        "no_shared_clip", not shared,
        (f"{len(shared)} phone(s) have the SAME spoofed clip queued on both accounts -- "
         f"duplicate content: {', '.join(sorted(shared))}") if shared
        else "no clip is queued on both accounts of a phone"))

    # 3b. Still going. A second account that posted once and then stopped is the
    # rollout half-failing, which is easy to miss precisely because it did work.
    last_second = _latest(second_rows, (at.POST_STATUS_POSTED,))
    silent = last_second and (now - last_second) >= SILENT_SECONDS
    report.checks.append(Check(
        "second_accounts_still_posting", not silent,
        (f"no second account has posted for {(now - last_second) / 3600:.1f}h")
        if silent else "second accounts posted recently"))

    # 3c. ...and the first account did not lose its phone to the second one. A
    # phone stuck on the second account posts everything there; the primary's
    # rows fail (the switch back is refused) or, worse, quietly go to the wrong
    # account. Either way the primary stops landing posts.
    last_primary = _latest(primary_rows, (at.POST_STATUS_POSTED,))
    starved = last_primary and last_second and (last_second - last_primary) >= SILENT_SECONDS
    report.checks.append(Check(
        "first_accounts_still_posting", not starved,
        (f"the first accounts have not posted for {(now - last_primary) / 3600:.1f}h while the "
         f"second ones have -- the phones may be stuck on the second account")
        if starved else "both accounts of these phones are posting"))

    # 3d. The switch itself. A refusal is safe (nothing is posted on the wrong
    # account) but it costs the row a retry, and a phone that refuses every time
    # is one whose Airtable handle does not match what the phone actually has.
    report.switch_failures = count_switch_failures(log_text)
    report.checks.append(Check(
        "account_switch_works", not report.switch_failures,
        (f"{report.switch_failures} run(s) refused to post because the phone could not be "
         f"put on the right account -- check the handles against the phone's own switcher")
        if report.switch_failures else "no account-switch refusals in the log"))

    return report


def read_recent_log(path, window_seconds: float = LOG_WINDOW_SECONDS, now=None) -> str:
    """The tail of a loop log, or "" if it cannot be read.

    Whole-file rather than time-sliced: the logs rotate, so the live file is
    already short, and parsing timestamps to slice it would make this module
    depend on the log format doing more than holding the sentences it greps for.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # A generous tail keeps a busy day's log from being read in full on every
    # tick while still covering far more than one posting cycle.
    return text[-2_000_000:]


def run_watch(airtable, logger, log_path=None, now=None, watchdog=None,
              app_dir=None) -> WatchReport:
    """Gather what :func:`evaluate` needs, rule on it, and alert if it is wrong.

    Read-only against Airtable. The only thing it writes is its own state file
    (and, through the watchdog, an alert) -- a monitor that can change what it
    monitors is a monitor nobody can trust.
    """
    now = float(now if now is not None else time.time())
    state = load_state(app_dir)

    try:
        profiles = airtable.second_account_profiles()
        queue_rows = airtable.list_queue_rows()
        variants = airtable.list_ready_variants()
    except Exception as exc:
        logger.error("second-accounts: could not read Airtable: %s", exc)
        report = WatchReport()
        report.checks.append(Check("airtable_readable", False, f"{type(exc).__name__}: {exc}"))
        return report

    if log_path is None:
        log_path = Path(__file__).resolve().parents[2] / "logs" / "loop_posting.log"
    report = evaluate(profiles, queue_rows, ready_variants=variants,
                      log_text=read_recent_log(log_path), state=state, now=now)

    # First run stamps the clock the "stuck at a stage" rules measure from, so a
    # freshly deployed watch never alerts on a fleet it has only just met.
    state.setdefault("first_seen_at", now)
    if report.started and not state.get("first_post_at"):
        state["first_post_at"] = report.first_post_at
        logger.warning("second-accounts: FIRST second-account post confirmed (%s). "
                       "Correctness checks are live from now on.",
                       time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.first_post_at)))
    state["last_run_at"] = now
    state["last_summary"] = report.summary()
    save_state(state, app_dir)

    for check in report.checks:
        level = "info" if (check.ok or check.pending) else "warning"
        getattr(logger, level)("second-accounts: %s%s -- %s", check.name,
                               "" if check.ok else " FAILING", check.detail)
    logger.info("second-accounts: %s", report.summary())

    if watchdog is not None:
        # Same entry point `doctor` uses: alerts on the transition into failing,
        # whenever the failing set changes, and once per re-notify interval
        # while it lasts. Nothing new to configure or to watch a second time.
        watchdog.observe_health("second-accounts", report.failures,
                                checked=report.checked, detail=report.summary(), now=now)
    return report
