"""When Instagram says the handle is taken, use a different one.

The first run ever to get an SMS code delivered walked the whole signup and
then died on "username did not advance in 4 tries" -- while the screen said,
in plain words, `the username sarasommer56 is not available`. The flow retyped
the same rejected handle every pass, which is exactly what the repeat guard is
built to notice, so a solvable problem read as a stuck one.
"""

import unittest

from adb_bot.automation.flows import signup
from adb_bot.automation.flows.signup import next_username

# Verbatim from `Katherine new 5`, 2026-08-21.
TAKEN_SCREEN = (
    "create a username create a username add a username or use our "
    "suggestion. you can change this at any time. username username "
    "sarasommer56 username,sara970149 input username is invalid. the username "
    "sarasommer56 is not available. the username sarasommer56 is not "
    "available. next next next back"
)

FRESH_SCREEN = (
    "create a username create a username add a username or use our "
    "suggestion. you can change this at any time. username next back"
)


class UsernameTakenTest(unittest.TestCase):

    def test_the_rejection_is_visible_on_the_screen(self):
        self.assertTrue(any(marker in TAKEN_SCREEN
                            for marker in signup._USERNAME_TAKEN_MARKERS))

    def test_a_fresh_screen_is_not_read_as_a_rejection(self):
        """Otherwise every run would change its handle for no reason."""
        self.assertFalse(any(marker in FRESH_SCREEN
                             for marker in signup._USERNAME_TAKEN_MARKERS))

    def test_both_screens_are_still_the_username_screen(self):
        for screen in (TAKEN_SCREEN, FRESH_SCREEN):
            with self.subTest(screen=screen[:30]):
                self.assertEqual(signup.classify_signup_screen(screen),
                                 signup.SCREEN_USERNAME)


# Verbatim from `Jasmin new 4`. The handle was typed as `emma8613` and the
# field renders it as `emma8_613` -- so a fill that insists on an exact echo
# can never succeed, however many times it tries.
VALID_SCREEN = (
    "create a username create a username add a username or use our "
    "suggestion. you can change this at any time. username username "
    "emma8_613 username,emma620477 input username is valid. next next next "
    "back"
)


class UsernameAcceptedTest(unittest.TestCase):
    """A handle Instagram calls valid needs a button press, not more typing.

    The flow retyped `emma8613` until its four-try budget ran out while the
    screen said `input username is valid` and `Next` sat there enabled. Two
    identical steps, byte for byte, and a run lost several screens past a
    delivered SMS code.
    """

    def test_the_acceptance_is_visible_on_the_screen(self):
        self.assertTrue(any(marker in VALID_SCREEN
                            for marker in signup._USERNAME_VALID_MARKERS))

    def test_a_rejection_is_never_read_as_an_acceptance(self):
        """"invalid" contains "valid"; the markers must not collide."""
        self.assertFalse(any(marker in TAKEN_SCREEN
                             for marker in signup._USERNAME_VALID_MARKERS))

    def test_an_acceptance_is_never_read_as_a_rejection(self):
        self.assertFalse(any(marker in VALID_SCREEN
                             for marker in signup._USERNAME_TAKEN_MARKERS))

    def test_a_fresh_screen_is_neither(self):
        """Nothing has been typed yet, so there is nothing to accept."""
        self.assertFalse(any(marker in FRESH_SCREEN
                             for marker in signup._USERNAME_VALID_MARKERS))
        self.assertFalse(any(marker in FRESH_SCREEN
                             for marker in signup._USERNAME_TAKEN_MARKERS))

    def test_it_is_still_the_username_screen(self):
        self.assertEqual(signup.classify_signup_screen(VALID_SCREEN),
                         signup.SCREEN_USERNAME)


class NextUsernameTest(unittest.TestCase):

    def test_the_new_handle_differs_from_the_rejected_one(self):
        self.assertNotEqual(next_username("sarasommer56", 1), "sarasommer56")

    def test_it_keeps_the_name_and_replaces_the_tail(self):
        """A rejected `sarasommer56` should still look like Sara Sommer."""
        self.assertTrue(next_username("sarasommer56", 1).startswith(
            "sarasommer"))

    def test_it_never_exceeds_instagrams_limit(self):
        long_handle = "a" * 40
        for attempt in range(1, 5):
            with self.subTest(attempt=attempt):
                self.assertLessEqual(len(next_username(long_handle, attempt)),
                                     30)

    def test_a_handle_that_is_all_digits_still_yields_something(self):
        self.assertTrue(next_username("12345", 1))

    def test_it_is_a_legal_handle(self):
        out = next_username("sara.sommer_56", 1)
        self.assertTrue(all(ch.isalnum() or ch in "._" for ch in out))


if __name__ == "__main__":
    unittest.main()
