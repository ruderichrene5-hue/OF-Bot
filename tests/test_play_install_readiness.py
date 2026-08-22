"""An empty package list is not the same as an absent package.

A phone that has just booted answers the shell before its package manager is
up, and `pm list packages <name>` returns nothing for an app that is installed.
Reading that as "not installed" sent Geelark phones to the Play Store -- signed
out, with nothing to tap -- to fetch an Instagram they already had, burning
100-200s of a phone that lives about fifteen minutes.
"""

import unittest

from adb_bot.automation.flows.play_install import is_installed

PACKAGE = "com.instagram.android"


class FakeAdb:
    """Answers `pm list packages` from a script of replies, in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.commands = []

    def run_command(self, command):
        self.commands.append(command)
        return self.replies.pop(0) if self.replies else ""


class PackageManagerReadinessTest(unittest.TestCase):

    def test_a_silent_package_manager_is_retried_not_believed(self):
        # Filtered query empty, unfiltered empty too -> pm is not up yet.
        # Second round the phone has woken up and the package is there.
        adb = FakeAdb(["", "", f"package:{PACKAGE}\n"])
        self.assertTrue(is_installed(adb, "t", PACKAGE, sleep=lambda _s: None))

    def test_a_package_manager_that_answers_is_believed(self):
        # Filtered query empty, but the unfiltered list is full -- so the
        # package manager is alive and the app genuinely is not there. No
        # retry: this is a real "no", and waiting on it wastes the phone.
        adb = FakeAdb(["", "package:com.android.settings\n"])
        self.assertFalse(is_installed(adb, "t", PACKAGE, sleep=lambda _s: None))
        self.assertEqual(len(adb.commands), 2)

    def test_an_installed_package_answers_immediately(self):
        adb = FakeAdb([f"package:{PACKAGE}\n"])
        self.assertTrue(is_installed(adb, "t", PACKAGE, sleep=lambda _s: None))
        self.assertEqual(len(adb.commands), 1)

    def test_a_phone_that_never_wakes_up_gives_up(self):
        adb = FakeAdb([""] * 10)
        self.assertFalse(is_installed(adb, "t", PACKAGE, sleep=lambda _s: None))

    def test_a_substring_match_is_still_not_a_match(self):
        """com.google.android.gm must not match com.google.android.gms."""
        adb = FakeAdb(["package:com.google.android.gms\n"])
        self.assertFalse(is_installed(adb, "t", "com.google.android.gm",
                                      sleep=lambda _s: None))


if __name__ == "__main__":
    unittest.main()
