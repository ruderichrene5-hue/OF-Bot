"""The discovery pass: which phones get launched, and what gets written.

The device half (`read_phone_accounts`) needs a real cloud phone and is not
covered here; everything that decides *whether* to launch one, and what to write
afterwards, is.
"""

import logging
from unittest import TestCase

from adb_bot.automation import second_account_sync as sync
from adb_bot.automation.second_account_sync import ProfileAccounts, plan_write

LOG = logging.getLogger("test")


def _mlx(name, tags, serial, ident, remark=""):
    return {"serial_name": name, "tags": tags, "serial_no": serial,
            "id": ident, "remark": remark}


TAGGED = [
    _mlx("Jasmin 5", ["Active / Posting", "Second Account"], "173486", "625615005450240115",
         "@jasjasmin00 second account - Hazel"),
    _mlx("nikki 9", ["2 accounts", "Active / Posting"], "184090", "626549106931728520",
         "@nikk.iie02 second account - Hazel"),
    _mlx("Luisa 9", ["Active / Posting"], "999", "111", "@aminulchow3596 - Hazel"),
]


class FakeAirtable:
    def __init__(self, known=None, write_ok=True):
        self._known = known or {}
        self.writes = []
        self._write_ok = write_ok

    def second_account_profiles(self):
        return self._known

    def record_profile_accounts(self, record_id, primary, second, note=None,
                                existing_notes=None):
        self.writes.append({"record_id": record_id, "primary": primary,
                            "second": second, "note": note})
        return self._write_ok


def _known(serial="173486", record_id="rec1", primary="", second="", has_second=False):
    return {serial: {"record_id": record_id, "name": "Jasmin 5", "launch_id": "x",
                     "primary": primary, "second": second, "has_second": has_second,
                     "checked_at": None}}


class PlanWriteTest(TestCase):
    def test_a_new_second_account_is_written(self):
        observed = ProfileAccounts("173486", "x", "Jasmin 5",
                                   primary="jasmindiecoolee", second="naughty_jasminn")
        patch = plan_write(observed, {"primary": "", "second": "", "has_second": False})
        self.assertEqual(patch, {"primary": "jasmindiecoolee",
                                 "second": "naughty_jasminn", "has_second": True})

    def test_a_row_that_already_says_this_is_not_rewritten(self):
        observed = ProfileAccounts("173486", "x", "Jasmin 5",
                                   primary="jasmindiecoolee", second="naughty_jasminn")
        self.assertIsNone(plan_write(observed, {"primary": "jasmindiecoolee",
                                                "second": "naughty_jasminn",
                                                "has_second": True}))

    def test_an_at_prefixed_stored_handle_counts_as_the_same(self):
        """A human typing "@name" must not make every run rewrite the row."""
        observed = ProfileAccounts("173486", "x", "Jasmin 5",
                                   primary="jasmindiecoolee", second="naughty_jasminn")
        self.assertIsNone(plan_write(observed, {"primary": "@JasminDiecoolee",
                                                "second": "@Naughty_Jasminn",
                                                "has_second": True}))

    def test_a_phone_with_only_one_account_clears_the_flag(self):
        """MLX tagged it, the phone disagrees. The phone wins -- otherwise the
        queue keeps making a second slot that can only fail."""
        observed = ProfileAccounts("173486", "x", "Jasmin 5", primary="onlyone", second="")
        patch = plan_write(observed, {"primary": "onlyone", "second": "gone",
                                      "has_second": True})
        self.assertEqual(patch, {"primary": "onlyone", "second": "", "has_second": False})

    def test_a_failed_read_writes_nothing(self):
        """Never overwrite good data with the result of a phone that wouldn't boot."""
        observed = ProfileAccounts("173486", "x", "Jasmin 5", error="phone never became ADB-ready")
        self.assertIsNone(plan_write(observed, _known()["173486"]))
        self.assertIsNone(plan_write(observed, None))


