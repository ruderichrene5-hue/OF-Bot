"""Stopping the fleet when MultiLogin runs out of proxy traffic or minutes.

Written against two real outages. On 2026-08-20 and again on 2026-08-21 the
proxy allowance emptied and the fleet spent hours launching phones into a
gateway that answered `402 Payment Required`; on 2026-08-18 the *minutes* ran
out and produced a different signature entirely. The cases that matter are the
ones that would have stopped those, and -- just as important -- the ones that
must not stop a healthy fleet.
"""

from datetime import datetime, timedelta

from adb_bot.automation import mlx_guard as g


# --- the probe -------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _profiles(n, kind="http"):
    return [{"serial_name": f"P{i}",
             "proxy": {"type": kind, "username": f"u{i}", "password": "p",
                       "server": "gate.multilogin.com", "port": 8080}}
            for i in range(n)]


def _patch_get(monkeypatch, responses):
    """Answer each probe from `responses` in order; an Exception is raised."""
    calls = iter(responses)

    def fake_get(url, **kwargs):
        nxt = next(calls)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr("requests.get", fake_get)


def test_a_gateway_answering_402_everywhere_is_the_allowance_being_empty(monkeypatch):
    _patch_get(monkeypatch, [FakeResponse(402)] * 4)

    probe = g.probe_gateway(profiles=_profiles(4))

    assert probe.status == "exhausted"
    assert probe.exhausted
    assert probe.paid == 4


def test_one_success_outranks_any_number_of_402s(monkeypatch):
    """An account that can still serve a request is not empty. Sticky sessions
    die individually, and stopping the fleet for one is worse than waiting."""
    _patch_get(monkeypatch, [FakeResponse(402), FakeResponse(402),
                             FakeResponse(200, {"city": "Mannheim",
                                                "query": "1.2.3.4"}),
                             FakeResponse(402)])

    probe = g.probe_gateway(profiles=_profiles(4))

    assert probe.status == "ok"
    assert not probe.exhausted
    assert probe.exits == ["Mannheim/1.2.3.4"]


def test_a_single_402_is_not_enough_to_stop_the_fleet(monkeypatch):
    _patch_get(monkeypatch, [FakeResponse(402), ConnectionError("boom"),
                             ConnectionError("boom"), ConnectionError("boom")])

    probe = g.probe_gateway(profiles=_profiles(4))

    assert probe.status == "unknown"
    assert not probe.exhausted


def test_a_probe_that_cannot_run_stops_nothing(monkeypatch):
    """`unknown` is the safe answer: a guard that mistakes its own network
    trouble for an empty account takes the fleet down for free."""
    _patch_get(monkeypatch, [ConnectionError("no route")] * 4)

    probe = g.probe_gateway(profiles=_profiles(4))

    assert probe.status == "unknown"
    assert probe.errors == 4


def test_socks5_profiles_are_skipped_because_the_box_cannot_dial_them():
    probe = g.probe_gateway(profiles=_profiles(3, kind="socks5"))

    assert probe.status == "unknown"
    assert probe.sampled == 0
    assert "http-type" in probe.detail


# --- telling the two outages apart -----------------------------------------

REFUSALS = """2026-08-18T16:59:46.011Z\terror\trpUpihMak.go:1\tstart profiles for wsID f03f25bc returned 501 http status
2026-08-18T17:00:07.773Z\terror\trpUpihMak.go:1\tstart profiles for wsID f03f25bc returned 501 http status
2026-08-18T17:01:07.773Z\terror\trpUpihMak.go:1\tstart profiles for wsID f03f25bc returned 500 http status
"""


def test_only_501s_inside_the_window_count(tmp_path):
    """A 500 is not a quota refusal, and an outage that was already topped up
    this morning must not keep the fleet stopped all afternoon."""
    (tmp_path / "launcher_20260818.log").write_text(REFUSALS)
    now = datetime(2026, 8, 18, 17, 30)

    assert g.refusals_501(now=now, path=tmp_path) == 2
    # Same log, hours later: the 501s have aged out of the window.
    assert g.refusals_501(now=datetime(2026, 8, 18, 23, 0), path=tmp_path) == 0


