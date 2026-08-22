"""Instagram's two email screens, told apart by their title.

The signup email field now titles itself "what's your email?" and subtitles it
"enter the email where you can be contacted" -- the exact subtitle the
post-creation "add an email address" prompt carries. Sharing the subtitle made
the signup screen classify as the prompt, and the flow toggled between it and
the phone screen forever, never typing the address. Same shape as the two
mobile-number screens; the title is what separates them.
"""

import unittest

from adb_bot.automation.flows import signup

# Verbatim from `Emely new 1`, 2026-08-22 -- the run that toggled.
SIGNUP_EMAIL = (
    "what's your email? what's your email? enter the email where you can be "
    "contacted. no one will see this on your profile. email next sign up with "
    "mobile number i already have an account back"
)

POST_CREATION_ADD_EMAIL = (
    "add an email address add an email address enter the email where you can "
    "be contacted. no one will see this on your profile. skip"
)


class EmailScreensTest(unittest.TestCase):

    def test_the_signup_email_field_is_the_signup_email_screen(self):
        self.assertEqual(signup.classify_signup_screen(SIGNUP_EMAIL),
                         signup.SCREEN_EMAIL)

    def test_the_post_creation_prompt_is_still_itself(self):
        self.assertEqual(signup.classify_signup_screen(POST_CREATION_ADD_EMAIL),
                         signup.SCREEN_ADD_EMAIL)

    def test_they_are_not_the_same_screen(self):
        self.assertNotEqual(signup.classify_signup_screen(SIGNUP_EMAIL),
                            signup.classify_signup_screen(POST_CREATION_ADD_EMAIL))

    def test_the_shared_subtitle_decides_nothing(self):
        shared = "enter the email where you can be contacted"
        self.assertIn(shared, SIGNUP_EMAIL)
        self.assertIn(shared, POST_CREATION_ADD_EMAIL)


if __name__ == "__main__":
    unittest.main()
