"""What may be spent, and when.

Both rules here cost real, non-recoverable things when they are wrong: a
wrongly-claimed mailbox is a pool row gone plus a `Profile Creation` entry for
a person who does not exist, and a wrongly-retired phone throws away a good
device because a Gmail row was bad. The first Geelark batch got both wrong in
the same run.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from adb_bot.automation import signup_geelark
from adb_bot.automation.flows import signup
from adb_bot.automation.signup_geelark import (failed_mailboxes,
                                               reached_instagram, skip_locked)
from adb_bot.core import locks


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


class NoMailboxTest(unittest.TestCase):
    """`run_phone` has to survive having no mailbox at all.

    The SMS fallback passes `box=None`, and every read of it has to be
    guarded. One that was not -- a print of the address in the run header --
    killed both phones of the first SMS batch before either launched.
    """

    def test_a_run_with_no_mailbox_does_not_blow_up(self):
        from adb_bot.automation import signup_phone

        args = mock.Mock(apply=False, screenshots=False, verify=False,
                         country=None, readiness_attempts=1, readiness_wait=1)
        out = signup_phone.run_phone(
            {"id": "1", "serial_name": "Emely new 1"}, None,
            host=mock.Mock(), adb_client=mock.Mock(), args=args,
            logger=mock.Mock())
        self.assertEqual(out["status"], "dry-run")
        self.assertEqual(out["email"], "")

    def test_a_run_with_a_mailbox_still_carries_its_address(self):
        from adb_bot.automation import signup_phone

        args = mock.Mock(apply=False, screenshots=False, verify=False,
                         country=None, readiness_attempts=1, readiness_wait=1)
        out = signup_phone.run_phone(
            {"id": "1", "serial_name": "Emely new 1"},
            {"address": "someone@gmail.com", "password": "x",
             "totp_secret": ""},
            host=mock.Mock(), adb_client=mock.Mock(), args=args,
            logger=mock.Mock())
        self.assertEqual(out["email"], "someone@gmail.com")


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


class SkipLockedTest(unittest.TestCase):
    """A phone another operator is already driving must not eat a batch slot.

    Two operators running batches from the same untried-phone pool at once
    (2026-08-22: this operator and a manager, both hitting Geelark's four
    slots) turned every collision into a wasted "busy" result instead of the
    next phone in the queue getting a real attempt.
    """

    def setUp(self):
        self.locked_id = f"test_locked_{os.getpid()}_{time.time_ns()}"
        self.addCleanup(locks.release, self.locked_id)
        locks.acquire(self.locked_id, owner="someone-else")

    def test_a_locked_phone_is_set_aside(self):
        free_id = f"test_free_{os.getpid()}_{time.time_ns()}"
        phones = [{"id": self.locked_id, "serialName": "Locked One"},
                 {"id": free_id, "serialName": "Free One"}]

        free, busy = skip_locked(phones)

        self.assertEqual([p["id"] for p in free], [free_id])
        self.assertEqual([p["id"] for p in busy], [self.locked_id])

    def test_nothing_locked_means_nothing_set_aside(self):
        free_id = f"test_free_{os.getpid()}_{time.time_ns()}"
        phones = [{"id": free_id, "serialName": "Free One"}]

        free, busy = skip_locked(phones)

        self.assertEqual(free, phones)
        self.assertEqual(busy, [])


if __name__ == "__main__":
    unittest.main()
