"""Who the flag review looks at, and what it is allowed to conclude.

The selection is the whole safety story: this tool removes flags, so a profile
it should not have touched is a profile put back into the posting loop broken.
"""

import unittest

from adb_bot.automation import flag_review
from adb_bot.automation.flows import verification


def _profile(name, tags=(), pid="1"):
    return {"serial_name": name, "id": pid, "tags": list(tags)}


class SelectionTest(unittest.TestCase):
    def test_model_profiles_carrying_the_flag_are_checked(self):
        to_check, _ = flag_review.select([
            _profile("Jil 8", ["Issue"]),
            _profile("Nikki 12", ["Issue"]),
        ])
        self.assertEqual([p["serial_name"] for p in to_check], ["Jil 8", "Nikki 12"])

    def test_staging_phones_are_not_model_profiles(self):
        """They were never posting, so their flag is not a posting flag."""
        to_check, _ = flag_review.select([
            _profile("Blank (12)", ["Issue"]),
            _profile("Default profile name (44)", ["Issue"]),
            _profile("Jil 8", ["Issue"]),
        ])
        self.assertEqual([p["serial_name"] for p in to_check], ["Jil 8"])

    def test_link_in_bio_profiles_are_left_alone(self):
        """`ONBOARDING_A_MODEL.md`: a name carrying `Link` is not a posting
        target, so nothing about posting explains its flag."""
        to_check, _ = flag_review.select([
            _profile("Jasmin Link", ["Issue"]),
            _profile("luisa link", ["Issue"]),
            _profile("Luisa 9", ["Issue"]),
        ])
        self.assertEqual([p["serial_name"] for p in to_check], ["Luisa 9"])

    def test_unflagged_profiles_are_not_touched(self):
        to_check, diagnosed = flag_review.select([_profile("Jil 8", [])])
        self.assertEqual(to_check, [])
        self.assertEqual(diagnosed, [])

    def test_a_profile_somebody_already_diagnosed_is_skipped_for_free(self):
        """Launching a phone to rediscover what a VA already wrote costs two
        minutes and tells us nothing."""
        to_check, diagnosed = flag_review.select([
            _profile("Jil 17", ["Issue", "logged out"]),
            _profile("Jil 16", ["Issue", "Banned / Dead"]),
            _profile("Katja 2", ["Issue", "unable to verify"]),
            _profile("Luisa 9", ["Issue"]),
        ])
        self.assertEqual([p["serial_name"] for p in to_check], ["Luisa 9"])
        self.assertEqual(len(diagnosed), 3)

    def test_diagnosis_tags_are_matched_whatever_the_casing(self):
        """They are typed by hand."""
        _, diagnosed = flag_review.select([
            _profile("Jil 17", ["Issue", "Logged Out"]),
            _profile("Jil 16", ["Issue", "BANNED / DEAD"]),
        ])
        self.assertEqual(len(diagnosed), 2)


def _row(launch_id, reason, needs_human=True):
    return {"launch_id": launch_id, "reason": reason, "needs_human": needs_human}


