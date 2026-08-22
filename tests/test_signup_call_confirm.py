"""Instagram's offer to verify by ringing the number instead of texting it.

Seen first on 2026-08-21, on the first UK number ever used here. Twenty-four
German numbers never produced it, so it appears to follow the country -- which
means switching country surfaces a screen the flow had never met, and an
unnamed screen ends the run.

The trap is that the obvious button is the wrong one. `Next` grants Instagram
the `manage phone calls` permission so it can ring the handset and hang up; a
rented SMS number cannot answer a call, so that path spends the number for
nothing. `Confirm with a code` is the SMS route the flow already drives.
"""

import unittest

from adb_bot.automation.flows import signup

# Verbatim from the dump on `Katherine new 1`, truncated where the log was.
CALL_CONFIRM_SCREEN = (
    "permissions are set image confirm your account automatically with a "
    "phone call confirm your account automatically with a phone call you'll "
    "need to allow instagram the following permissions: you'll need to allow "
    "instagram the following permissions: manage phone calls, we'll call your "
    "mobile number and end the call automatically. manage phone calls manage "
    "phone calls we'll call your mobile number an"
)

CALL_CONFIRM_LABELS = ["Next", "Confirm with a code", "Back"]


# Verbatim from `Katherine new 2`, the screen that followed the decline.
SEND_SMS_SCREEN = (
    "send sms to confirm your account send sms to confirm your account send a "
    "prefilled code from +447840815977. standard rates apply. send a "
    "prefilled code from +447840815977. standard rates apply. tap to open "
    "your default sms app. tap to open your default sms app. send the "
    "prefilled code without making any changes."
)

SEND_SMS_LABELS = ["Open SMS app", "Try another way", "Back"]


class SendSmsScreenTest(unittest.TestCase):
    """Instagram asking the phone to send a message OUT, not receive one.

    It would go from the cloud phone's own SIM, which is not the number being
    confirmed -- and these phones have no usable SIM anyway. `Try another way`
    is the only move; `Open SMS app` is a dead end that looks like progress.
    """

    def test_the_screen_is_recognised(self):
        self.assertEqual(signup.classify_signup_screen(SEND_SMS_SCREEN),
                         signup.SCREEN_SEND_SMS)

    def test_it_is_not_mistaken_for_the_incoming_code_screen(self):
        self.assertNotEqual(signup.classify_signup_screen(SEND_SMS_SCREEN),
                            signup.SCREEN_CODE)

    def test_the_escape_hatch_is_among_the_labels_we_look_for(self):
        wanted = {label.lower() for label in signup._ANOTHER_WAY_LABELS}
        present = {label.lower() for label in SEND_SMS_LABELS}
        self.assertTrue(wanted & present)

    def test_open_sms_app_is_not_one_of_them(self):
        wanted = {label.lower() for label in signup._ANOTHER_WAY_LABELS}
        self.assertNotIn("open sms app", wanted)


class CallConfirmScreenTest(unittest.TestCase):

    def test_the_screen_is_recognised(self):
        self.assertEqual(signup.classify_signup_screen(CALL_CONFIRM_SCREEN),
                         signup.SCREEN_CALL_CONFIRM)

    def test_it_is_not_mistaken_for_the_number_form(self):
        """It repeats the number's context; retyping there would be wrong."""
        self.assertNotEqual(signup.classify_signup_screen(CALL_CONFIRM_SCREEN),
                            signup.SCREEN_PHONE)

    def test_the_escape_hatch_is_among_the_labels_we_look_for(self):
        """The button the handler taps must actually be on the real screen."""
        wanted = {label.lower() for label in signup._CONFIRM_WITH_CODE_LABELS}
        present = {label.lower() for label in CALL_CONFIRM_LABELS}
        self.assertTrue(wanted & present,
                        "none of the labels the handler taps is on the screen")

    def test_next_is_not_one_of_them(self):
        """`Next` takes the phone-call path and burns the number."""
        wanted = {label.lower() for label in signup._CONFIRM_WITH_CODE_LABELS}
        self.assertNotIn("next", wanted)


if __name__ == "__main__":
    unittest.main()
