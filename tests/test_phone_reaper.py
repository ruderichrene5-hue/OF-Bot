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


def cmdline(*args) -> str:
    """A /proc cmdline as `_read_cmdline` returns it: NUL-separated, NUL-ended."""
    return "".join(arg + "\x00" for arg in args)


#: The console URL the current launcher is given, with the parameters that sit
#: on either side of `envName` kept -- they are what a greedy pattern eats.
CONSOLE_URL = ("https://phone.geelark.com/index.html?isApi=true&target=SG"
               "&id=632162578940821574&envName={name}&envNo=274493&w=336"
               "&token=eyJhbGciOiJIUzI1NiJ9.abc&lang=en-US&center=false")


class NameFromCmdlineTest(unittest.TestCase):
    """Where the profile's name is read from.

    The launcher moved the name from a `-n` flag into the `-u` console URL, and
    nothing failed loudly when it did -- every live phone simply went nameless,
    and the dashboard's "Live right now" table showed a column of "unknown".
    These pin both spellings so a launcher update cannot quietly do it again.
    """

    def test_the_name_comes_from_the_console_url_when_there_is_no_n_flag(self):
        line = cmdline("phone_launcher_linux_amd64",
                       "-u", CONSOLE_URL.format(name="Kathi 9"),
                       "-p", "632162578940821574")
        self.assertEqual(phone_reaper._name_from_cmdline(line), "Kathi 9")

    def test_a_name_stops_at_the_next_parameter(self):
        """Not "Kathi 9&envNo=274493&w=336&token=..." -- the whole rest of the URL."""
        line = cmdline("-u", CONSOLE_URL.format(name="Kathi 9"))
        self.assertNotIn("&", phone_reaper._name_from_cmdline(line))

    def test_the_n_flag_still_wins_where_a_launcher_passes_one(self):
        line = cmdline("-u", CONSOLE_URL.format(name="from url"),
                       "-n", "Kathi 9", "-p", "632162578940821574")
        self.assertEqual(phone_reaper._name_from_cmdline(line), "Kathi 9")

    def test_a_percent_encoded_name_is_decoded(self):
        line = cmdline("-u", CONSOLE_URL.format(name="Kathi%209"))
        self.assertEqual(phone_reaper._name_from_cmdline(line), "Kathi 9")

    def test_a_phone_with_no_name_anywhere_is_empty_not_a_placeholder(self):
        """`Phone.label` is what turns this into an id; it must have the chance."""
        line = cmdline("phone_launcher_linux_amd64", "-p", "632162578940821574")
        self.assertEqual(phone_reaper._name_from_cmdline(line), "")


class ListPhonesTest(unittest.TestCase):
    def _list(self, line):
        completed = mock.Mock(stdout="4242\n")
        with mock.patch.object(phone_reaper.subprocess, "run", return_value=completed), \
             mock.patch.object(phone_reaper, "_read_cmdline", return_value=line), \
             mock.patch.object(phone_reaper, "_process_age", return_value=60.0), \
             mock.patch.object(phone_reaper, "_rss_mb", return_value=170.0):
            return phone_reaper.list_phones()

    def test_a_live_phone_carries_its_id_and_its_name(self):
        found = self._list(cmdline("phone_launcher_linux_amd64",
                                   "-u", CONSOLE_URL.format(name="Viktoria 9"),
                                   "-p", "625727194475528237"))
        self.assertEqual([(p.profile_id, p.name) for p in found],
                         [("625727194475528237", "Viktoria 9")])

    def test_an_unreadable_name_leaves_an_id_to_act_on(self):
        found = self._list(cmdline("phone_launcher_linux_amd64",
                                   "-p", "625727194475528237"))
        self.assertEqual(found[0].label, "625727194475528237")


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
