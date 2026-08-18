"""Counting MultiLogin minutes, and warning before they stop the fleet.

On 2026-08-18 the minutes ran out at 16:59 and every launch failed for hours
with nothing noticing: the bot said `42002 profile is not running`, the launcher
said `501`, and neither mentioned quota. Both alerts here exist for that day, so
the cases that matter are the ones that would have caught it -- and the ones
that must not cry wolf on an ordinary quiet night.
"""

from datetime import date, datetime
from types import SimpleNamespace

from adb_bot.automation import mlx_minutes as m

# Real lines, from `/root/mlx/logs/launcher_20260818.log`.
LOG = """2026-08-18T10:00:00.000Z	info	zCJFZYeDPc24.go:1	mobile profile '111' started
2026-08-18T10:04:00.000Z	info	XmdP91FrQz.go:1	mobileProcessMeta for profile 111 finished successfully.
2026-08-18T10:05:00.000Z	info	zCJFZYeDPc24.go:1	mobile profile '222' started
2026-08-18T10:11:00.000Z	info	XmdP91FrQz.go:1	mobileProcessMeta for profile 222 finished successfully.
2026-08-18T16:59:46.011Z	error	rpUpihMak.go:1	start profiles for wsID f03f25bc returned 501 http status
2026-08-18T17:00:07.773Z	error	rpUpihMak.go:1	start profiles for wsID f03f25bc returned 501 http status
"""


def _log_dir(tmp_path, name="launcher_20260818.log", text=LOG):
    (tmp_path / name).write_text(text)
    return tmp_path


# --- counting --------------------------------------------------------------

def test_minutes_are_the_paired_start_and_finish(tmp_path):
    days = m.usage_by_day(_log_dir(tmp_path))

    assert len(days) == 1
    assert days[0].sessions == 2
    assert days[0].minutes == 10          # 4 + 6


def test_a_session_the_log_never_closes_is_counted_as_zero_not_guessed(tmp_path):
    """Inventing a duration would turn a warning into a false alarm, so an
    unclosed session is reported separately and the total stays an undercount."""
    text = LOG + ("2026-08-18T18:00:00.000Z\tinfo\tzCJFZYeDPc24.go:1\t"
                  "mobile profile '333' started\n")
    days = m.usage_by_day(_log_dir(tmp_path, text=text))

    assert days[0].minutes == 10
    assert days[0].unclosed == 1


def test_a_restarted_profile_is_not_billed_for_the_gap(tmp_path):
    """The launcher does not log the end of a session it lost. Keeping the older
    start would bill everything between the two."""
    text = ("2026-08-18T10:00:00.000Z\tinfo\tx.go:1\tmobile profile '111' started\n"
            "2026-08-18T12:00:00.000Z\tinfo\tx.go:1\tmobile profile '111' started\n"
            "2026-08-18T12:03:00.000Z\tinfo\tx.go:1\t"
            "mobileProcessMeta for profile 111 finished successfully.\n")
    days = m.usage_by_day(_log_dir(tmp_path, text=text))

    assert days[0].minutes == 3


# --- the billing period ----------------------------------------------------

def test_the_period_starts_on_the_reset_day_of_this_month():
    assert m.period_start(date(2026, 8, 18), 1) == date(2026, 8, 1)
    assert m.period_start(date(2026, 8, 18), 12) == date(2026, 8, 12)


def test_before_the_reset_day_the_period_started_last_month():
    assert m.period_start(date(2026, 8, 3), 12) == date(2026, 7, 12)


def test_a_reset_day_that_does_not_exist_every_month_is_clamped():
    """A period that silently failed to start would report the fleet's whole
    history as this month's usage."""
    assert m.period_start(date(2026, 2, 20), 31).day == 28


def test_only_days_inside_the_period_are_counted(tmp_path):
    _log_dir(tmp_path, "launcher_20260718.log", LOG.replace("2026-08", "2026-07"))
    _log_dir(tmp_path)
    report = m.collect(path=tmp_path, now=datetime(2026, 8, 18, 20, 0),
                       allowance=1000, reset_day=1)

    assert report.period_minutes == 10        # July's 10 excluded
    assert report.remaining == 990


# --- the low-balance alert -------------------------------------------------

def test_no_allowance_configured_is_not_a_low_balance(tmp_path):
    """An unknown balance is not a low one. Firing here would train everyone to
    mute the alert on a fresh install."""
    report = m.collect(path=tmp_path, now=datetime(2026, 8, 18), allowance=None)

    assert report.remaining is None
    assert report.low is False


def test_the_warning_fires_below_the_threshold(tmp_path):
    report = m.collect(path=_log_dir(tmp_path), now=datetime(2026, 8, 18),
                       allowance=700, reset_day=1, warn_below=800)

    assert report.low is True
    assert "minutes are low" in m.minutes_message(report)


