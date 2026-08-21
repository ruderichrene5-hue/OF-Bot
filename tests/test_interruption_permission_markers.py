"""Android's notification permission dialog must be recognised as one.

Android words this prompt "allow Instagram to SEND YOU notifications", while
every marker here described "access" to something. So the dialog was invisible
to the handler: it reported "answering an android permission dialog" eight
times in a row and answered nothing, because detection failed before any button
was looked for. It stood in front of a password-reset flow the whole time.

The apostrophe matters for the same reason it did on the signup side: Android
renders `DON'T ALLOW` with U+2019, and a marker typed with the ASCII one never
matches it.
"""

import unittest

from adb_bot.automation.flows.interruptions import _PERMISSION_MARKERS

# Verbatim from a Geelark phone, 2026-08-21.
NOTIFICATIONS_DIALOG = "allow instagram to send you notifications? allow don’t allow"
CONTACTS_DIALOG = "allow instagram to access your contacts? allow don’t allow"
PHOTOS_DIALOG = "allow instagram to access photos and videos? allow don’t allow"


def matches(text):
    return [m for m in _PERMISSION_MARKERS if m in text]


class PermissionMarkerTest(unittest.TestCase):

    def test_the_notifications_dialog_is_recognised(self):
        self.assertTrue(matches(NOTIFICATIONS_DIALOG),
                        "no marker matched the notifications prompt")

    def test_the_contacts_dialog_still_is(self):
        self.assertTrue(matches(CONTACTS_DIALOG))

    def test_the_photos_dialog_still_is(self):
        self.assertTrue(matches(PHOTOS_DIALOG))

    def test_the_curly_apostrophe_is_covered(self):
        """`don’t allow` appears on all three, and is what Android renders."""
        self.assertIn("don’t allow", _PERMISSION_MARKERS)

    def test_an_instagram_screen_is_not_a_permission_dialog(self):
        """A false positive taps `Allow` on a screen that is not a dialog."""
        feed = ("your story for you search and explore reels "
                "welcome to instagram")
        self.assertEqual(matches(feed), [])

    def test_the_signup_number_screen_is_not_one_either(self):
        number = ("what's your mobile number? enter the mobile number where "
                  "you can be contacted. next")
        self.assertEqual(matches(number), [])


if __name__ == "__main__":
    unittest.main()
