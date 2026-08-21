"""Instagram leaving the foreground mid-signup is not an unnamed screen.

A run tapped the mobile-number field, found the field list empty ten seconds
later, and read the Play Store on the next dump. Instagram had gone; the store
was merely what lay behind it. Read as `unknown_screen` that ends the run and
spends the phone -- read as "the app is not in front" it is the same cheap fix
as the home screen, which is to start Instagram again.

The danger in the other direction is worse, so the markers are deliberately
narrow: a loose one would restart Instagram in the middle of a signup that was
going fine, and a restarted signup begins again at "Join Instagram" and throws
away a verified number.
"""

import unittest

from adb_bot.automation.flows import signup

# Verbatim, the screen four runs died on.
PLAY_STORE = ("options sign in to find the latest android apps, games, "
              "movies, music, & more sign in")

# Real Instagram screens that must never be mistaken for another app.
SIGNUP_PHONE = (
    "what's your mobile number? enter the mobile number where you can be "
    "contacted. no one will see this on your profile. next sign up with email"
)
CODE_SCREEN = (
    "enter the confirmation code to confirm your profile, enter the 6-digit "
    "code we sent via sms to +19832051751. next i didn't get the code"
)
USERNAME_SCREEN = (
    "create a username add a username or use our suggestion. you can change "
    "this at any time. next"
)


class AppVanishedTest(unittest.TestCase):

    def test_the_play_store_is_recognised_as_another_app(self):
        self.assertTrue(signup.looks_like_another_app(PLAY_STORE))

    def test_it_classifies_as_not_instagram_rather_than_unknown(self):
        self.assertEqual(signup.classify_signup_screen(PLAY_STORE),
                         signup.SCREEN_NOT_INSTAGRAM)
        self.assertNotEqual(signup.classify_signup_screen(PLAY_STORE),
                            signup.SCREEN_UNKNOWN)

    def test_real_signup_screens_are_never_mistaken_for_another_app(self):
        """A false positive restarts a healthy signup and loses everything."""
        for screen in (SIGNUP_PHONE, CODE_SCREEN, USERNAME_SCREEN):
            with self.subTest(screen=screen[:32]):
                self.assertFalse(signup.looks_like_another_app(screen))

    def test_those_screens_keep_their_own_classifications(self):
        self.assertEqual(signup.classify_signup_screen(SIGNUP_PHONE),
                         signup.SCREEN_PHONE)
        self.assertEqual(signup.classify_signup_screen(CODE_SCREEN),
                         signup.SCREEN_CODE)
        self.assertEqual(signup.classify_signup_screen(USERNAME_SCREEN),
                         signup.SCREEN_USERNAME)

    def test_empty_text_is_not_another_app(self):
        """A dead phone reads empty; that has its own handling."""
        self.assertFalse(signup.looks_like_another_app(""))
        self.assertFalse(signup.looks_like_another_app(None))


if __name__ == "__main__":
    unittest.main()
