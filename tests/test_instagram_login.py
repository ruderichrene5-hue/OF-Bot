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

    def test_a_dead_account_is_told_apart_from_a_failed_login(self):
        """Observed verbatim. The credentials are fine as data; the account
        behind them is gone. Filing that as a login failure would have somebody
        re-testing a dead account for ever.
        """
        text = ("recover your account | it looks like that login info is no "
                "longer connected to an account. we'll use a secure process to "
                "help you get back in.")
        self.assertEqual(login.classify_login_screen(text), login.SCREEN_GONE)

    def test_a_dead_account_outranks_the_login_form(self):
        """The recovery screen still carries form-ish words; order decides."""
        text = ("recover your account no longer connected to an account "
                "username, email or mobile number forgot password?")
        self.assertEqual(login.classify_login_screen(text), login.SCREEN_GONE)

    def test_a_near_miss_handle_is_its_own_outcome(self):
        """Observed: stored handle `babybri732`, real account `babybri73`.

        Instagram offers to log into the near match. Accepting that would put
        the phone on a DIFFERENT account from the one recorded against it, and
        nothing downstream would notice -- so this is reported, never taken.
        """
        text = ("is this your account? | we couldn\u2019t find an account that "
                "matches what you entered, but found one that closely matches. "
                "| babybri73 | continue | log into another account")
        self.assertEqual(login.classify_login_screen(text),
                         login.SCREEN_NO_SUCH_HANDLE)

    def test_the_hard_no_such_account_wording_too(self):
        """Observed: Instagram has two wordings for a missing handle and only
        one offers a near match. Both mean the stored handle is wrong."""
        text = ("can't find account | we can't find an account with catcutie02. "
                "try another mobile number or email, or if you don't have an "
                "account, you can sign up. | try again | sign up")
        self.assertEqual(login.classify_login_screen(text),
                         login.SCREEN_NO_SUCH_HANDLE)

    def test_a_blank_read_is_unknown_not_a_screen(self):
        """An empty dump is a dump that failed, not a screen that is wrong."""
        self.assertEqual(login.classify_login_screen(""), login.SCREEN_UNKNOWN)
        self.assertEqual(login.classify_login_screen(None), login.SCREEN_UNKNOWN)

    def test_chrome_only_text_is_loading(self):
        """A dump caught mid-draw carries furniture and nothing else."""
        self.assertEqual(login.classify_login_screen("back | help | continue"),
                         login.SCREEN_LOADING)


class FakeDriver:
    """Screens are consumed one per read_screen().

    Note the fixtures include an extra copy of the form: the flow re-reads the
    screen after dismissing the keyboard, because `tap_label` taps a cached
    dump and would otherwise use a position from before the keyboard closed.
    """

    def __init__(self, screens, tap_results=None):
        self.screens = list(screens)
        self.filled = []
        self.tapped = []
        self.dismissed = False
        self.reads_before_tap = None
        # None means every tap_label() call succeeds, matching every existing
        # fixture. A list lets a test script specific taps failing, to drive
        # `_submit_after_typing`'s dismiss-first fallback.
        self._tap_results = list(tap_results) if tap_results is not None else None

    reads = 0

    def read_screen(self):
        self.reads += 1
        return self.screens.pop(0) if self.screens else ""

    def fill(self, hints, value, what, submits_itself=False,
             fallback_index=None):
        self.filled.append((what, value))
        self.fallback_indices = getattr(self, "fallback_indices", [])
        self.fallback_indices.append(fallback_index)
        return True

    def tap_label(self, labels, require_clickable=True):
        if self.reads_before_tap is None:
            self.reads_before_tap = self.reads
        self.tapped.append(tuple(labels))
        if self._tap_results is not None:
            return self._tap_results.pop(0) if self._tap_results else True
        return True

    def dismiss_keyboard(self):
        self.dismissed = True
        return True


FORM = ("instagram from meta | username, email or mobile number | password | "
        "log in | forgot password? | create new account")
JOIN = "join instagram | get started | i already have a profile"
EMAIL = "check your email | enter the code we sent to a@b.com | enter code"
FEED = "your story | what's on your mind | suggested for you"
GONE = "recover your account | no longer connected to an account"


