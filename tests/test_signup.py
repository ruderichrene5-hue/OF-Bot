"""Signup screens and the loop over them, tested off real dump text.

Every string in `SCREENS` was read off an actual phone during the 2026-08-13
run; they are kept verbatim (lowercased, as `read_screen` returns them) so a
marker list cannot drift away from what the screens really say.
"""

import random

import pytest

from adb_bot.automation.flows import signup
from adb_bot.automation.signup_identity import make_identity

# --- real screen text ---------------------------------------------------------
SCREENS = {
    signup.SCREEN_ENTRY: (
        "english (us) join instagram share what you're into with the people who "
        "get you. get started i already have a profile meta logo"),
    signup.SCREEN_PHONE: (
        "what's your mobile number? enter the mobile number on which you can be "
        "contacted. no one will see this on your profile. mobile number you may "
        "receive whatsapp and sms notifications from us. learn more next sign up "
        "with email address i already have an account back"),
    signup.SCREEN_EMAIL: (
        "what's your email address? enter the email address at which you can be "
        "contacted. no one will see this on your profile. email address "
        "next sign up with mobile number i already have an account back"),
    signup.SCREEN_CODE: (
        "enter the confirmation code to confirm your profile, enter the 6-digit "
        "code that we sent via sms to +4915905609843. code input entry field "
        "next i didn't receive the code back"),
    signup.SCREEN_PASSWORD: (
        "create a password create a password with at least six letters or "
        "numbers. it should be something that others can't guess. password "
        "remember login info. learn more next i already have an account back"),
    signup.SCREEN_BIRTHDAY: (
        "what's your date of birth? use your own date of birth, even if this "
        "account is for a business, a pet or something else. no one will see "
        "this unless you choose to share it. why do i need to provide my date "
        "of birth? next i already have an account back"),
    signup.SCREEN_DATE_PICKER: (
        "set date 12 13 14 jul aug sept 2025 2026 2027 cancel set"),
    signup.SCREEN_NAME: (
        "what's your name? full name next i already have an account back"),
    signup.SCREEN_USERNAME: (
        "create a username add a username or use our suggestion. you can change "
        "this at any time. username next back"),
    signup.SCREEN_TERMS: (
        "agree to instagram's terms and policies people who use our service may "
        "have uploaded your contact information to instagram. learn more by "
        "tapping i agree, you agree to create an account and to instagram's "
        "terms. i agree i already have an account back"),
    signup.SCREEN_PERMISSIONS: (
        "allow instagram to access your device? notifications turning on "
        "notifications helps you keep up with your friends. contacts contacts "
        "on this device will be periodically synced. skip"),
    signup.SCREEN_PHOTO_PROMPT: (
        "skip add a profile photo that shows your vibe add a photo import from "
        "facebook"),
    signup.SCREEN_FOLLOW: (
        "follow 5 or more people search hand, fuss, mund following isn't "
        "required, but it's more fun skip"),
    signup.SCREEN_ADD_EMAIL: (
        "add an email address enter the email where you can be contacted. no "
        "one will see this on your profile. i1aikjgs11a@gmail.com email skip"),
    signup.SCREEN_INTERSTITIAL: (
        "see more of what you love in your feed video preview photo preview "
        "options less like this more like this back skip"),
    signup.SCREEN_SAVE_PASSWORD: (
        "save password to google password manager? passwords are saved to "
        "google password manager username 1787254899 password not now continue"),
}


@pytest.mark.parametrize("expected,text", sorted(SCREENS.items()))
def test_every_real_screen_is_named(expected, text):
    assert signup.classify_signup_screen(text) == expected


def test_a_working_profile_reads_as_done():
    text = ("your profile. cici aurainta 0 posts 0 followers 0 following add "
            "your bio edit profile share profile discover people")
    assert signup.classify_signup_screen(text) == signup.SCREEN_DONE


def test_an_unseen_screen_is_unknown_not_done():
    """The property the whole flow leans on: unnamed means stop, not proceed."""
    assert signup.classify_signup_screen(
        "something nobody has ever seen before") == signup.SCREEN_UNKNOWN
    assert signup.classify_signup_screen("") == signup.SCREEN_UNKNOWN


def test_the_code_screen_wins_over_the_phone_screen():
    """It names the number it just texted, so it looks like a phone screen."""
    text = SCREENS[signup.SCREEN_CODE] + " what's your mobile number?"
    assert signup.classify_signup_screen(text) == signup.SCREEN_CODE


def test_the_post_creation_email_prompt_is_not_the_signup_email_screen():
    """Confusing them types an address into a live account's settings."""
    assert signup.classify_signup_screen(
        SCREENS[signup.SCREEN_ADD_EMAIL]) == signup.SCREEN_ADD_EMAIL