def test_the_balance_never_reads_as_negative(tmp_path):
    """Overshooting the allowance is exactly what happened, and "-4,000 left"
    reads as a bug rather than an empty account."""
    report = m.collect(path=_log_dir(tmp_path), now=datetime(2026, 8, 18),
                       allowance=5, reset_day=1)

    assert report.remaining == 0


# --- the everything-is-failing alert ---------------------------------------

def _run(attempts, ok):
    return SimpleNamespace(attempts=attempts, ok=ok)


def test_two_runs_that_launched_nothing_are_the_alert():
    """The real shape of 2026-08-18: attempts in every run, successes in none."""
    tail = m.failing_runs([_run(10, 3), _run(56, 0), _run(48, 0)])

    assert len(tail) == 2


def test_one_bad_run_is_not_enough():
    assert m.failing_runs([_run(10, 3), _run(48, 0)]) == []


def test_a_run_with_nothing_to_do_is_not_a_failure():
    """A quiet night plans no launches. Counting those would fire this every
    time the fleet had no work, which is how an alert gets ignored."""
    assert m.failing_runs([_run(48, 0), _run(0, 0), _run(0, 0)]) == []


def test_a_recovered_run_clears_it():
    assert m.failing_runs([_run(48, 0), _run(48, 0), _run(10, 5)]) == []


def test_the_failure_message_names_the_likely_cause():
    tail = m.failing_runs([_run(56, 0), _run(48, 0)])
    body = m.failure_message(tail, {"501": 872})

    assert "501" in body and "minute balance" in body


def test_without_cloud_refusals_it_does_not_blame_the_account():
    """Same symptom, different cause: no 501s means the phones or the launcher,
    and sending somebody to the billing page would waste the outage."""
    body = m.failure_message([_run(48, 0), _run(48, 0)], {})

    assert "minute balance" not in body


# --- not shouting every tick -----------------------------------------------

def test_an_alert_fires_once_and_then_stays_quiet():
    """Four-hourly ticks through a day-long outage would otherwise send the same
    message six times."""
    state, now = {}, datetime(2026, 8, 18, 12, 0)

    assert m.should_alert("low_minutes", True, now=now, state=state) is True
    assert m.should_alert("low_minutes", True, now=now, state=state) is False


def test_a_condition_that_persists_is_raised_again_the_next_day():
    state = {}
    m.should_alert("low_minutes", True, now=datetime(2026, 8, 18, 12, 0), state=state)

    assert m.should_alert("low_minutes", True,
                          now=datetime(2026, 8, 19, 13, 0), state=state) is True


def test_clearing_resets_it_so_the_next_occurrence_is_reported():
    state = {}
    m.should_alert("all_failing", True, now=datetime(2026, 8, 18, 12, 0), state=state)
    m.should_alert("all_failing", False, now=datetime(2026, 8, 18, 16, 0), state=state)

    assert m.should_alert("all_failing", True,
                          now=datetime(2026, 8, 18, 20, 0), state=state) is True


# --- the tick --------------------------------------------------------------

class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, text, logger=None):
        self.sent.append(text)
        return True


def test_a_tick_with_nothing_wrong_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(m, "collect", lambda **kw: m.MinutesReport(
        period_start="2026-08-01", period_minutes=10, allowance=5000,
        remaining=4990))
    notifier = _Notifier()

    out = m.run_check(notifier=notifier, runs=[_run(10, 5)],
                      state_path=tmp_path / "state.json")

    assert notifier.sent == []
    assert out["low"] is False and out["all_failing"] is False


def test_a_tick_that_finds_both_problems_sends_both(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "collect", lambda **kw: m.MinutesReport(
        period_start="2026-08-01", period_minutes=36397, allowance=36000,
        remaining=0, warn_below=800))
    monkeypatch.setattr(m, "refusal_counts", lambda **kw: {"501": 872})
    notifier = _Notifier()

    out = m.run_check(notifier=notifier, runs=[_run(56, 0), _run(48, 0)],
                      state_path=tmp_path / "state.json")

    assert len(notifier.sent) == 2
    assert set(out["sent"]) == {"low_minutes", "all_failing"}


def test_a_tick_survives_unreadable_logs(tmp_path, monkeypatch):
    """This runs on a timer. An alerting path that can crash is one that stops
    alerting, and it would go unnoticed for exactly as long as the last outage."""
    def boom(**kw):
        raise OSError("no such directory")

    monkeypatch.setattr(m, "collect", boom)

    out = m.run_check(runs=[], state_path=tmp_path / "state.json")

    assert out["sent"] == [] and "error" in out
