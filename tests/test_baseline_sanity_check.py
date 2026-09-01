"""_sanity_check_baseline_count -- added 2026-09-01 after a real false
CONFIRMED: a fresh app launch's baseline read (0) contradicted an
already-confirmed post the ledger had for the same account, letting a later
unrelated count change get misattributed as a different post's own +1.
"""

import unittest
from unittest.mock import MagicMock

from adb_bot.automation import post_ledger
from adb_bot.automation.flows import reel_verify
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow


class BaselineSanityCheckTest(unittest.TestCase):
    def _ledger(self, records):
        ledger = MagicMock()
        ledger.load.return_value = {f"k{i}": r for i, r in enumerate(records)}
        return ledger

    def _confirmed(self, profile_id):
        return post_ledger.ShareRecord(
            profile_id=profile_id, media_hash="a" * 64,
            status=post_ledger.STATUS_CONFIRMED)

    def test_none_baseline_passes_through_unchanged(self):
        ledger = self._ledger([])
        emit = MagicMock()
        result = InstagramReelUploadU2Flow._sanity_check_baseline_count(
            None, ledger, "ph1", "1.2.3.4:5555", emit)
        self.assertIsNone(result)
        emit.assert_not_called()

    def test_inexact_baseline_is_not_second_guessed(self):
        """A rounded count ("1.2K") is already untrustworthy for a different
        reason -- this check only has an opinion about exact reads."""
        ledger = self._ledger([self._confirmed("ph1")] * 5)
        emit = MagicMock()
        baseline = reel_verify.Count(value=0, exact=False)
        result = InstagramReelUploadU2Flow._sanity_check_baseline_count(
            baseline, ledger, "ph1", "1.2.3.4:5555", emit)
        self.assertIs(result, baseline)
        emit.assert_not_called()

    def test_a_baseline_at_or_above_the_confirmed_floor_is_trusted(self):
        ledger = self._ledger([self._confirmed("ph1")])
        emit = MagicMock()
        baseline = reel_verify.Count(value=1, exact=True)
        result = InstagramReelUploadU2Flow._sanity_check_baseline_count(
            baseline, ledger, "ph1", "1.2.3.4:5555", emit)
        self.assertIs(result, baseline)
        emit.assert_not_called()

    def test_a_stale_baseline_below_the_confirmed_floor_is_downgraded(self):
        """The exact scenario found live: one already-confirmed post for
        this account, but a fresh baseline read comes back 0."""
        ledger = self._ledger([self._confirmed("ph1")])
        emit = MagicMock()
        baseline = reel_verify.Count(value=0, exact=True)
        result = InstagramReelUploadU2Flow._sanity_check_baseline_count(
            baseline, ledger, "ph1", "1.2.3.4:5555", emit)
        self.assertEqual(result.value, 0)
        self.assertFalse(result.exact)
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "warning")

    def test_only_this_profiles_confirmed_posts_count_toward_the_floor(self):
        """A different account's confirmed posts must not inflate this
        account's floor."""
        ledger = self._ledger([self._confirmed("some-other-profile")])
        emit = MagicMock()
        baseline = reel_verify.Count(value=0, exact=True)
        result = InstagramReelUploadU2Flow._sanity_check_baseline_count(
            baseline, ledger, "ph1", "1.2.3.4:5555", emit)
        self.assertIs(result, baseline)
        emit.assert_not_called()

    def test_shared_but_not_yet_confirmed_records_dont_count_toward_the_floor(self):
        """An unresolved "shared" record is not proof anything landed --
        only STATUS_CONFIRMED should raise the floor."""
        unresolved = post_ledger.ShareRecord(
            profile_id="ph1", media_hash="b" * 64, status=post_ledger.STATUS_SHARED)
        ledger = self._ledger([unresolved])
        emit = MagicMock()
        baseline = reel_verify.Count(value=0, exact=True)
        result = InstagramReelUploadU2Flow._sanity_check_baseline_count(
            baseline, ledger, "ph1", "1.2.3.4:5555", emit)
        self.assertIs(result, baseline)
        emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