class LogInTest(unittest.TestCase):
    def test_the_join_screen_is_stepped_through_to_the_form(self):
        driver = FakeDriver([JOIN, FORM, FORM, FEED])
        result = login.log_in(driver, "someone", "pw", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_LOGGED_IN)
        self.assertIn(("I already have a profile",), driver.tapped)

    def test_credentials_are_typed_then_submitted(self):
        driver = FakeDriver([FORM, FORM, FEED])
        login.log_in(driver, "alina", "secret", sleep=lambda *_: None)
        self.assertEqual([v for _w, v in driver.filled], ["alina", "secret"])
        self.assertIn(("Log in", "Log In"), driver.tapped)

    def test_username_and_password_each_get_a_positional_fallback(self):
        """Confirmed live 2026-08-23 (@daudkim272): a login screen with two
        genuinely hint-less EditTexts made both fields unidentifiable, and
        the run typed nothing into either. Username is always first,
        password always second on this form -- pin that the fallback
        indices actually reach `fill`, not just that credentials are
        passed somewhere."""
        driver = FakeDriver([FORM, FORM, FEED])
        login.log_in(driver, "alina", "secret", sleep=lambda *_: None)
        self.assertEqual(driver.fallback_indices, [0, 1])

    def test_an_email_challenge_is_a_result_not_a_failure(self):
        """The account is fine; something else answers the code. Treating this
        as an error would write off a perfectly good account."""
        driver = FakeDriver([FORM, FORM, EMAIL])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_EMAIL_CODE)

    def test_a_dead_account_ends_the_run_with_its_own_result(self):
        driver = FakeDriver([FORM, FORM, GONE])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_ACCOUNT_GONE)

    def test_the_screen_is_re_read_before_tapping_log_in(self):
        """tap_label taps the driver's CACHED dump. Without a fresh read the
        tap uses a position captured while the keyboard was still open, and on
        a device where the button only becomes reachable after the keyboard
        closes, a stale tap lands on nothing and the account gets reported
        "stuck". Here the tap succeeds with the keyboard still open (see
        `SubmitAfterTypingTest`), so dismiss_keyboard is never needed --
        the property this test actually pins is the fresh read, not the
        dismiss.
        """
        driver = FakeDriver([FORM, FORM, FEED])
        login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertFalse(driver.dismissed)
        # A read happened immediately before the (successful) first tap.
        self.assertEqual(driver.reads_before_tap, 2)

    def test_a_device_where_dismissing_the_keyboard_exits_the_flow_still_logs_in(self):
        """Real bug, found 2026-08-22 testing a live Geelark phone: BACK
        (what dismiss_keyboard sends) was not consumed by the IME on that
        device and fell through to the activity, exiting the whole login flow
        back to the Join Instagram screen with the typed credentials lost.

        Simulated here by making dismiss_keyboard() corrupt the next screen --
        if the fix regressed to a dismiss-first sequence, this would land back
        on the join screen instead of the feed and the login would not
        complete.
        """
        class FlakyDismissDriver(FakeDriver):
            def dismiss_keyboard(self):
                super().dismiss_keyboard()
                self.screens.insert(0, JOIN)

        driver = FlakyDismissDriver([FORM, FORM, FEED])
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_LOGGED_IN)
        self.assertFalse(driver.dismissed)