# --- the loop -----------------------------------------------------------------
class FakeLease:
    def __init__(self, code="653741", number="+4915905609843"):
        self.e164 = number
        self.typed_number = number[3:]
        self._code = code
        self.released = False

    def wait_for_code(self, timeout=None):
        return self._code

    def release(self, count_failure=True):
        self.released = True


class FakeRouter:
    def __init__(self, leases):
        self._leases = list(leases)
        self.leased = 0

    def lease(self, *a, **kw):
        self.leased += 1
        return self._leases.pop(0)


class FakeDriver:
    """Replays a scripted sequence of screens and records what was done."""

    def __init__(self, screens):
        self._screens = list(screens)
        self.filled = {}
        self.autosubmitted = {}
        self.taps = []
        self.dates = []
        self.keyboard_dismissals = 0

    def read_screen(self):
        return self._screens.pop(0) if self._screens else SCREENS[
            signup.SCREEN_INTERSTITIAL]

    def tap_label(self, labels):
        self.taps.append(tuple(labels))
        return True

    def fill(self, hints, value, what, submits_itself=False):
        self.filled[what] = value
        self.autosubmitted[what] = submits_itself
        return True

    def dismiss_keyboard(self):
        self.keyboard_dismissals += 1

    def set_date(self, day, month, year):
        self.dates.append((day, month, year))
        return True


DONE = ("your profile. mia berg 0 posts 0 followers 0 following add your bio "
        "edit profile share profile")


def _identity():
    return signup.Identity(full_name="Mia Berg", username="mia.berg",
                           password="hunter2hunter", birth_day=12,
                           birth_month="April", birth_year=1999)


def test_a_whole_signup_walks_to_created():
    driver = FakeDriver([
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
    ])
    lease = FakeLease()
    result = signup.run_signup(driver, FakeRouter([lease]), _identity(),
                               sleep=lambda _s: None)

    assert result.status == signup.RESULT_CREATED
    assert result.username == "mia.berg"
    assert result.numbers_used == 1
    # The three fields that arrive pre-filled with something wrong are all
    # written, not accepted.
    assert driver.filled["username"] == "mia.berg"
    assert driver.filled["mobile number"] == "15905609843"
    assert driver.filled["confirmation code"] == "653741"
    assert driver.dates == [(12, "April", 1999)]
    assert ("I agree",) in driver.taps
    assert driver.keyboard_dismissals >= 3


def test_an_unknown_screen_stops_the_run_with_its_text():
    driver = FakeDriver([SCREENS[signup.SCREEN_ENTRY], "a screen nobody has seen"])
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_UNKNOWN_SCREEN
    assert "nobody has seen" in result.detail


def test_a_number_that_never_delivers_is_swapped_and_released():
    dead, good = FakeLease(code=None), FakeLease()
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],      # dead number -> Back
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],      # second number delivers
        SCREENS[signup.SCREEN_PASSWORD],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([dead, good]), _identity(),
                               sleep=lambda _s: None)

    assert result.status == signup.RESULT_CREATED
    assert result.numbers_used == 2
    assert dead.released and good.released


def test_the_number_budget_is_finite():
    leases = [FakeLease(code=None) for _ in range(signup.MAX_NUMBER_ATTEMPTS)]
    screens = [SCREENS[signup.SCREEN_ENTRY]]
    for _ in range(signup.MAX_NUMBER_ATTEMPTS + 1):
        screens += [SCREENS[signup.SCREEN_PHONE], SCREENS[signup.SCREEN_CODE]]
    result = signup.run_signup(FakeDriver(screens), FakeRouter(leases),
                               _identity(), sleep=lambda _s: None)

    assert result.status == signup.RESULT_NO_NUMBER
    assert result.numbers_used == signup.MAX_NUMBER_ATTEMPTS
    assert all(lease.released for lease in leases)


def test_a_screen_that_never_advances_gives_up():
    driver = FakeDriver([SCREENS[signup.SCREEN_PASSWORD]] * 10)
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_STUCK


def test_a_banned_screen_is_not_treated_as_a_step():
    driver = FakeDriver([
        "sorry, your account has been disabled for violating our terms"])
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_BANNED


# --- identities ---------------------------------------------------------------
def test_identities_are_plausible_and_adult():
    rng = random.Random(7)
    for _ in range(40):
        identity = make_identity(rng)
        assert "link" not in identity.username
        assert 3 <= len(identity.username) <= 28
        assert len(identity.password) >= 6
        assert 1 <= identity.birth_day <= 28
        assert identity.birth_year <= 2026 - 23


def test_a_known_username_is_not_handed_out_again():
    rng = random.Random(3)
    first = make_identity(rng)
    again = make_identity(random.Random(3), avoid=[first.username])
    assert again.username != first.username


# --- screens caught mid-render ------------------------------------------------
def test_a_half_drawn_screen_is_loading_not_unknown():
    """Both of these are real: replaying the 2026-08-13 recordings through the
    classifier found exactly these two Instagram screens unnamed, and stopping
    a run on either would abandon an account for no reason."""
    assert signup.classify_signup_screen("checking info…") == signup.SCREEN_LOADING
    assert signup.classify_signup_screen("next") == signup.SCREEN_LOADING


