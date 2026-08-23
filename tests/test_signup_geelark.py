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
from adb_bot.automation.signup_geelark import (CONNECTED_TAG, FAILED_TAG,
                                               NEW_TAG, failed_mailboxes,
                                               increment_profiles_created,
                                               reached_instagram, skip_locked,
                                               signup_status_tag, write_back)
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


class SignupStatusTagTest(unittest.TestCase):
    """The three lifecycle tags, and what earns each one.

    Before this, a phone that had genuinely tried and failed kept the exact
    same `new profile` tag as one nobody had ever touched -- no way to tell
    a spent phone from a fresh one without reading its remark.
    """

    def test_a_created_account_earns_connected(self):
        self.assertEqual(signup_status_tag(signup.RESULT_CREATED),
                         CONNECTED_TAG)

    def test_no_attempt_statuses_earn_nothing(self):
        for status in ("busy", "not-ready", "dry-run", "unreachable"):
            with self.subTest(status=status):
                self.assertIsNone(signup_status_tag(status))

    def test_a_checkpoint_is_progress_not_a_failure(self):
        """`created_unverified` is a real account, just not a finished one --
        `write_back`'s own rule is that only a finished signup earns
        `IG connected`, and it would be wrong to call this a failure too."""
        self.assertIsNone(
            signup_status_tag(signup.RESULT_CREATED_UNVERIFIED))

    def test_everything_else_is_a_genuine_failure(self):
        for status in (signup.RESULT_STUCK, "google_robot_check",
                       "wrong_password", "error", "install-gmail-timed_out"):
            with self.subTest(status=status):
                self.assertEqual(signup_status_tag(status), FAILED_TAG)


class _FakeTagClient:
    """Enough of `GeelarkTagClient` for `write_back` to run against."""

    def __init__(self, existing=None):
        self.existing = dict(existing or {NEW_TAG: "id-new-profile"})
        self.ensured = []

    def tag_ids_by_name(self, refresh=False):
        return dict(self.existing)

    def ensure_tag(self, name, color="blue"):
        self.existing.setdefault(name, f"id-{name}")
        self.ensured.append((name, color))
        return self.existing[name]


class WriteBackTagsTest(unittest.TestCase):
    """`write_back` resolves tag *names* to ids through a fresh list every
    call, so the fake has to behave like the real lookup: known names in,
    ids out, nothing invented.
    """

    def setUp(self):
        self.identity = mock.Mock(username="alina", password="pw123")
        self.phones_patch = mock.patch.object(signup_geelark,
                                              "GeelarkPhoneClient")
        self.fake_phones_cls = self.phones_patch.start()
        self.addCleanup(self.phones_patch.stop)

    def _run(self, phone, status, fake_tags):
        with mock.patch("adb_bot.clients.geelark.tags.GeelarkTagClient",
                        return_value=fake_tags):
            write_back(phone, self.identity, "a@gmail.com", status,
                      transport=None, logger=mock.Mock())
        update = self.fake_phones_cls.return_value.update_phone
        tag_ids = update.call_args.kwargs["tag_ids"]
        by_id = {v: k for k, v in fake_tags.existing.items()}
        return [by_id.get(i, i) for i in (tag_ids or [])]

    def test_a_success_replaces_new_profile_with_ig_connected(self):
        phone = {"id": "1", "tags": [{"name": NEW_TAG}]}
        names = self._run(phone, signup.RESULT_CREATED, _FakeTagClient())

        self.assertIn(CONNECTED_TAG, names)
        self.assertNotIn(NEW_TAG, names)

    def test_a_genuine_failure_gets_signup_failed_not_new_profile(self):
        phone = {"id": "1", "tags": [{"name": NEW_TAG}]}
        names = self._run(phone, signup.RESULT_STUCK, _FakeTagClient())

        self.assertIn(FAILED_TAG, names)
        self.assertNotIn(NEW_TAG, names)

    def test_a_checkpoint_leaves_new_profile_alone(self):
        phone = {"id": "1", "tags": [{"name": NEW_TAG}]}
        names = self._run(phone, signup.RESULT_CREATED_UNVERIFIED,
                          _FakeTagClient())

        self.assertIn(NEW_TAG, names)
        self.assertNotIn(CONNECTED_TAG, names)
        self.assertNotIn(FAILED_TAG, names)

    def test_a_success_after_an_earlier_failure_clears_signup_failed(self):
        """A phone can be retried after `Signup Failed` -- succeeding this
        time must not leave the old failure tag sitting alongside the new
        `IG connected` one."""
        phone = {"id": "1", "tags": [{"name": FAILED_TAG}]}
        fake_tags = _FakeTagClient(
            existing={NEW_TAG: "id-new-profile", FAILED_TAG: "id-failed"})
        names = self._run(phone, signup.RESULT_CREATED, fake_tags)

        self.assertIn(CONNECTED_TAG, names)
        self.assertNotIn(FAILED_TAG, names)


