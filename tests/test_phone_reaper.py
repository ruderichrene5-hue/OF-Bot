"""Reaping phones nothing owns.

The danger is the opposite of the bug: a reaper that closes a phone mid-post is
far worse than one that leaves a leak for another 20 minutes. These tests pin
the conditions that keep it from touching live work.
"""

import unittest
from unittest import mock

from adb_bot.automation import phone_reaper
from adb_bot.core import locks


def phone(pid, profile_id="", name="", age=0.0):
    return phone_reaper.Phone(pid=pid, profile_id=profile_id, name=name, age_seconds=age)


class FindOrphansTest(unittest.TestCase):
    OLD = phone_reaper.DEFAULT_MIN_AGE_SECONDS + 60

    def _unlocked(self):
        return mock.patch.object(locks, "is_locked", return_value=False)

    def _locked(self):
        return mock.patch.object(locks, "is_locked", return_value=True)

    def test_an_old_unlocked_phone_is_an_orphan(self):
        with self._unlocked():
            found = phone_reaper.find_orphans([phone(1, "111", "Jil 1", self.OLD)])
        self.assertEqual([p.pid for p in found], [1])

    def test_a_young_phone_is_never_touched(self):
        """Even with no lock: a run that just started has not taken one yet."""
        with self._unlocked():
            found = phone_reaper.find_orphans([phone(1, "111", "Jil 1", 60)])
        self.assertEqual(found, [])

    def test_a_locked_phone_is_never_touched_however_old(self):
        with self._locked():
            found = phone_reaper.find_orphans([phone(1, "111", "Jil 1", self.OLD * 10)])
        self.assertEqual(found, [])

    def test_an_unidentifiable_old_phone_still_counts(self):
        """A broken launch leaves exactly this, and it is the easiest to miss."""
        with self._unlocked():
            found = phone_reaper.find_orphans([phone(2, "", "", self.OLD)])
        self.assertEqual([p.pid for p in found], [2])

    def test_an_unreadable_lock_state_means_leave_it_alone(self):
        with mock.patch.object(locks, "is_locked", side_effect=OSError("boom")):
            found = phone_reaper.find_orphans([phone(1, "111", "Jil 1", self.OLD)])
        self.assertEqual(found, [], "uncertainty must not become a kill")

    def test_the_threshold_matches_the_lock_ttl(self):
        # If these drift, the reaper and the locks disagree about "abandoned".
        self.assertEqual(phone_reaper.DEFAULT_MIN_AGE_SECONDS, locks.DEFAULT_TTL_SECONDS)


class ReapTest(unittest.TestCase):
    OLD = phone_reaper.DEFAULT_MIN_AGE_SECONDS + 60

    def setUp(self):
        self.logger = mock.Mock()
        self.orphan = phone(11, "111", "Jil 1", self.OLD)
        self.live = phone(22, "222", "Jil 2", 30)

    def _run(self, dry_run=False, shutdown_client=None, alive=lambda pid: False, kill=None):
        with mock.patch.object(phone_reaper, "list_phones", return_value=[self.orphan, self.live]), \
             mock.patch.object(locks, "is_locked", return_value=False), \
             mock.patch.object(phone_reaper, "_still_alive", side_effect=alive), \
             mock.patch.object(phone_reaper.os, "kill", side_effect=kill or (lambda *a: None)), \
             mock.patch.object(phone_reaper.time, "sleep"):
            return phone_reaper.reap(shutdown_client=shutdown_client, logger=self.logger,
                                     dry_run=dry_run)

    def test_dry_run_closes_nothing(self):
        killed = []
        report = self._run(dry_run=True, kill=lambda pid, sig: killed.append(pid))
        self.assertEqual(len(report.orphans), 1)
        self.assertEqual(report.killed, [])
        self.assertEqual(killed, [])

    def test_the_api_is_asked_first_and_the_process_is_left_alone_if_it_works(self):
        client = mock.Mock()
        killed = []
        report = self._run(shutdown_client=client, alive=lambda pid: False,
                           kill=lambda pid, sig: killed.append(pid))
        client.shutdown_profiles.assert_called_once_with(["111"])
        self.assertEqual([p.pid for p in report.shut_down], [11])
        self.assertEqual(killed, [], "process was signalled even though the API closed it")

    def test_it_falls_back_to_signals_when_the_api_does_not_close_it(self):
        """What actually happened on 2026-08-05: HTTP 200, phone still running."""
        client = mock.Mock()
        states = {11: [True, True, False]}     # alive, alive, then gone after SIGTERM
        report = self._run(shutdown_client=client,
                           alive=lambda pid: states[pid].pop(0) if states.get(pid) else False)
        self.assertEqual([p.pid for p in report.killed], [11])
        self.assertEqual(report.shut_down, [])

    def test_a_failing_api_does_not_stop_the_reap(self):
        client = mock.Mock()
        client.shutdown_profiles.side_effect = RuntimeError("connection refused")
        states = {11: [True, False]}
        report = self._run(shutdown_client=client,
                           alive=lambda pid: states[pid].pop(0) if states.get(pid) else False)
        self.assertEqual([p.pid for p in report.killed], [11])
        self.assertTrue(report.errors)

    def test_only_the_orphan_is_ever_signalled(self):
        killed = []
        states = {11: [True, False]}
        self._run(alive=lambda pid: states[pid].pop(0) if states.get(pid) else False,
                  kill=lambda pid, sig: killed.append(pid))
        self.assertEqual(set(killed), {11}, "a live phone was signalled")

    def test_a_phone_that_survives_everything_is_reported_not_hidden(self):
        report = self._run(alive=lambda pid: True)
        self.assertEqual([p.pid for p in report.survived], [11])
        self.assertEqual(report.killed, [])

    def test_nothing_to_do_is_quiet(self):
        with mock.patch.object(phone_reaper, "list_phones", return_value=[self.live]), \
             mock.patch.object(locks, "is_locked", return_value=False):
            report = phone_reaper.reap(logger=self.logger, dry_run=False)
        self.assertEqual(report.orphans, [])
        self.assertEqual(report.summary().split()[1], "orphans=0")


class ScheduleTest(unittest.TestCase):
    def test_the_reaper_is_scheduled(self):
        from adb_bot.automation import schedule_spec, scheduling
        self.assertIn("reap-phones", schedule_spec.RECOMMENDED_LOOPS)
        self.assertEqual(schedule_spec.RECOMMENDED_INTERVALS["reap-phones"], 20)
        self.assertIn("reap-phones", scheduling.installable_loops())

    def test_its_cadence_is_shorter_than_the_age_threshold(self):
        """Otherwise a leak can outlive several ticks before anything sees it."""
        from adb_bot.automation import schedule_spec
        self.assertLess(schedule_spec.RECOMMENDED_INTERVALS["reap-phones"] * 60,
                        phone_reaper.DEFAULT_MIN_AGE_SECONDS)


if __name__ == "__main__":
    unittest.main()