def test_a_real_screen_is_never_dismissed_as_loading():
    """The password screen says 'loading' on its own button while submitting."""
    submitting = SCREENS[signup.SCREEN_PASSWORD] + " loading loading"
    assert signup.classify_signup_screen(submitting) == signup.SCREEN_PASSWORD


def test_the_run_waits_through_loading_then_carries_on():
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        "checking info…",
        "next",
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_CREATED
    assert signup.SCREEN_LOADING not in result.steps


def test_a_screen_that_never_draws_ends_the_run():
    """A blank screen appeared after a code was accepted and never rendered."""
    driver = FakeDriver(["checking info…"] * (signup.MAX_LOADING_WAITS + 3))
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_STUCK
    assert "never finished drawing" in result.detail


# --- the phone on its home screen ---------------------------------------------
LAUNCHER = ("search gallery gallery play store play store home telephone "
            "telephone messaging messaging music music chrome chrome camera")


def test_the_home_screen_is_named_not_unknown():
    assert signup.classify_signup_screen(LAUNCHER) == signup.SCREEN_LAUNCHER


class RestartingDriver(FakeDriver):
    def __init__(self, screens, can_restart=True):
        super().__init__(screens)
        self.restarts = 0
        self._can_restart = can_restart

    def restart_app(self):
        self.restarts += 1
        return self._can_restart


def test_a_backgrounded_instagram_is_restarted_mid_signup():
    driver = RestartingDriver([
        SCREENS[signup.SCREEN_ENTRY],
        LAUNCHER,
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert driver.restarts == 1
    assert result.status == signup.RESULT_CREATED
    assert signup.SCREEN_LAUNCHER not in result.steps


def test_a_phone_that_will_not_run_instagram_stops_the_signup():
    driver = RestartingDriver([LAUNCHER] * 8, can_restart=False)
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_STUCK
    assert "would not start" in result.detail


# --- a submit that is still working -------------------------------------------
# Verbatim from `Blank (1)`, 2026-08-13: the password had gone through on the
# first tap and the button had renamed itself, so re-tapping `Next` found no
# such button and the run gave up on a verified number after four tries.
PASSWORD_SUBMITTING = (
    "create a password create a password with at least six letters or numbers. "
    "it should be something that others can't guess. password •••••••••••• "
    "password, remember login info. learn more loading loading "
    "i already have an account back")


def test_a_screen_mid_submit_is_still_its_own_screen():
    assert signup.classify_signup_screen(PASSWORD_SUBMITTING) == \
        signup.SCREEN_PASSWORD
    assert signup.submit_in_flight(PASSWORD_SUBMITTING)


def test_an_idle_screen_is_not_mistaken_for_a_working_one():
    assert not signup.submit_in_flight(SCREENS[signup.SCREEN_PASSWORD])
    assert not signup.submit_in_flight(SCREENS[signup.SCREEN_USERNAME])


def test_the_run_waits_for_a_submit_instead_of_tapping_again():
    """The regression that cost the first real run its account."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_PASSWORD],
        PASSWORD_SUBMITTING,      # the tap landed; the button says Loading
        PASSWORD_SUBMITTING,
        PASSWORD_SUBMITTING,
        PASSWORD_SUBMITTING,      # more repeats than MAX_REPEATS allows
        PASSWORD_SUBMITTING,
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_CREATED
    # The password is typed once, not once per look.
    assert driver.filled["password"] == "hunter2hunter"


def test_a_submit_that_never_finishes_still_ends_the_run():
    driver = FakeDriver([SCREENS[signup.SCREEN_ENTRY]]
                        + [PASSWORD_SUBMITTING] * (signup.MAX_LOADING_WAITS + 4))
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_STUCK
    assert "still working" in result.detail


def test_the_confirmation_code_is_marked_as_self_submitting():
    """It acts on the sixth digit, so an empty field afterwards is success."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                      sleep=lambda _s: None)
    assert driver.autosubmitted["confirmation code"] is True
    assert driver.autosubmitted.get("mobile number") is False


# --- the phone going away -----------------------------------------------------
def test_a_dead_phone_is_named_as_such_not_as_an_unknown_screen():
    """A cloud phone that dies returns nothing; reporting that as an
    unrecognised screen sends somebody hunting for a marker list that does not
    exist. Seen on `Blank (1)`, 2026-08-13, which ended `unknown_screen:` with
    an empty detail."""
    driver = FakeDriver([SCREENS[signup.SCREEN_ENTRY], "", "", "", ""])
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_PHONE_LOST
    assert "stopped answering" in result.detail


def test_one_empty_read_is_forgiven():
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        "",                                   # a redraw, not a dead phone
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_CREATED
