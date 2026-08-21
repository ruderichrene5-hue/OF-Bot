"""Instagram refusing a number outright is not the same as no code arriving.

One run leased `+12274442163`, Instagram answered "mobile number is invalid",
and the flow retyped the same number four times and gave up with the phone
spent. It was not a format problem: `+19382778361` had gone through in exactly
the same shape minutes earlier. Instagram simply declines some numbers.

The flow already knows how to swap a number when no code arrives. It has to do
the same here, and must not count the swap against the provider -- the number
was delivered to us fine; Instagram declined to use it.
"""

import unittest

from adb_bot.automation.flows import signup

# Verbatim from `Luisa new 2`.
REJECTED = (
    "what's your mobile number? mobile number +12274442163 mobile number "
    "input mobile number is invalid. looks like your mobile number may be "
    "incorrect. try entering your full number, next"
)

ACCEPTED = (
    "what's your mobile number? enter the mobile number where you can be "
    "contacted. no one will see this on your profile. mobile number next "
    "sign up with email"
)


class InvalidNumberTest(unittest.TestCase):

    def test_the_rejection_is_recognised(self):
        self.assertTrue(any(marker in REJECTED
                            for marker in signup._PHONE_INVALID_MARKERS))

    def test_an_ordinary_number_screen_is_not_a_rejection(self):
        """Otherwise every run would throw away its first number."""
        self.assertFalse(any(marker in ACCEPTED
                             for marker in signup._PHONE_INVALID_MARKERS))

    def test_both_are_still_the_number_screen(self):
        for screen in (REJECTED, ACCEPTED):
            with self.subTest(screen=screen[:32]):
                self.assertEqual(signup.classify_signup_screen(screen),
                                 signup.SCREEN_PHONE)

    def test_the_code_screen_is_not_a_rejection(self):
        code = ("enter the confirmation code to confirm your profile, enter "
                "the 6-digit code we sent via sms to +19382778361.")
        self.assertFalse(any(marker in code
                             for marker in signup._PHONE_INVALID_MARKERS))


if __name__ == "__main__":
    unittest.main()
