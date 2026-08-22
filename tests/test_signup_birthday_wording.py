"""Instagram calls the same screen two different things.

The German build asks for a "date of birth"; the US build asks for a
"birthday". Only the first was recognised, so the first run that ever got an
SMS code delivered -- past the wall that had stopped everything else all day --
stopped one screen later on a form it had always known how to fill.

The date picker behind it is unchanged; this is purely what the screen is
called.
"""

import unittest

from adb_bot.automation.flows import signup

# Verbatim from `Katherine new 4`, the first delivered-code run, 2026-08-21.
US_BIRTHDAY_SCREEN = (
    "what's your birthday? what's your birthday? use your own birthday, even "
    "if this account is for a business, a pet or something else. no one will "
    "see this unless you choose to share it. why do i need to provide my "
    "birthday? august 21, 1995 next i already have an account back"
)

DE_BIRTHDAY_SCREEN = (
    "what's your date of birth? use your own date of birth, even if this "
    "account is for a business, a pet or something else. why do i need to "
    "provide my date of birth? next"
)


class BirthdayWordingTest(unittest.TestCase):

    def test_the_us_wording_is_recognised(self):
        self.assertEqual(signup.classify_signup_screen(US_BIRTHDAY_SCREEN),
                         signup.SCREEN_BIRTHDAY)

    def test_the_german_wording_still_is(self):
        self.assertEqual(signup.classify_signup_screen(DE_BIRTHDAY_SCREEN),
                         signup.SCREEN_BIRTHDAY)

    def test_neither_is_unknown(self):
        """An unnamed screen ends the run, whatever else is going right."""
        for screen in (US_BIRTHDAY_SCREEN, DE_BIRTHDAY_SCREEN):
            with self.subTest(screen=screen[:40]):
                self.assertNotEqual(signup.classify_signup_screen(screen),
                                    signup.SCREEN_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
