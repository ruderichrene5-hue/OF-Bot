"""What may be spent, and when.

Both rules here cost real, non-recoverable things when they are wrong: a
wrongly-claimed mailbox is a pool row gone plus a `Profile Creation` entry for
a person who does not exist, and a wrongly-retired phone throws away a good
device because a Gmail row was bad. The first Geelark batch got both wrong in
the same run.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adb_bot.automation import signup_geelark
from adb_bot.automation.flows import signup
from adb_bot.automation.signup_geelark import failed_mailboxes, reached_instagram


class FailedMailboxesTest(unittest.TestCase):
    """Which addresses are never offered again.

    Only Google's verdicts on the address itself. A run that ended because the
    phone would not produce a UI dump has learned nothing about the mailbox --
    and mailboxes are the scarce thing here, so retiring one on that evidence
    is the expensive direction to be wrong in.
    """

    def _ledger(self, rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.close()
        return Path(handle.name)

    def test_googles_own_verdicts_retire_an_address(self):
        path = self._ledger([
            {"email": "a@gmail.com", "status": "mailbox-wrong_password"},
            {"email": "b@gmail.com", "status": "mailbox-google_robot_check"},
        ])
        with mock.patch.object(signup_geelark, "LEDGER", path):
            self.assertEqual(failed_mailboxes(),
                             {"a@gmail.com", "b@gmail.com"})

    def test_a_phone_that_would_not_dump_does_not(self):
        path = self._ledger([
            {"email": "c@gmail.com", "status": "mailbox-no_ui_dump"},
            {"email": "d@gmail.com", "status": "mailbox-stuck"},
        ])
        with mock.patch.object(signup_geelark, "LEDGER", path):
            self.assertEqual(failed_mailboxes(), set())

    def test_a_finished_signup_does_not_retire_its_address_here(self):
        """That is the claim's job, not the blocklist's."""
        path = self._ledger([{"email": "e@gmail.com", "status": "created"}])
        with mock.patch.object(signup_geelark, "LEDGER", path):
            self.assertEqual(failed_mailboxes(), set())


class ReachedInstagramTest(unittest.TestCase):

    def test_a_failed_google_sign_in_has_touched_nothing(self):
        """Google refused the mailbox, so Instagram never opened.

        Nothing is owed: the address is still free and the phone is still
        blank. Both were spent anyway on 2026-08-21 -- two mailboxes claimed
        and two phones retired for accounts that were never made.
        """
        self.assertFalse(reached_instagram("mailbox-wrong_password"))
        self.assertFalse(reached_instagram("mailbox-stuck"))
        self.assertFalse(reached_instagram("mailbox-google_robot_check"))

    def test_a_phone_that_never_came_up_has_touched_nothing(self):
        for status in ("not-ready", "unreachable", "busy", "dry-run", ""):
            with self.subTest(status=status):
                self.assertFalse(reached_instagram(status))

    def test_an_install_failure_has_touched_nothing(self):
        self.assertFalse(reached_instagram("install-instagram-stuck"))
        self.assertFalse(reached_instagram("install-gmail-unavailable"))

    def test_a_created_account_has(self):
        self.assertTrue(reached_instagram(signup.RESULT_CREATED))
        self.assertTrue(reached_instagram(signup.RESULT_CREATED_UNVERIFIED))

    def test_a_signup_that_broke_mid_chain_counts_too(self):
        """Instagram may already hold the address even though nothing finished.

        The expensive mistake here is the optimistic one: leaving the mailbox
        free after Instagram has seen it hands the same address to the next
        phone, and these mailboxes take one account each.
        """
        for status in (signup.RESULT_STUCK, signup.RESULT_BANNED,
                       signup.RESULT_UNKNOWN_SCREEN, signup.RESULT_PHONE_LOST,
                       "error"):
            with self.subTest(status=status):
                self.assertTrue(reached_instagram(status))


if __name__ == "__main__":
    unittest.main()
