"""Telling Instagram's two mobile-number screens apart.

They are worded almost identically and mean opposite things: one is a step in
creating the account, the other is a prompt after it already exists. Confusing
them is not a cosmetic error -- the flow skips the second, and there is nothing
to skip on the first, so it loops until it gives up with no account made.
"""

import unittest

from adb_bot.automation.flows import signup

# Taken from a real dump on a Geelark phone, 2026-08-21 -- the screen two runs
# looped thirty times on. Note "where you can be contacted", which used to be
# a marker for the post-creation prompt alone.
SIGNUP_PHONE_SCREEN = (
    "what's your mobile number? what's your mobile number? enter the mobile "
    "number where you can be contacted. no one will see this on your profile. "
    "mobile number you may receive whatsapp and sms notifications from us. "
    "learn more next sign up with email i already have an account back"
)

POST_CREATION_PROMPT = (
    "add a mobile number add a mobile number enter the mobile number where "
    "you can be contacted. no one will see this on your profile. skip"
)


class MobileNumberScreensTest(unittest.TestCase):

    def test_the_signup_screen_is_a_step_not_a_prompt(self):
        self.assertEqual(signup.classify_signup_screen(SIGNUP_PHONE_SCREEN),
                         signup.SCREEN_PHONE)

    def test_the_post_creation_prompt_is_still_recognised(self):
        self.assertEqual(signup.classify_signup_screen(POST_CREATION_PROMPT),
                         signup.SCREEN_ADD_PHONE)

    def test_your_profile_is_not_evidence_of_a_profile(self):
        """Both screens say it, so it can never decide between them."""
        self.assertIn("no one will see this on your profile",
                      SIGNUP_PHONE_SCREEN)
        self.assertIn("no one will see this on your profile",
                      POST_CREATION_PROMPT)
        self.assertNotEqual(signup.classify_signup_screen(SIGNUP_PHONE_SCREEN),
                            signup.classify_signup_screen(POST_CREATION_PROMPT))


if __name__ == "__main__":
    unittest.main()