class SubmitAfterTypingTest(unittest.TestCase):
    """`_submit_after_typing` in isolation: the ordering that matters is tap
    first with the keyboard open, dismiss only as a fallback -- never dismiss
    first, which is what made BACK exit the whole flow on the device this was
    found on."""

    def test_taps_immediately_when_the_button_is_reachable_with_the_keyboard_open(self):
        driver = FakeDriver([])
        result = login._submit_after_typing(driver, ("Log in",),
                                            lambda *_: None, 1)
        self.assertTrue(result)
        self.assertFalse(driver.dismissed)
        self.assertEqual(driver.tapped, [("Log in",)])

    def test_falls_back_to_dismissing_the_keyboard_if_the_first_tap_misses(self):
        """The case the original code was written for: the button is not
        reachable until the keyboard closes (measured ~230px shift)."""
        driver = FakeDriver([], tap_results=[False, True])
        result = login._submit_after_typing(driver, ("Log in",),
                                            lambda *_: None, 1)
        self.assertTrue(result)
        self.assertTrue(driver.dismissed)
        self.assertEqual(driver.tapped, [("Log in",), ("Log in",)])

    def test_reports_failure_only_if_neither_attempt_finds_the_button(self):
        driver = FakeDriver([], tap_results=[False, False])
        result = login._submit_after_typing(driver, ("Log in",),
                                            lambda *_: None, 1)
        self.assertFalse(result)
        self.assertTrue(driver.dismissed)

    def test_a_near_miss_never_taps_continue(self):
        near = ("is this your account? we couldn\u2019t find an account that "
                "matches what you entered | continue | log into another account")
        driver = FakeDriver([FORM, FORM, near])
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_HANDLE_NOT_FOUND)
        self.assertNotIn(("Continue",), driver.tapped)

    def test_a_mailbox_answers_the_code_and_the_login_completes(self):
        class Mailbox:
            address = "a@b.com"
            def __init__(self):
                self.back = False
            def wait_for_code(self):
                return "123456"
            def back_to_instagram(self):
                self.back = True

        box = Mailbox()
        # EMAIL is followed by two FEEDs: the flow re-reads the screen
        # before tapping Continue, same cached-dump guard as Log in.
        driver = FakeDriver([FORM, FORM, EMAIL, FEED, FEED])
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None,
                              mailbox=box)
        self.assertEqual(result, login.RESULT_LOGGED_IN)
        self.assertIn(("instagram email code", "123456"), driver.filled)
        self.assertTrue(box.back, "must return to Instagram after reading")

    def test_a_code_that_never_arrives_is_its_own_result(self):
        class Empty:
            address = "a@b.com"
            def wait_for_code(self):
                return ""
            def back_to_instagram(self):
                pass

        driver = FakeDriver([FORM, FORM, EMAIL])
        self.assertEqual(
            login.log_in(driver, "a", "b", sleep=lambda *_: None,
                         mailbox=Empty()),
            login.RESULT_NO_CODE)

    def test_a_mailbox_that_throws_still_returns_to_instagram(self):
        """Leaving the phone sitting in Gmail strands the login half-done."""
        class Angry:
            address = "a@b.com"
            def __init__(self):
                self.back = False
            def wait_for_code(self):
                raise RuntimeError("gmail exploded")
            def back_to_instagram(self):
                self.back = True

        box = Angry()
        driver = FakeDriver([FORM, FORM, EMAIL])
        login.log_in(driver, "a", "b", sleep=lambda *_: None, mailbox=box)
        self.assertTrue(box.back)

    def test_without_a_mailbox_the_code_screen_is_still_the_end(self):
        driver = FakeDriver([FORM, FORM, EMAIL])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_EMAIL_CODE)

    def test_a_wrong_password_stops_immediately(self):
        driver = FakeDriver([FORM, FORM, "the password you entered is incorrect"])
        self.assertEqual(login.log_in(driver, "a", "b", sleep=lambda *_: None),
                         login.RESULT_WRONG_PASSWORD)

    def test_it_does_not_retype_credentials_after_submitting(self):
        """The fields still hold what was typed; typing again would append."""
        driver = FakeDriver([FORM] * 12)
        login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(len(driver.filled), 2)   # one username + one password

    def test_the_form_staying_up_briefly_is_not_a_failure(self):
        """Instagram leaves the form on screen while it works -- measured still
        showing six seconds after Log in, with the answer arriving about twenty
        seconds in. Giving up on the first re-appearance abandoned four good
        accounts in a row.
        """
        driver = FakeDriver([FORM, FORM, FORM, FORM, FEED])
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_LOGGED_IN)

    def test_a_form_that_never_goes_away_is_eventually_stuck(self):
        """The wait is bounded -- a submit that truly missed must still end."""
        driver = FakeDriver([FORM] * 30)
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_STUCK)

    def test_a_challenge_after_the_wait_is_still_recognised(self):
        """The answer usually arrives a few reads in, not on the first."""
        driver = FakeDriver([FORM, FORM, FORM, FORM, FORM, EMAIL])
        result = login.log_in(driver, "a", "b", sleep=lambda *_: None)
        self.assertEqual(result, login.RESULT_EMAIL_CODE)

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