def test_an_empty_gateway_is_never_blamed_on_minutes(monkeypatch, tmp_path):
    """Both outages stop every launch. Naming the wrong one sends the top-up to
    the wrong product, so the 402 wins and the minutes signal is not even read."""
    _patch_get(monkeypatch, [FakeResponse(402)] * 4)
    monkeypatch.setattr(g, "stop_burners", lambda **kw: [])

    report = g.run_check(profiles=_profiles(4),
                         state_path=tmp_path / "s.json", dry_run=True)

    assert report.probe.exhausted
    assert report.minutes_out is False
    assert "proxy_out" in report.sent
    assert "minutes_out" not in report.sent


# --- the gigabyte estimate -------------------------------------------------

SESSIONS = """2026-08-20T15:00:00.000Z\tinfo\tz.go:1\tmobile profile '111' started
2026-08-20T15:30:00.000Z\tinfo\tX.go:1\tmobileProcessMeta for profile 111 finished successfully.
"""


def test_minutes_are_clipped_to_the_top_up_not_counted_whole(tmp_path):
    """A session that straddles the top-up only contributes the part after it;
    billing the earlier half to the new allowance would overstate the burn."""
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)

    whole = g.minutes_since(datetime(2026, 8, 20, 14, 0),
                            now=datetime(2026, 8, 20, 16, 0), path=tmp_path)
    clipped = g.minutes_since(datetime(2026, 8, 20, 15, 20),
                              now=datetime(2026, 8, 20, 16, 0), path=tmp_path)

    assert whole == 30
    assert clipped == 10


def test_no_allowance_means_no_number_rather_than_a_guess(monkeypatch, tmp_path):
    """An unknown balance is not a low one -- the same rule `mlx_minutes` uses
    for minutes. Without it every unconfigured box warns on every tick."""
    monkeypatch.delenv("MLX_PROXY_GB_ALLOWANCE", raising=False)
    state = {"topped_up_at": "2026-08-20T15:00:00", "gb_per_minute": 0.01}

    gb, allowance, rate, minutes = g.estimate_gb(
        state, now=datetime(2026, 8, 20, 16, 0), path=tmp_path)

    assert gb is None
    assert allowance is None


def test_gb_left_is_the_allowance_minus_minutes_times_rate(monkeypatch, tmp_path):
    monkeypatch.setenv("MLX_PROXY_GB_ALLOWANCE", "10")
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)
    state = {"topped_up_at": "2026-08-20T15:00:00", "gb_per_minute": 0.1}

    gb, allowance, rate, minutes = g.estimate_gb(
        state, now=datetime(2026, 8, 20, 16, 0), path=tmp_path)

    assert minutes == 30
    assert gb == 7.0                      # 10 - 30 * 0.1
    assert allowance == 10


def test_the_estimate_never_goes_negative(monkeypatch, tmp_path):
    monkeypatch.setenv("MLX_PROXY_GB_ALLOWANCE", "1")
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)
    state = {"topped_up_at": "2026-08-20T15:00:00", "gb_per_minute": 0.5}

    gb, _, _, _ = g.estimate_gb(state, now=datetime(2026, 8, 20, 16, 0),
                                path=tmp_path)

    assert gb == 0.0


def test_running_dry_calibrates_the_rate_for_next_time(monkeypatch):
    """The one good thing about having had several outages: each measures a
    full cycle, so `allowance / minutes` is a real rate rather than a guess."""
    monkeypatch.setenv("MLX_PROXY_GB_ALLOWANCE", "10")

    assert g.calibrate({}, 1000) == 0.01
    # Nothing to calibrate against, so no rate rather than a division by zero.
    assert g.calibrate({}, 0) is None


# --- stopping --------------------------------------------------------------

def test_the_guard_never_stops_itself():
    """A checker that switches itself off cannot notice the recovery, and the
    outage becomes permanent."""
    assert not any("guard" in unit for unit in g.DEFAULT_TIMERS)


