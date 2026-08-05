"""Scheduled preflight: doctor's failures reach a human through the watchdog.

Before this, every connectivity check -- MLX agent listening, Airtable
readable, Drive reachable -- only ran when a person typed `run_loop doctor`. On
2026-08-04 the MLX agent died twice and nothing said so; the failure surfaced
an hour later as launches failing. These tests pin the alerting behaviour, not
the checks themselves.
"""

import time
import unittest
from unittest import mock

from adb_bot.automation import doctor, loop_watchdog, schedule_spec, scheduling


def _result(name, status, detail=""):
    return doctor.CheckResult(name, status, detail)


class _Recorder:
    """A sink that just remembers what it was handed."""

    def __init__(self):
        self.alerts = []

    def __call__(self, alert):
        self.alerts.append(alert)

    @property
    def kinds(self):
        return [a.kind for a in self.alerts]


class DoctorAlertTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sink = _Recorder()
        self.t = 1_000_000.0

    def _watchdog(self):
        return loop_watchdog.LoopWatchdog(state_dir=self.tmp.name, sinks=[self.sink])

    def _observe(self, results, wd=None, now=None, **kw):
        return doctor.observe_doctor(wd or self._watchdog(), results,
                                     now=now if now is not None else self.t, **kw)

    # -- the basic transitions -------------------------------------------

    def test_all_passing_never_alerts(self):
        v = self._observe([_result("Airtable", doctor.PASS),
                           _result("MultiLogin agent", doctor.PASS)])
        self.assertEqual(v.state, loop_watchdog.STATE_OK)
        self.assertEqual(self.sink.alerts, [])

    def test_a_failing_check_alerts_once(self):
        wd = self._watchdog()
        results = [_result("MultiLogin agent", doctor.FAIL, "nothing on :45001"),
                   _result("Airtable", doctor.PASS)]
        first = self._observe(results, wd=wd)
        self.assertEqual(first.state, loop_watchdog.STATE_UNHEALTHY)
        self.assertEqual(self.sink.kinds, [loop_watchdog.ALERT_UNHEALTHY])
        alert = self.sink.alerts[0]
        self.assertEqual(alert.severity, "error")
        self.assertIn("UNHEALTHY", alert.message)
        self.assertIn("MultiLogin agent", alert.message)
        self.assertIn("nothing on :45001", alert.message)

        # Same failure one tick later: recorded, but nobody is told again.
        self._observe(results, wd=wd, now=self.t + 30 * 60)
        self.assertEqual(self.sink.kinds, [loop_watchdog.ALERT_UNHEALTHY])

    def test_persisting_failure_reminds_after_the_renotify_interval(self):
        wd = self._watchdog()
        results = [_result("MultiLogin agent", doctor.FAIL, "down")]
        self._observe(results, wd=wd)
        self._observe(results, wd=wd, now=self.t + loop_watchdog.DEFAULT_RENOTIFY_SECONDS + 1)
        self.assertEqual(self.sink.kinds,
                         [loop_watchdog.ALERT_UNHEALTHY, loop_watchdog.ALERT_REMINDER])

    def test_recovery_clears_and_says_so(self):
        wd = self._watchdog()
        self._observe([_result("MultiLogin agent", doctor.FAIL, "down")], wd=wd)
        v = self._observe([_result("MultiLogin agent", doctor.PASS)],
                          wd=wd, now=self.t + 600)
        self.assertEqual(v.state, loop_watchdog.STATE_OK)
        self.assertEqual(self.sink.kinds[-1], loop_watchdog.ALERT_HEALTHY)
        self.assertEqual(self.sink.alerts[-1].severity, "info")
        # And the budget resets, so the next outage is not silent.
        self._observe([_result("Airtable", doctor.FAIL, "auth rejected")],
                      wd=wd, now=self.t + 700)
        self.assertEqual(self.sink.kinds[-1], loop_watchdog.ALERT_UNHEALTHY)

    def test_a_new_failure_alerts_immediately_despite_the_storm_guard(self):
        """The case the signature exists for: one outage must not mask another."""
        wd = self._watchdog()
        self._observe([_result("MultiLogin agent", doctor.FAIL, "down")], wd=wd)
        self.assertEqual(len(self.sink.alerts), 1)
        # Well inside the re-notify hour, but a second thing has now broken.
        v = self._observe([_result("MultiLogin agent", doctor.FAIL, "down"),
                           _result("Airtable", doctor.FAIL, "auth rejected")],
                          wd=wd, now=self.t + 60)
        self.assertEqual(v.due, 2)
        self.assertEqual(self.sink.kinds,
                         [loop_watchdog.ALERT_UNHEALTHY, loop_watchdog.ALERT_UNHEALTHY])
        self.assertIn("Airtable", self.sink.alerts[-1].message)

    # -- what counts as a failure ----------------------------------------

    def test_warnings_are_not_failures_by_default(self):
        v = self._observe([_result("Desktop UI (tkinter)", doctor.WARN, "no display"),
                           _result("Airtable", doctor.PASS)])
        self.assertEqual(v.state, loop_watchdog.STATE_OK)
        self.assertEqual(self.sink.alerts, [])

    def test_the_headless_display_warning_is_ignored_even_when_opted_in(self):
        """A server has no display. Alerting on it every 30 min teaches people
        to ignore alerts, which costs more than the check is worth."""
        v = self._observe([_result("Desktop UI (tkinter)", doctor.WARN, "no display")],
                          include_warnings=True)
        self.assertEqual(v.state, loop_watchdog.STATE_OK)
        self.assertEqual(self.sink.alerts, [])

    def test_other_warnings_can_be_opted_in(self):
        v = self._observe([_result("Google Drive", doctor.WARN, "quota low")],
                          include_warnings=True)
        self.assertEqual(v.state, loop_watchdog.STATE_UNHEALTHY)
        self.assertEqual([a.kind for a in self.sink.alerts], [loop_watchdog.ALERT_UNHEALTHY])

    def test_passing_count_is_reported(self):
        v = self._observe([_result("A", doctor.FAIL), _result("B", doctor.PASS),
                           _result("C", doctor.PASS)])
        self.assertEqual((v.due, v.produced), (1, 2))

    # -- the feedback loop ------------------------------------------------

    def test_doctors_own_entry_does_not_fail_its_own_production_check(self):
        """`check_loop_production` reads the watchdog; `observe_doctor` writes to
        it. Without a guard, one failing check latches permanently: doctor fails
        -> the entry goes unhealthy -> the entry makes the check fail forever."""
        wd = self._watchdog()
        self._observe([_result("MultiLogin agent", doctor.FAIL, "down")], wd=wd)
        # Built before patching: the reader must be a real watchdog, or the
        # lambda would re-enter the patched name and recurse.
        reader = self._watchdog()
        with mock.patch.object(loop_watchdog, "LoopWatchdog", lambda *a, **k: reader):
            result = doctor.check_loop_production()
        self.assertNotEqual(result.status, doctor.FAIL)
        self.assertIn("doctor", reader.snapshot(), "the entry under test was never written")

    def test_a_genuinely_stalled_loop_still_fails_the_production_check(self):
        wd = self._watchdog()
        grace = loop_watchdog.stall_after_seconds("posting")
        wd.observe("posting", due=5, produced=0, now=self.t)
        wd.observe("posting", due=5, produced=0, now=self.t + grace + 1)
        reader = self._watchdog()
        with mock.patch.object(loop_watchdog, "LoopWatchdog", lambda *a, **k: reader):
            result = doctor.check_loop_production()
        self.assertEqual(result.status, doctor.FAIL)
        self.assertIn("posting", result.detail)


