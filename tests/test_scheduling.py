"""The schedule itself: what is supposed to run, how often, and whether it is.

These loops only ever ran when a human typed the command, so the things worth
pinning are (a) every loop the CLI can run has an agreed cadence, and (b)
`doctor` names the loops that are *not* scheduled instead of one flat warning.

Nothing here shells out to systemctl -- the backend listing is faked.
"""

import re
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from adb_bot.automation import schedule_spec, scheduler_admin, scheduling
from adb_bot.automation import systemd_admin as sd


@contextmanager
def fake_systemd(status: dict, listed=None):
    """Pretend systemd is the backend and reports `status` ({loop: state}).

    `listed` limits what the backend's own `list_status()` enumerates, so the
    per-loop fallback lookup can be exercised. Nothing runs systemctl.
    """
    listing = {loop: state for loop, state in status.items()
               if listed is None or loop in listed}
    with mock.patch.object(scheduler_admin, "is_supported", return_value=False), \
         mock.patch.object(sd, "is_supported", return_value=True), \
         mock.patch.object(sd, "list_status", return_value=listing), \
         mock.patch.object(sd, "query_state", side_effect=lambda loop: status.get(loop)):
        yield


class RecommendedSetTest(unittest.TestCase):
    def test_every_cli_loop_is_in_the_recommended_set(self):
        """A loop the CLI can run but nobody scheduled runs only by hand.

        Unless running only by hand is the point: `MANUAL_ONLY_LOOPS` is the
        explicit, named exception, so "not scheduled" cannot happen by
        forgetting -- it has to be written down.
        """
        from adb_bot.automation import run_loop
        for loop in run_loop.LOOPS:
            if loop in schedule_spec.MANUAL_ONLY_LOOPS:
                continue
            self.assertIn(loop, scheduling.RECOMMENDED_LOOPS, loop)

    def test_a_manual_only_loop_is_runnable_but_never_installed(self):
        """The deploy footgun this exists to close: `install_units.sh` asks for
        `installable_loops()` and enables every name it gets back, with
        `--apply` appended by `loop_arguments`. So a loop that writes to the
        shared MultiLogin workspace must not be reachable from that list -- or
        the next routine installer run, for any unrelated reason, silently arms
        an unattended 15-minute writer."""
        from adb_bot.automation import run_loop
        for loop in schedule_spec.MANUAL_ONLY_LOOPS:
            self.assertIn(loop, run_loop.COMMANDS, loop)
            self.assertNotIn(loop, scheduling.RECOMMENDED_LOOPS, loop)
            self.assertNotIn(loop, scheduling.installable_loops(), loop)
            self.assertNotIn(loop, scheduling.pending_loops(), loop)

    def test_a_manual_only_loop_cannot_be_armed_by_re_listing_it(self):
        """The second fence. Putting the name back in the recommended set --
        the easy edit, and the one somebody tidying the spec would make -- must
        still not make it installable."""
        with mock.patch.object(scheduling, "RECOMMENDED_LOOPS",
                               tuple(scheduling.RECOMMENDED_LOOPS) + ("issue-tags",)):
            self.assertNotIn("issue-tags", scheduling.installable_loops())

    def test_spec_declares_no_loop_the_cli_cannot_run(self):
        # The reverse (a CLI loop missing from the spec) is tolerated: the
        # status lookup fills it in, so a loop landing in run_loop.py first
        # still gets reported. A spec-only loop would just fail every tick.
        from adb_bot.automation import run_loop
        self.assertTrue(set(schedule_spec.LOOPS) <= set(run_loop.LOOPS),
                        set(schedule_spec.LOOPS) - set(run_loop.LOOPS))
        self.assertIn("recheck", schedule_spec.LOOPS)

    def test_recheck_is_scheduled(self):
        # The regression this schedule exists for: without a recheck timer,
        # Posting Queue rows sit in `Verifying` forever.
        self.assertIn("recheck", scheduling.RECOMMENDED_LOOPS)
        self.assertIn("recheck", scheduling.installable_loops())

    def test_every_recommended_loop_is_now_wired(self):
        """`queue` and `retry` were pending while they were being written; they
        are wired now, so nothing recommended should still be unrunnable. A name
        appearing here again means the spec names a loop the CLI cannot run."""
        self.assertEqual(scheduling.pending_loops(), ())
        for loop in ("queue", "retry"):
            self.assertIn(loop, scheduling.installable_loops())

    def test_a_loop_the_cli_does_not_know_stays_pending(self):
        """The tolerance itself, tested with a name that will never be real --
        installing a timer for a command that does not exist would fail every
        tick and bury genuine errors in the journal."""
        with mock.patch.object(scheduling, "cli_loops", return_value=("posting",)), \
             mock.patch.object(scheduling, "RECOMMENDED_LOOPS", ("posting", "not-a-loop")):
            self.assertIn("not-a-loop", scheduling.pending_loops())
            self.assertNotIn("not-a-loop", scheduling.installable_loops())

    def test_installable_loops_are_exactly_the_recommended_ones_the_cli_knows(self):
        known = set(scheduling.cli_loops())
        self.assertEqual(set(scheduling.installable_loops()),
                         set(scheduling.RECOMMENDED_LOOPS) & known)
        self.assertEqual(set(scheduling.installable_loops()) | set(scheduling.pending_loops()),
                         set(scheduling.RECOMMENDED_LOOPS))

    def test_cli_loops_falls_back_when_run_loop_cannot_be_imported(self):
        """The installer box may not have the device/Airtable deps importable."""
        with mock.patch.dict("sys.modules", {"adb_bot.automation.run_loop": None}):
            self.assertEqual(scheduling.cli_loops(), tuple(schedule_spec.LOOPS))