def test_only_units_that_were_still_enabled_are_reported(monkeypatch):
    """This runs every tick for as long as the outage lasts. Reporting units it
    did not change would make the alert claim to stop an already-stopped fleet."""
    enabled = {"adbbot-posting.timer": "enabled",
               "adbbot-recheck.timer": "disabled",
               "adbbot-warmup.timer": "disabled"}
    monkeypatch.setattr(g, "_is_enabled", lambda unit: enabled.get(unit, "unknown"))

    assert g.stop_burners(dry_run=True) == ["adbbot-posting.timer"]


def test_recovery_only_resumes_what_the_guard_itself_stopped(monkeypatch):
    """Warm-up has been off by a person's decision since 2026-08-20. Resuming
    `DEFAULT_TIMERS` wholesale would switch it back on behind their back."""
    calls = []
    monkeypatch.setattr(g, "_systemctl",
                        lambda *a, **kw: calls.append(a) or True)

    back = g.resume_burners(["adbbot-posting.timer"])

    assert back == ["adbbot-posting.timer"]
    assert calls == [("enable", "--now", "adbbot-posting.timer")]


def test_autoresume_is_off_unless_asked_for(monkeypatch, tmp_path):
    """Resuming spends money and posts to live accounts, so it stays a person's
    decision unless the box explicitly opts in."""
    monkeypatch.delenv("ADBBOT_GUARD_AUTORESUME", raising=False)
    _patch_get(monkeypatch, [FakeResponse(200, {"city": "Stuttgart",
                                                "query": "5.6.7.8"})] * 4)
    state_path = tmp_path / "s.json"
    state_path.write_text('{"gateway": "exhausted", '
                          '"stopped_by_guard": ["adbbot-posting.timer"]}')

    report = g.run_check(profiles=_profiles(4), state_path=state_path,
                         dry_run=True)

    assert report.resumed == []
    assert "proxy_back" in report.sent


def test_without_a_gb_figure_it_warns_off_the_last_cycles_length(monkeypatch,
                                                                 tmp_path):
    """The GB number only exists on the MultiLogin dashboard, so a fresh install
    would otherwise have no early warning at all and hear nothing until the
    fleet stopped. The previous cycle's length is measured, and needs nobody."""
    monkeypatch.delenv("MLX_PROXY_GB_ALLOWANCE", raising=False)
    monkeypatch.setenv("MLX_LOG_DIR", str(tmp_path))
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)   # 30 minutes
    _patch_get(monkeypatch, [FakeResponse(200, {"city": "Mannheim",
                                                "query": "1.2.3.4"})] * 4)
    monkeypatch.setattr(g, "refusals_501", lambda **kw: 0)
    state_path = tmp_path / "s.json"
    # The last top-up bought 32 minutes; 30 of them are already gone.
    state_path.write_text('{"gateway": "ok", '
                          '"topped_up_at": "2026-08-20T15:00:00", '
                          '"last_cycle_minutes": 32}')

    report = g.run_check(profiles=_profiles(4), state_path=state_path,
                         now=datetime(2026, 8, 20, 16, 0), dry_run=True)

    assert report.low_basis == "cycle"
    assert report.gb_low is True
    assert "proxy_low" in report.sent
    assert "94%" in g.proxy_low_message(report)


def test_the_fallback_stays_quiet_early_in_a_cycle(monkeypatch, tmp_path):
    monkeypatch.delenv("MLX_PROXY_GB_ALLOWANCE", raising=False)
    monkeypatch.setenv("MLX_LOG_DIR", str(tmp_path))
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)   # 30 minutes
    _patch_get(monkeypatch, [FakeResponse(200, {"city": "Mannheim",
                                                "query": "1.2.3.4"})] * 4)
    monkeypatch.setattr(g, "refusals_501", lambda **kw: 0)
    state_path = tmp_path / "s.json"
    # 30 minutes into a cycle the last one ran 3,000 -- nowhere near.
    state_path.write_text('{"gateway": "ok", '
                          '"topped_up_at": "2026-08-20T15:00:00", '
                          '"last_cycle_minutes": 3000}')

    report = g.run_check(profiles=_profiles(4), state_path=state_path,
                         now=datetime(2026, 8, 20, 16, 0), dry_run=True)

    assert report.gb_low is False
    assert report.sent == []