class DoctorSchedulingTest(unittest.TestCase):
    def test_doctor_is_in_the_recommended_set_with_a_cadence(self):
        self.assertIn("doctor", schedule_spec.RECOMMENDED_LOOPS)
        self.assertEqual(schedule_spec.RECOMMENDED_INTERVALS["doctor"], 30)
        self.assertIn("doctor", schedule_spec.DESCRIPTIONS)

    def test_doctor_is_installable_and_not_held_back(self):
        self.assertIn("doctor", scheduling.installable_loops())
        self.assertNotIn("doctor", scheduling.pending_loops())

    def test_doctor_is_never_given_apply(self):
        args = schedule_spec.loop_arguments("doctor", apply=True)
        self.assertIn("run_loop doctor", args)
        self.assertNotIn("--apply", args)

    def test_the_real_loops_still_get_apply(self):
        self.assertIn("--apply", schedule_spec.loop_arguments("posting", apply=True))

    def test_the_installer_knows_doctor_so_remove_cleans_it_up(self):
        from pathlib import Path
        script = Path(__file__).resolve().parents[1] / "deploy/systemd/install_units.sh"
        line = [ln for ln in script.read_text().splitlines() if ln.startswith("LOOPS=")]
        self.assertTrue(line)
        self.assertIn("doctor", line[0])


if __name__ == "__main__":
    unittest.main()
