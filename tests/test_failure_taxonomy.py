"""What the failure taxonomy claims about a log, and what it must not.

The tool exists to decide which fault gets fixed first, so a miscount points the
next day's work at the wrong thing. The counting rule that matters: a launch is
a launch line, not a mention. `nikki 8` appeared 1270 times in six hours of the
recheck journal and had launched 12 times -- the rest was one HTTP response
logged repeatedly. Counting mentions turned a self-healing row into a fake
capacity crisis.
"""

import unittest

from adb_bot.automation import failure_taxonomy as ft


LOG = """
2026-08-14 14:18:53 | WARNING | The account switcher on 1.2.3.4:20551 does not list @helenisyourebabe -- Airtable says this phone has it, the phone disagrees
2026-08-14 14:18:54 | INFO | Post result for helenisyourebabe (profile 625727378051825764): failed
2026-08-14 14:11:12 | ERROR | Failed to launch profile 626547576228806881 -- MultiLogin-side 500 (their cloud)
2026-08-14 14:14:04 | WARNING | Device 1.2.3.4:39805 connected but never reached 'device' state
2026-08-14 14:15:44 | INFO | Post result for helen_aiscooll (profile 626547576228806881): adb_connect_failed
2026-08-14 14:10:46 | INFO | Post result for Laila 4 (profile 625727194475266093): done -- via post_count [strong]
2026-08-14 14:07:08 | INFO | Launched profile 625727194475266093
2026-08-14 14:11:00 | INFO | Relaunch response for 626547576228806881: {'status': 'ok'}
2026-08-14 13:00:00 | INFO | Profile 626547576228806881 has reported 'not running' 3 times in a row
"""


class ClassifyTest(unittest.TestCase):
    def test_each_cause_is_counted_once_per_occurrence(self):
        totals, _ = ft.classify(LOG)
        self.assertEqual(totals["handle-not-on-phone"], 1)
        self.assertEqual(totals["mlx-500"], 1)
        self.assertEqual(totals["device-never-ready"], 1)
        self.assertEqual(totals["launch-did-not-take"], 1)

    def test_a_cause_that_did_not_happen_is_absent(self):
        totals, _ = ft.classify(LOG)
        self.assertEqual(totals["banned"], 0)
        self.assertEqual(totals["checkpoint"], 0)

    def test_failures_are_attributed_to_a_readable_name(self):
        """A profile id is not something anyone can act on."""
        _, who = ft.classify(LOG)
        self.assertEqual(who["mlx-500"].most_common(1)[0][0], "helen_aiscooll")

    def test_an_unmatched_id_still_reports_the_raw_key(self):
        """Better a bare id than dropping the row."""
        _, who = ft.classify(
            "Profile 999 has reported 'not running' 2 times in a row")
        self.assertEqual(who["launch-did-not-take"].most_common(1)[0][0], "999")


class OutcomeTest(unittest.TestCase):
    def test_terminal_statuses_are_tallied(self):
        result = ft.outcomes(LOG)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["adb_connect_failed"], 1)


class LaunchCountTest(unittest.TestCase):
    """Launches are counted from launch lines only."""

    def test_launches_and_relaunches_both_count(self):
        launches = ft.launches_by_profile(LOG)
        self.assertEqual(launches["625727194475266093"], 1)
        self.assertEqual(launches["626547576228806881"], 1)

    def test_merely_mentioning_a_profile_is_not_a_launch(self):
        """The mistake that invented a capacity crisis on 2026-08-14."""
        noisy = "\n".join(
            ['[HTTP] Response: {"items":[{"id":"626549106931662984"}]}'] * 50)
        self.assertEqual(ft.launches_by_profile(noisy), {})

    def test_a_log_with_no_launches_is_empty_not_an_error(self):
        self.assertEqual(ft.launches_by_profile(""), {})


if __name__ == "__main__":
    unittest.main()