class IncrementProfilesCreatedTest(unittest.TestCase):
    """`Models.Profiles Created` is a convenience counter, not the source of
    truth -- a model missing from Airtable or a request that fails must
    never stop the signup itself from being recorded.
    """

    def setUp(self):
        self.at_patch = mock.patch.object(signup_geelark, "AirtableClient")
        self.fake_at_cls = self.at_patch.start()
        self.addCleanup(self.at_patch.stop)
        self.fake_client = self.fake_at_cls.return_value

        self.settings_patch = mock.patch.object(signup_geelark, "settings")
        fake_settings = self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        fake_settings.get_saved_airtable_token.return_value = "tok"
        fake_settings.get_saved_airtable_base_id.return_value = "app123"

    def test_a_blank_model_never_calls_airtable_at_all(self):
        increment_profiles_created("", mock.Mock())

        self.fake_at_cls.assert_not_called()

    def test_a_known_model_gets_its_count_bumped_by_one(self):
        self.fake_client.models_by_name.return_value = {"nikki": "recNikki"}
        self.fake_client._get_fields.return_value = {"Profiles Created": 4}

        increment_profiles_created("Nikki", mock.Mock())

        self.fake_client._patch_in.assert_called_once_with(
            signup_geelark.TABLE_MODELS, "recNikki", {"Profiles Created": 5})

    def test_a_missing_count_field_starts_from_zero(self):
        self.fake_client.models_by_name.return_value = {"nikki": "recNikki"}
        self.fake_client._get_fields.return_value = {}

        increment_profiles_created("Nikki", mock.Mock())

        self.fake_client._patch_in.assert_called_once_with(
            signup_geelark.TABLE_MODELS, "recNikki", {"Profiles Created": 1})

    def test_a_model_with_no_models_row_is_skipped_without_crashing(self):
        self.fake_client.models_by_name.return_value = {}
        logger = mock.Mock()

        increment_profiles_created("Nikki", logger)

        self.fake_client._patch_in.assert_not_called()
        logger.warning.assert_called_once()

    def test_an_airtable_exception_is_caught_not_raised(self):
        self.fake_client.models_by_name.side_effect = RuntimeError("boom")
        logger = mock.Mock()

        increment_profiles_created("Nikki", logger)  # must not raise

        logger.warning.assert_called_once()


class PhoneProxyPortTest(unittest.TestCase):
    """The batch pipeline was starting phones with no check that another
    phone was already running on the same statically-assigned proxy port --
    confirmed live 2026-08-23, fixed by leasing this port through
    `GeelarkHost`. This is the extraction half of that fix: the phone dict
    already carries its proxy (from the same `list_phones()` call `main()`
    uses to build the worklist), so no extra API call is needed."""

    def test_a_phones_own_port_is_read_from_its_proxy_field(self):
        phone = {"id": "1", "proxy": {"type": "socks5",
                                      "server": "162.55.84.35", "port": 54018}}

        self.assertEqual(signup_geelark._phone_proxy_port(phone), 54018)

    def test_no_proxy_field_is_none_not_zero(self):
        self.assertIsNone(signup_geelark._phone_proxy_port({"id": "1"}))
        self.assertIsNone(
            signup_geelark._phone_proxy_port({"id": "1", "proxy": {}}))


if __name__ == "__main__":
    unittest.main()
