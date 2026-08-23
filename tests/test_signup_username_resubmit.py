"""The username screen submitting via keyboard-covers-button, not a real re-suggestion.

Seen 2026-08-22: two of three accounts in one batch went "stuck" at the
username step, the screen replaying with a *new* Instagram-generated
suggestion each time (`hanna3` -> `hanna6337` -> `hanna63372026`) rather than
ever confirming what was typed. The existing "box holds Instagram's
suggestion again" handling correctly detected the mismatch and retyped every
time -- it was never wrong, it just kept firing forever.

The likely mechanism, found the same day proving the mirror-image bug in
`instagram_login.py`: `dismiss_keyboard()` sends `KEYCODE_BACK`, which on that
device is not reliably consumed by the IME -- it falls through and steps the
signup flow back a screen. Re-entering "create a username" hands back a fresh
suggestion instead of confirming the typed one, which is indistinguishable
from Instagram genuinely re-suggesting.
"""

import unittest

from adb_bot.automation.flows import signup
from tests.test_signup import SCREENS, FakeLease, FakeRouter, _identity


class ScriptedDriver:
    """Like `test_signup.FakeDriver`, but tap results are scriptable and a
    dismiss can be made to corrupt the next screen -- standing in for BACK
    stepping the signup flow itself back a screen on the real device."""

    def __init__(self, screens, tap_results=None, corrupt_on_dismiss=None):
        self._screens = list(screens)
        self.filled = {}
        self.taps = []
        self.dismissals = 0
        self._tap_results = list(tap_results) if tap_results is not None else None
        self._corrupt_on_dismiss = corrupt_on_dismiss

    def read_screen(self):
        return self._screens.pop(0) if self._screens else SCREENS[
            signup.SCREEN_INTERSTITIAL]

    def tap_label(self, labels):
        self.taps.append(tuple(labels))
        if self._tap_results is not None:
            return self._tap_results.pop(0) if self._tap_results else True
        return True

    def fill(self, hints, value, what, submits_itself=False):
        self.filled[what] = value
        return True

    def dismiss_keyboard(self):
        self.dismissals += 1
        if self._corrupt_on_dismiss is not None:
            self._screens.insert(0, self._corrupt_on_dismiss)

    def set_date(self, day, month, year):
        return True


DONE = ("your profile. mia berg 0 posts 0 followers 0 following add your bio "
       "edit profile share profile")


class SubmitAfterTypingTest(unittest.TestCase):
    """`_submit_after_typing` in isolation: try the tap first, dismiss only
    as a fallback -- never dismiss first, which is what let BACK corrupt the
    screen on the device this was found on."""

    def test_taps_immediately_when_the_button_is_reachable(self):
        driver = ScriptedDriver([])
        self.assertTrue(signup._submit_after_typing(driver, ("Next",)))
        self.assertEqual(driver.dismissals, 0)
        self.assertEqual(driver.taps, [("Next",)])

    def test_falls_back_to_dismissing_the_keyboard_if_the_first_tap_misses(self):
        driver = ScriptedDriver([], tap_results=[False, True])
        self.assertTrue(signup._submit_after_typing(driver, ("Next",)))
        self.assertEqual(driver.dismissals, 1)
        self.assertEqual(driver.taps, [("Next",), ("Next",)])


class UsernameResubmitTest(unittest.TestCase):
    def test_a_username_screen_that_would_corrupt_on_dismiss_still_completes(self):
        """The regression, under a deliberately adversarial condition: EVERY
        `dismiss_keyboard()` call anywhere in the run -- not just the
        username step's -- hands back the username screen, standing in for
        BACK stepping the signup flow itself backwards on the real device.

        Before the fix, the username step dismissed unconditionally, so this
        would have looped: retype, dismiss, get handed the username screen
        again, forever, ending in `RESULT_STUCK` exactly like the real batch
        (`hanna6337` -> `hanna63372026` -> ...). After the fix, the username
        step's own tap succeeds directly and never dismisses, so it is never
        the one re-triggering the trap -- other steps still legitimately
        dismiss for their own reasons and each re-visit of the username
        screen just retypes the same, unchanged handle and moves on, which is
        why this still reaches `RESULT_CREATED` rather than looping forever.
        """
        driver = ScriptedDriver(
            [
                SCREENS[signup.SCREEN_ENTRY],
                SCREENS[signup.SCREEN_PHONE],
                SCREENS[signup.SCREEN_CODE],
                SCREENS[signup.SCREEN_PASSWORD],
                SCREENS[signup.SCREEN_BIRTHDAY],
                SCREENS[signup.SCREEN_DATE_PICKER],
                SCREENS[signup.SCREEN_NAME],
                SCREENS[signup.SCREEN_USERNAME],
                SCREENS[signup.SCREEN_TERMS],
                SCREENS[signup.SCREEN_PERMISSIONS],
                SCREENS[signup.SCREEN_PHOTO_PROMPT],
                DONE,
            ],
            corrupt_on_dismiss=SCREENS[signup.SCREEN_USERNAME],
        )
        result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                                   sleep=lambda _s: None)
        self.assertEqual(result.status, signup.RESULT_CREATED)
        self.assertEqual(result.username, "mia.berg")


if __name__ == "__main__":
    unittest.main()