class IntervalTest(unittest.TestCase):
    def test_every_recommended_loop_has_an_interval(self):
        for loop in scheduling.RECOMMENDED_LOOPS:
            self.assertIn(loop, scheduling.RECOMMENDED_INTERVALS, loop)
            self.assertGreater(scheduling.recommended_interval(loop), 0)

    def test_recheck_interval_matches_the_airtable_eligibility_delay(self):
        """Rows become eligible RECHECK_DELAY_SECONDS after an unproven post;
        ticking at that same period bounds the wait at one extra tick."""
        from adb_bot.clients import airtable as at
        self.assertEqual(scheduling.recommended_interval("recheck") * 60,
                         at.RECHECK_DELAY_SECONDS)

    def test_posting_ticks_faster_than_the_loops_that_feed_it(self):
        self.assertLessEqual(scheduling.recommended_interval("posting"),
                             scheduling.recommended_interval("queue"))
        self.assertLessEqual(scheduling.recommended_interval("queue"),
                             scheduling.recommended_interval("pipeline"))

    def test_unknown_loop_gets_a_fallback_instead_of_raising(self):
        # A loop added to the CLI before anyone agrees a cadence must still be
        # installable rather than blowing up the installer.
        self.assertEqual(scheduling.recommended_interval("brand-new-loop"),
                         schedule_spec.FALLBACK_INTERVAL_MIN)

    def test_default_intervals_alias_still_works(self):
        # ui.py and both backends read DEFAULT_INTERVALS.
        self.assertIs(scheduling.DEFAULT_INTERVALS, scheduling.RECOMMENDED_INTERVALS)