def test_a_real_gb_figure_beats_the_cycle_proxy(monkeypatch, tmp_path):
    """The fallback is the coarser instrument; it must not override a number
    somebody actually configured."""
    monkeypatch.setenv("MLX_PROXY_GB_ALLOWANCE", "10")
    monkeypatch.setenv("MLX_LOG_DIR", str(tmp_path))
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)
    _patch_get(monkeypatch, [FakeResponse(200, {"city": "Mannheim",
                                                "query": "1.2.3.4"})] * 4)
    monkeypatch.setattr(g, "refusals_501", lambda **kw: 0)
    state_path = tmp_path / "s.json"
    # Deep into the last cycle's length, but 7 GB still left by the real figure.
    state_path.write_text('{"gateway": "ok", '
                          '"topped_up_at": "2026-08-20T15:00:00", '
                          '"last_cycle_minutes": 32, "gb_per_minute": 0.1}')

    report = g.run_check(profiles=_profiles(4), state_path=state_path,
                         now=datetime(2026, 8, 20, 16, 0), dry_run=True)

    assert report.low_basis == "gb"
    assert report.gb_left == 7.0
    assert report.gb_low is False
    assert report.sent == []


def test_recovery_does_not_also_warn_that_traffic_is_nearly_gone(monkeypatch,
                                                                 tmp_path):
    """The estimate is measured from the top-up that just ran dry, so on the
    recovery tick it reads ~0. Sending "nearly gone" in the same tick as "it is
    back" is the sort of contradiction that gets an alert channel muted."""
    monkeypatch.setenv("MLX_PROXY_GB_ALLOWANCE", "10")
    monkeypatch.delenv("ADBBOT_GUARD_AUTORESUME", raising=False)
    _patch_get(monkeypatch, [FakeResponse(200, {"city": "Stuttgart",
                                                "query": "5.6.7.8"})] * 4)
    monkeypatch.setattr(g, "refusals_501", lambda **kw: 0)
    # Point the minute counter at a log dir holding one long dead cycle, so the
    # test does not depend on whatever this machine's real fleet has been doing.
    monkeypatch.setenv("MLX_LOG_DIR", str(tmp_path))
    (tmp_path / "launcher_20260820.log").write_text(SESSIONS)
    # A cycle that burned the whole 10 GB at the calibrated rate.
    state_path = tmp_path / "s.json"
    state_path.write_text('{"gateway": "exhausted", '
                          '"topped_up_at": "2026-08-20T15:17:00", '
                          '"gb_per_minute": 0.5}')

    report = g.run_check(profiles=_profiles(4), state_path=state_path,
                         dry_run=True)

    assert "proxy_back" in report.sent
    assert "proxy_low" not in report.sent
    assert report.gb_left == 10.0        # the new cycle, not the dead one


def test_a_healthy_fleet_is_left_alone(monkeypatch, tmp_path):
    _patch_get(monkeypatch, [FakeResponse(200, {"city": "Mannheim",
                                                "query": "1.2.3.4"})] * 4)
    monkeypatch.setattr(g, "refusals_501", lambda **kw: 0)

    report = g.run_check(profiles=_profiles(4),
                         state_path=tmp_path / "s.json", dry_run=True)

    assert report.probe.status == "ok"
    assert report.stopped == []
    assert report.sent == []


# --- not crying wolf -------------------------------------------------------

def test_an_outage_alerts_once_not_every_tick(monkeypatch, tmp_path):
    """The guard ticks every few minutes. Without suppression a night-long
    outage would send hundreds of identical messages and get the chat muted."""
    monkeypatch.setattr(g, "stop_burners", lambda **kw: [])
    state_path = tmp_path / "s.json"

    sent = []
    for _ in range(3):
        _patch_get(monkeypatch, [FakeResponse(402)] * 4)
        report = g.run_check(profiles=_profiles(4), state_path=state_path,
                             dry_run=False, notifier=_Silent(sent))
        del report

    assert sent.count("proxy_out") == 1


class _Silent:
    """A notifier that records instead of sending."""

    def __init__(self, log):
        self.log = log

    def send(self, body, logger=None):
        self.log.append("proxy_out" if "Out of MultiLogin proxy" in body
                        else "other")
        return True