class ByReasonTest(unittest.TestCase):
    """The reason lives in Airtable and the tag lives in MultiLogin, so
    narrowing to "the ones that stopped posting" is a join, not a tag read."""

    def test_only_the_wanted_reasons_survive(self):
        items = [_profile("Jil 8", ["Issue"], pid="1"),
                 _profile("Kathi 7", ["Issue"], pid="2"),
                 _profile("Luisa 9", ["Issue"], pid="3")]
        rows = [_row("1", "No Recent Success"),
                _row("2", "Human Verification Required"),
                _row("3", "Banned / Blocked")]
        kept = flag_review.by_reason(items, rows, flag_review.POSTING_REASONS)
        self.assertEqual([i["serial_name"] for i in kept], ["Jil 8"])

    def test_both_posting_reasons_are_included(self):
        items = [_profile("Jil 8", ["Issue"], pid="1"),
                 _profile("Jasmin 5", ["Issue"], pid="2")]
        rows = [_row("1", "No Recent Success"), _row("2", "Retries Exhausted")]
        kept = flag_review.by_reason(items, rows, flag_review.POSTING_REASONS)
        self.assertEqual(len(kept), 2)

    def test_the_reason_is_matched_whatever_the_casing(self):
        items = [_profile("Jil 8", ["Issue"], pid="1")]
        rows = [_row("1", "NO RECENT SUCCESS")]
        self.assertEqual(len(flag_review.by_reason(items, rows, ["No Recent Success"])), 1)

    def test_a_profile_with_no_airtable_row_is_dropped(self):
        """Asking for a named subset and getting an unmatched profile back is
        the one answer that cannot be right."""
        items = [_profile("Ghost 1", ["Issue"], pid="99")]
        self.assertEqual(flag_review.by_reason(items, [], flag_review.POSTING_REASONS), [])

    def test_the_flagged_row_wins_when_two_rows_share_a_profile(self):
        """A re-created row leaves two rows on one MLX id; the one carrying the
        diagnosis is the one that describes the profile."""
        items = [_profile("Jil 8", ["Issue"], pid="1")]
        rows = [_row("1", "", needs_human=False), _row("1", "Retries Exhausted")]
        kept = flag_review.by_reason(items, rows, flag_review.POSTING_REASONS)
        self.assertEqual([i["serial_name"] for i in kept], ["Jil 8"])


class ProtectedReasonTest(unittest.TestCase):
    """A screen read may not overrule a verdict a posting attempt already got.

    `Laila 5` was reported `banned` on 2026-08-12 -- the suspension notice says
    "177 days left to appeal" -- and on 2026-08-13 its phone showed an ordinary
    feed with its own story tray, because a suspended account keeps rendering a
    cached feed. The sweep cleared it, the recovery loop un-flagged it, and
    `stale_profiles` re-flagged it as `No Recent Success`: the ban diagnosis was
    erased by the tool meant to protect it.
    """

    def test_a_banned_profile_is_protected(self):
        rows = [_row("1", "Banned / Blocked"), _row("2", "No Recent Success")]
        self.assertEqual(flag_review.protected_ids(rows), {"1"})

    def test_a_verification_diagnosis_is_protected(self):
        rows = [_row("1", "Human Verification Required")]
        self.assertEqual(flag_review.protected_ids(rows), {"1"})

    def test_the_posting_reasons_are_not_protected(self):
        """They are the ones this tool exists to re-examine."""
        rows = [_row("1", "No Recent Success"), _row("2", "Retries Exhausted")]
        self.assertEqual(flag_review.protected_ids(rows), set())

    def test_an_unflagged_row_protects_nothing(self):
        """A stale reason on a profile nobody flagged is history, not a verdict."""
        rows = [_row("1", "Banned / Blocked", needs_human=False)]
        self.assertEqual(flag_review.protected_ids(rows), set())

    def test_the_reason_is_matched_whatever_the_casing(self):
        self.assertEqual(flag_review.protected_ids([_row("1", "BANNED / BLOCKED")]),
                         {"1"})


class VerdictTest(unittest.TestCase):
    """The screens that may and may not clear a flag."""

    def test_a_challenge_is_never_cleared(self):
        for challenge in (verification.CHALLENGE_PHONE, verification.CHALLENGE_CODE,
                          verification.CHALLENGE_PHOTO,
                          verification.CHALLENGE_IMAGE_CAPTCHA,
                          verification.CHALLENGE_BANNED,
                          verification.CHALLENGE_SIGNED_OUT,
                          verification.CHALLENGE_CONSENT):
            self.assertIn(challenge, flag_review.REAL_ISSUES)
            self.assertNotIn(challenge, flag_review.CLEARS_THE_FLAG)

    def test_only_a_screen_with_no_challenge_can_clear_it(self):
        self.assertEqual(flag_review.CLEARS_THE_FLAG,
                         (verification.CHALLENGE_NONE,))


if __name__ == "__main__":
    unittest.main()