class RunSyncTest(TestCase):
    def _run(self, airtable, **kwargs):
        calls = []

        def fake_read(launch_id, name, serial_no, **_kw):
            calls.append(name)
            return ProfileAccounts(serial_no, launch_id, name,
                                   primary="jasmindiecoolee", second="naughty_jasminn",
                                   all_handles=["jasmindiecoolee", "naughty_jasminn"])

        original = sync.read_phone_accounts
        sync.read_phone_accounts = fake_read
        try:
            report = sync.run_second_account_sync(
                airtable, TAGGED, api_client=None, adb_enable_client=None,
                launcher_client=None, shutdown_client=None, logger=LOG, **kwargs)
        finally:
            sync.read_phone_accounts = original
        return report, calls

    def test_a_dry_run_launches_no_phones(self):
        """Twenty launches is the better part of an hour -- never something you
        get by forgetting --apply."""
        airtable = FakeAirtable(known=_known())
        report, calls = self._run(airtable, dry_run=True)

        self.assertEqual(calls, [])
        self.assertEqual(airtable.writes, [])
        self.assertTrue(report.dry_run)
        # ...and it says so honestly: nothing was read, so nothing is "updated".
        self.assertEqual(report.updated, [])
        self.assertEqual(report.to_check, ["Jasmin 5"])
        self.assertIn("would check=1", report.summary())

    def test_untagged_profiles_are_never_launched(self):
        airtable = FakeAirtable(known=_known())
        _report, calls = self._run(airtable, dry_run=False)
        self.assertNotIn("Luisa 9", calls)

    def test_only_tagged_phones_with_an_airtable_row_are_read(self):
        """nikki 9 is tagged but has no Profiles row here, so there is nowhere
        to record the answer -- launching it would waste two minutes."""
        airtable = FakeAirtable(known=_known())
        report, calls = self._run(airtable, dry_run=False)

        self.assertEqual(calls, ["Jasmin 5"])
        self.assertTrue(any("no Profiles (Cloning) row" in reason
                            for _n, reason in report.skipped))

    def test_the_write_uses_what_the_phone_said_not_the_mlx_remark(self):
        """Jasmin 5's remark claims @jasjasmin00; the phone says otherwise."""
        airtable = FakeAirtable(known=_known())
        self._run(airtable, dry_run=False)

        self.assertEqual(len(airtable.writes), 1)
        self.assertEqual(airtable.writes[0]["primary"], "jasmindiecoolee")
        self.assertEqual(airtable.writes[0]["second"], "naughty_jasminn")
        self.assertNotIn("jasjasmin00", str(airtable.writes[0]))

    def test_phones_already_recorded_are_skipped_on_a_repeat_run(self):
        airtable = FakeAirtable(known=_known(primary="jasmindiecoolee",
                                             second="naughty_jasminn", has_second=True))
        report, calls = self._run(airtable, dry_run=False)

        self.assertEqual(calls, [])
        self.assertTrue(any("already recorded" in reason for _n, reason in report.skipped))

    def test_recheck_re_reads_a_phone_that_is_already_recorded(self):
        airtable = FakeAirtable(known=_known(primary="old_handle", second="old_second",
                                             has_second=True))
        _report, calls = self._run(airtable, dry_run=False, recheck_known=True)

        self.assertEqual(calls, ["Jasmin 5"])
        self.assertEqual(airtable.writes[0]["primary"], "jasmindiecoolee")

    def test_max_phones_caps_how_many_get_launched(self):
        airtable = FakeAirtable(known={
            **_known(),
            "184090": {"record_id": "rec2", "name": "nikki 9", "launch_id": "y",
                       "primary": "", "second": "", "has_second": False, "checked_at": None},
        })
        report, calls = self._run(airtable, dry_run=False, limit=1)

        self.assertEqual(len(calls), 1)
        self.assertTrue(any("run cap" in reason for _n, reason in report.skipped))

    def test_only_serials_restricts_to_one_phone(self):
        airtable = FakeAirtable(known={
            **_known(),
            "184090": {"record_id": "rec2", "name": "nikki 9", "launch_id": "y",
                       "primary": "", "second": "", "has_second": False, "checked_at": None},
        })
        _report, calls = self._run(airtable, dry_run=False, only_serials=["184090"])
        self.assertEqual(calls, ["nikki 9"])

    def test_a_tagging_gap_in_mlx_is_reported(self):
        """Nikki 14's remark names two handles but nobody tagged the profile, so
        it silently posts half as much as it could."""
        items = TAGGED + [_mlx("Nikki 14", ["Active / Posting"], "170000", "620000",
                               "@lamgirmina - Hazel\n@minacr2914 second account")]
        airtable = FakeAirtable(known=_known())

        original = sync.read_phone_accounts
        sync.read_phone_accounts = lambda *a, **k: ProfileAccounts(
            "173486", "x", "Jasmin 5", primary="a", second="b")
        try:
            report = sync.run_second_account_sync(
                airtable, items, api_client=None, adb_enable_client=None,
                launcher_client=None, shutdown_client=None, logger=LOG, dry_run=False)
        finally:
            sync.read_phone_accounts = original

        self.assertEqual([name for name, _ in report.untagged_hints], ["Nikki 14"])

    def test_an_unread_switcher_never_clears_a_recorded_second_account(self):
        """`read_phone_accounts` reports that case as an error, and an errored
        read must not reach Airtable -- otherwise one flaky tap demotes a
        two-account phone to single and halves its posting."""
        airtable = FakeAirtable(known=_known(primary="jasmindiecoolee",
                                             second="naughty_jasminn", has_second=True))
        original = sync.read_phone_accounts
        sync.read_phone_accounts = lambda launch_id, name, serial_no, **k: ProfileAccounts(
            serial_no, launch_id, name, primary="", second="",
            error="could not read the account switcher (header said 'jasmindiecoolee')")
        try:
            report = sync.run_second_account_sync(
                airtable, TAGGED, api_client=None, adb_enable_client=None,
                launcher_client=None, shutdown_client=None, logger=LOG,
                dry_run=False, recheck_known=True)
        finally:
            sync.read_phone_accounts = original

        self.assertEqual(airtable.writes, [])
        self.assertEqual([n for n, _ in report.failed], ["Jasmin 5"])

    def test_a_phone_that_would_not_boot_is_reported_and_writes_nothing(self):
        airtable = FakeAirtable(known=_known())
        original = sync.read_phone_accounts
        sync.read_phone_accounts = lambda launch_id, name, serial_no, **k: ProfileAccounts(
            serial_no, launch_id, name, error="phone never became ADB-ready")
        try:
            report = sync.run_second_account_sync(
                airtable, TAGGED, api_client=None, adb_enable_client=None,
                launcher_client=None, shutdown_client=None, logger=LOG, dry_run=False)
        finally:
            sync.read_phone_accounts = original

        self.assertEqual(airtable.writes, [])
        self.assertEqual([n for n, _ in report.failed], ["Jasmin 5"])