class TimerReportTest(unittest.TestCase):
    def _all_ready(self):
        return {loop: "Ready" for loop in scheduling.installable_loops()}

    def test_full_set_installed_is_ok(self):
        with fake_systemd(self._all_ready()):
            report = scheduling.timer_report()
        self.assertTrue(report.ok)
        self.assertEqual(report.missing, ())
        self.assertEqual(set(report.live), set(scheduling.installable_loops()))
        self.assertIn("systemd timers", report.summary())
        self.assertEqual(report.hint(), "")

    def test_missing_loops_are_named(self):
        status = self._all_ready()
        del status["recheck"]
        del status["cleanup"]
        with fake_systemd(status):
            report = scheduling.timer_report()
        self.assertFalse(report.ok)
        self.assertEqual(set(report.missing), {"recheck", "cleanup"})
        self.assertIn("recheck", report.summary())
        self.assertIn("posting", report.summary())  # still reports what *is* live
        self.assertIn("install_units.sh", report.hint())

    def test_installed_but_disabled_counts_as_not_running(self):
        status = self._all_ready()
        status["posting"] = "Disabled"
        with fake_systemd(status):
            report = scheduling.timer_report()
        self.assertFalse(report.ok)
        self.assertEqual(report.stopped, {"posting": "Disabled"})
        self.assertEqual(report.missing, ())
        self.assertIn("not running", report.summary())

    def test_nothing_installed_lists_every_loop(self):
        with fake_systemd({loop: None for loop in scheduling.installable_loops()}):
            report = scheduling.timer_report()
        self.assertFalse(report.ok)
        self.assertEqual(set(report.missing), set(scheduling.installable_loops()))
        self.assertEqual(report.live, {})

    def test_loops_not_wired_yet_are_reported_but_not_a_failure(self):
        """A recommended loop the CLI cannot run yet is surfaced, but must not
        fail the check -- it is work in progress, not a broken install."""
        with mock.patch.object(scheduling, "cli_loops", return_value=("posting",)), \
             mock.patch.object(scheduling, "RECOMMENDED_LOOPS", ("posting", "not-a-loop")), \
             fake_systemd({"posting": "Ready"}):
            report = scheduling.timer_report()
        self.assertEqual(set(report.pending), {"not-a-loop"})
        self.assertTrue(report.ok)
        self.assertIn("not-a-loop", report.summary())

    def test_stray_timer_outside_the_recommended_set_is_surfaced(self):
        status = self._all_ready()
        status["old-posting"] = "Ready"
        with fake_systemd(status):
            report = scheduling.timer_report()
        self.assertEqual(report.extra, {"old-posting": "Ready"})

    def test_status_falls_back_to_a_per_loop_query(self):
        """A loop the backend's listing does not enumerate (one that reached the
        CLI before the spec) must still be reported, not counted as missing."""
        status = self._all_ready()
        with fake_systemd(status, listed=["posting"]):
            report = scheduling.timer_report()
        self.assertTrue(report.ok)
        self.assertEqual(set(report.live), set(scheduling.installable_loops()))

    def test_injected_status_is_used_verbatim(self):
        with fake_systemd({}):  # backend listing would say "nothing installed"
            report = scheduling.timer_report(status=self._all_ready())
        self.assertTrue(report.ok)

    def test_no_backend_reports_everything_missing_with_a_reason(self):
        with mock.patch.object(scheduler_admin, "is_supported", return_value=False), \
             mock.patch.object(sd, "is_supported", return_value=False):
            report = scheduling.timer_report()
        self.assertFalse(report.supported)
        self.assertFalse(report.ok)
        self.assertEqual(set(report.missing), set(scheduling.installable_loops()))
        self.assertTrue(report.hint())


class DoctorSchedulerCheckTest(unittest.TestCase):
    def test_doctor_names_the_missing_loops(self):
        from adb_bot.automation import doctor
        status = {loop: "Ready" for loop in scheduling.installable_loops()}
        del status["recheck"]
        with fake_systemd(status):
            result = doctor.check_scheduler()
        self.assertEqual(result.status, doctor.WARN)
        self.assertIn("recheck", result.detail)
        self.assertTrue(result.hint)

    def test_doctor_passes_when_everything_is_scheduled(self):
        from adb_bot.automation import doctor
        with fake_systemd({loop: "Ready" for loop in scheduling.installable_loops()}):
            result = doctor.check_scheduler()
        self.assertEqual(result.status, doctor.PASS)


class InstallScriptTest(unittest.TestCase):
    """The shell installer keeps its own loop list for --remove; it has to cover
    everything this module may have installed, or removal leaves timers behind."""

    def _script(self):
        return (Path(__file__).resolve().parents[1]
                / "deploy" / "systemd" / "install_units.sh").read_text(encoding="utf-8")

    def test_remove_list_covers_everything_that_may_be_on_disk(self):
        """Including the manual-only loops. They are never *installed* by this
        script, but a person may have installed one by hand -- and `--remove`
        saying "unregister every loop" while leaving an armed MultiLogin writer
        firing is a worse trap than the one MANUAL_ONLY closes."""
        match = re.search(r"^LOOPS=\(([^)]*)\)", self._script(), re.MULTILINE)
        self.assertIsNotNone(match, "install_units.sh no longer declares LOOPS=(...)")
        self.assertEqual(set(match.group(1).split()),
                         set(scheduling.RECOMMENDED_LOOPS)
                         | set(schedule_spec.MANUAL_ONLY_LOOPS))

    def test_the_scripts_manual_only_list_matches_the_spec(self):
        """The script filters what Python hands it, so the two lists have to
        agree or the guard silently stops covering the loop it names."""
        match = re.search(r"^MANUAL_ONLY=\(([^)]*)\)", self._script(), re.MULTILINE)
        self.assertIsNotNone(match, "install_units.sh no longer declares MANUAL_ONLY=(...)")
        self.assertEqual(set(match.group(1).split()),
                         set(schedule_spec.MANUAL_ONLY_LOOPS))


if __name__ == "__main__":
    unittest.main()
