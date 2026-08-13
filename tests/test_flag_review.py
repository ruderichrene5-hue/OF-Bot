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
