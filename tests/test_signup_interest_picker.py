"""Instagram's post-signup interest picker, the last screen before a real feed.

Seen first on 2026-08-22, on `nora.sommer62`. The run had already tapped "I
agree" -- the account was created -- and correctly walked every prompt after it
(permissions, photo, follow suggestions, add-email), then fell through to
`unknown_screen` on this one, because nothing named it. The account and its
credentials were real and already saved (`~/.adb_bot/accounts/accounts.json`
does not gate on the final classification); only the run's own verdict was
wrong, which would have left a working account looking like a failure to
every caller that reads the ledger.
"""

import unittest

from adb_bot.automation.flows import signup

# Verbatim from the dump on `Viktoria new 10` / `nora.sommer62`, 2026-08-22.
INTEREST_PICKER_SCREEN = (
    "pick what you want to see more of bkblend_official sportscenter "
    "verified done back skip"
)

INTEREST_PICKER_LABELS = ["Back", "Skip"]


class InterestPickerScreenTest(unittest.TestCase):

    def test_the_screen_is_recognised_as_a_working_account(self):
        self.assertEqual(signup.classify_signup_screen(INTEREST_PICKER_SCREEN),
                         signup.SCREEN_DONE)

    def test_it_is_not_mistaken_for_unknown(self):
        self.assertNotEqual(signup.classify_signup_screen(INTEREST_PICKER_SCREEN),
                            signup.SCREEN_UNKNOWN)

    def test_it_is_not_read_as_a_checkpoint(self):
        """SCREEN_DONE is checked last precisely so a challenge can never be
        misread as success; this pins the other direction too."""
        self.assertNotEqual(signup.classify_signup_screen(INTEREST_PICKER_SCREEN),
                            signup.SCREEN_CHECKPOINT)


if __name__ == "__main__":
    unittest.main()
