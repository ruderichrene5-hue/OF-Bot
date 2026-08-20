"""Logging an existing Instagram account into a phone.

The screen strings here were read off a real login on 2026-08-20, and the two
tests that matter most encode mistakes that were actually made during it:

* Instagram folds "wrong password" into the **same screen** as the emailed
  confirmation code. Classify in the wrong order and a bad password reads as a
  routine security check -- or worse, a good account gets written off.
* A feed check of `"for you"` matched the *email code* screen's own prose,
  "it may take a few minutes **for you** to get this code", and reported a
  successful login on a phone sitting on a challenge. This fleet has been
  bitten by exactly that shape of feed check before.
"""

import unittest

from adb_bot.automation.flows import instagram_login as login


class ClassifyTest(unittest.TestCase):
    def test_the_join_entry_screen(self):
        """Observed. Two entry screens exist and this one needs a tap first."""
        text = ("english (us) | join instagram | share what you're into with "
                "the people who get you. | get started | i already have a "
                "profile | meta logo")
        self.assertEqual(login.classify_login_screen(text), login.SCREEN_JOIN)

    def test_the_login_form(self):
        """Observed, after tapping 'I already have a profile'."""
        text = ("instagram from meta | username, email or mobile number | "
                "password | log in | forgot password? | create new account | "
                "terms and imprint")
        self.assertEqual(login.classify_login_screen(text), login.SCREEN_FORM)

    def test_a_wrong_password_is_not_read_as_a_security_check(self):
        """Observed verbatim. Instagram puts both on one screen, and the
        wrong-password test has to win or a bad password looks routine."""
        text = ("check your email | the password you entered is incorrect. to "
                "log in, enter the code we sent to c*******a@gmail.com | "
                "enter code | get a new code | continue")
        self.assertEqual(login.classify_login_screen(text),
                         login.SCREEN_WRONG_PASSWORD)

    def test_a_clean_email_challenge(self):
        """Observed: the same screen *without* the incorrect-password line.

        This is what a correct password on an unrecognised device looks like --
        the account is fine, it just needs the code.
        """
        text = ("check your email | enter the code we sent to "
                "c*******a@gmail.com | enter code | get a new code | continue "
                "| back | help")
        self.assertEqual(login.classify_login_screen(text),
                         login.SCREEN_EMAIL_CODE)

    def test_the_email_screen_is_never_mistaken_for_the_feed(self):
        """The false positive that reported a logged-in phone that wasn't.

        "it may take a few minutes for you to get this code" contains "for you".
        """
        text = ("check your email | enter the code we sent to a@b.com | it may "
                "take a few minutes for you to get this code. get a new code | "
                "enter code | continue")
        screen = login.classify_login_screen(text)
        self.assertNotEqual(screen, login.SCREEN_FEED)
        self.assertEqual(screen, login.SCREEN_EMAIL_CODE)

    def test_a_blank_read_is_unknown_not_a_screen(self):
        """An empty dump is a dump that failed, not a screen that is wrong."""
        self.assertEqual(login.classify_login_screen(""), login.SCREEN_UNKNOWN)
        self.assertEqual(login.classify_login_screen(None), login.SCREEN_UNKNOWN)

    def test_chrome_only_text_is_loading(self):
        """A dump caught mid-draw carries furniture and nothing else."""
        self.assertEqual(login.classify_login_screen("back | help | continue"),
                         login.SCREEN_LOADING)


class FakeDriver:
    def __init__(self, screens):
        self.screens = list(screens)
        self.filled = []
        self.tapped = []

    def read_screen(self):
        return self.screens.pop(0) if self.screens else ""

    def fill(self, hints, value, what, submits_itself=False):
        self.filled.append((what, value))
        return True

    def tap_label(self, labels, require_clickable=True):
        self.tapped.append(tuple(labels))
        return True

    def dismiss_keyboard(self):
        return True


FORM = ("instagram from meta | username, email or mobile number | password | "
        "log in | forgot password? | create new account")
JOIN = "join instagram | get started | i already have a profile"
EMAIL = "check your email | enter the code we sent to a@b.com | enter code"
FEED = "your story | what's on your mind | suggested for you"


class LogInTest(unittest.TestCase):
    def test_the_join_screen_is_stepped_through_to_the_form(self):
        driver = FakeDriver([JOIN, FORM, FEED])
        result = login.log_in(driver, "someone", "pw", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_LOGGED_IN)
        self.assertIn(("I already have a profile",), driver.tapped)

    def test_credentials_are_typed_then_submitted(self):
        driver = FakeDriver([FORM, FEED])
        login.log_in(driver, "alina", "secret", sleep=lambda *_: None)
        self.assertEqual([v for _w, v in driver.filled], ["alina", "secret"])
        self.assertIn(("Log in", "Log In"), driver.tapped)

    def test_an_email_challenge_is_a_result_not_a_failure(self):
        """The account is fine; something else answers the code. Treating this
        as an error would write off a perfectly good account."""
        driver = FakeDriver([FORM, EMAIL])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_EMAIL_CODE)

    def test_a_wrong_password_stops_immediately(self):
        driver = FakeDriver([FORM, "the password you entered is incorrect"])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_WRONG_PASSWORD)

    def test_it_does_not_retype_credentials_after_submitting(self):
        """Landing back on the form means the submit missed. Typing everything
        again blindly is how a flow burns its whole step budget."""
        driver = FakeDriver([FORM, FORM])
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_STUCK)
        self.assertEqual(len(driver.filled), 2)   # one username + one password

    def test_an_unnamed_screen_ends_the_run(self):
        driver = FakeDriver([FORM, "some screen nobody has named yet at all"])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_UNKNOWN_SCREEN)

    def test_a_repeating_screen_gives_up(self):
        driver = FakeDriver(["loading"] * 30)
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_STUCK)


if __name__ == "__main__":
    unittest.main()
