"""Who drew the screen is a fact; whether we recognise it is not.

`open_instagram` decided Instagram was absent whenever the signup classifier
could not name the screen -- which is every screen nobody has named yet. It
relaunched five times over Instagram's own "set up on new device" onboarding,
fully drawn and in front, and the run it was supporting never started. Meta's
ads-consent screen did the same thing an hour earlier.

The UI dump carries the owning package on every node, so it can simply be
asked.
"""

import unittest
import xml.etree.ElementTree as ET

from adb_bot.automation.flows.signup_driver import AdbSignupDriver

INSTAGRAM = "com.instagram.android"
PLAY_STORE = "com.android.vending"


def driver_showing(package_on_nodes):
    """A driver whose cached dump was drawn by `package_on_nodes`."""
    root = ET.fromstring(
        '<hierarchy><node package="%s" text="whatever this screen is">'
        '<node package="%s" text="a child"/></node></hierarchy>'
        % (package_on_nodes, package_on_nodes))
    driver = AdbSignupDriver.__new__(AdbSignupDriver)
    driver._root = root
    return driver


class ShowingPackageTest(unittest.TestCase):

    def test_an_unnamed_instagram_screen_still_counts_as_instagram(self):
        """The whole point: no classifier is consulted."""
        self.assertTrue(driver_showing(INSTAGRAM).showing_package(INSTAGRAM))

    def test_another_app_does_not(self):
        self.assertFalse(driver_showing(PLAY_STORE).showing_package(INSTAGRAM))

    def test_it_is_case_insensitive(self):
        self.assertTrue(
            driver_showing("COM.INSTAGRAM.ANDROID").showing_package(INSTAGRAM))

    def test_a_screen_that_will_not_dump_is_not_a_yes(self):
        """An unreadable screen must never be reported as the app being up."""
        driver = AdbSignupDriver.__new__(AdbSignupDriver)
        driver._root = None
        driver._dump = lambda: (None, "")
        self.assertFalse(driver.showing_package(INSTAGRAM))


if __name__ == "__main__":
    unittest.main()
