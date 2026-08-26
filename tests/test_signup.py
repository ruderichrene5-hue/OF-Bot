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
    signup.SCREEN_CODE_OPTIONS: (
        "dismiss resend confirmation code resend confirmation code resend "
        "confirmation code change email address change email address change "
        "email address confirm with mobile number confirm with mobile "
        "number confirm with mobile number close"),
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
        self.enters_pressed = 0

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

    def press_enter(self):
        self.enters_pressed += 1

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


def test_the_code_options_sheet_is_dismissed_not_restarted():
    """jgjfjfcjjvjfjcncncg@gmail.com, 2026-08-26 (Blank 1): submitting the
    code with the keyboard's own action landed on this sheet instead of a
    real result. It has no marker of its own, so it read as SCREEN_UNKNOWN
    and burned both restart_app attempts on the identical sheet each time.
    `Dismiss` is the safe way back to the real code screen -- no app
    restart, and the whole rest of the chain can still finish."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_CODE_OPTIONS],
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
    assert ("Dismiss", "DISMISS", "Close", "CLOSE") in driver.taps


def test_an_unknown_screen_stops_the_run_with_its_text():
    driver = FakeDriver([SCREENS[signup.SCREEN_ENTRY], "a screen nobody has seen"])
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_UNKNOWN_SCREEN
    assert "nobody has seen" in result.detail


def test_an_unknown_screen_is_recovered_by_restarting_instagram():
    """`edwardshaffermem792@gmail.com`, 2026-08-25 (GeeLark/Android 16): got
    all the way to Instagram's own signup, hit a screen this flow had never
    seen, and stopped -- most of the time this is exactly the same app-state
    glitch `restart_app` already fixes for a backgrounded Instagram, not a
    screen the flow needs to understand. Restarting never reads or acts on
    the unknown screen's own content, so the safety property (never tap or
    type based on a misread screen) stays intact."""
    driver = RestartingDriver([
        SCREENS[signup.SCREEN_ENTRY],
        "a screen nobody has seen",
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert driver.restarts == 1
    assert result.status == signup.RESULT_CREATED


def test_an_unknown_screen_that_does_not_recover_still_gives_up():
    """The restart is one bounded attempt, not a new way to loop forever --
    and a screen that survives a restart still has to be reported, not
    silently retried away."""
    driver = RestartingDriver(
        [SCREENS[signup.SCREEN_ENTRY]] + ["a screen nobody has seen"] * 6)
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_UNKNOWN_SCREEN
    assert "nobody has seen" in result.detail
    assert driver.restarts == signup.MAX_APP_RESTARTS


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


def test_a_model_name_makes_the_username_carry_it():
    """Reversed on 2026-08-23 from the earlier "organic, no branding"
    choice: a real person asked for the model's name back in the @handle
    for signup specifically."""
    rng = random.Random(11)
    for _ in range(20):
        identity = make_identity(rng, model="Cloe")
        assert identity.username.lower().startswith("cloe")
        assert 3 <= len(identity.username) <= 28


def test_no_model_name_keeps_the_organic_pattern():
    """The default (no `model`) must be untouched -- still used for the
    instagramEdit path, which stays name-agnostic on purpose."""
    rng = random.Random(12)
    identity = make_identity(rng)
    assert not identity.username.lower().startswith("cloe")


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


class EmailFillCountingDriver(RestartingDriver):
    def __init__(self, screens):
        super().__init__(screens)
        self.email_fills = 0

    def fill(self, hints, value, what, submits_itself=False):
        if what == "email address":
            self.email_fills += 1
        return super().fill(hints, value, what, submits_itself)


def test_email_is_refilled_after_a_restart_wipes_the_app():
    """ruhu56898@gmail.com, 2026-08-26 (Blank 6): a restart puts Instagram
    back at "Join Instagram" -- every field blank again -- but this run's
    own memory of "already filled the email" survived the restart. The next
    pass through SCREEN_EMAIL saw "email" already in `done_flags`, skipped
    filling it, and submitted the field empty; Instagram bounced that back
    to the phone screen, whose escape hatch led straight back to the same
    empty email screen. 13 phone<->email cycles, 30 steps, `stuck`."""
    identity = signup.Identity(full_name="Mia Berg", username="mia.berg",
                               password="hunter2hunter", birth_day=12,
                               birth_month="April", birth_year=1999,
                               email="mia.berg@gmail.com")
    driver = EmailFillCountingDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_EMAIL],
        "something nobody has ever seen before",   # triggers a restart
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_EMAIL],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([]), identity,
                               sleep=lambda _s: None, mailbox=FakeMailbox())

    assert driver.email_fills == 2, (
        "the second pass through the email screen must fill it again, not "
        "assume a restart-wiped app still has what a prior pass typed")
    assert result.status == signup.RESULT_CREATED


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


# --- the other entry screen ---------------------------------------------------
# A freshly installed Instagram opens on its login form, with signup offered at
# the bottom. Read off the `gmail test` phone, 2026-08-13.
LOGIN_ENTRY = ("english (us) username, email or mobile number, password, log in "
               "forgot password? create new account terms and imprint")


def test_the_login_form_counts_as_the_entry_screen():
    assert signup.classify_signup_screen(LOGIN_ENTRY) == signup.SCREEN_ENTRY


def test_either_entry_screen_is_opened():
    for entry in (SCREENS[signup.SCREEN_ENTRY], LOGIN_ENTRY):
        driver = FakeDriver([entry, SCREENS[signup.SCREEN_PHONE],
                             SCREENS[signup.SCREEN_CODE],
                             SCREENS[signup.SCREEN_TERMS], DONE])
        result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                                   sleep=lambda _s: None)
        assert result.status == signup.RESULT_CREATED
        assert ("Get started", "Create new account") in driver.taps


def test_nothing_is_pressed_after_a_code_that_submits_itself():
    """With no keyboard up, Back is navigation: it took a finished code screen
    back to "What's your mobile number?" and undid the signup."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE],
        SCREENS[signup.SCREEN_CODE],      # still settling after the code
        SCREENS[signup.SCREEN_TERMS],     # no password screen, to isolate this
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_CREATED
    # Exactly one: the phone field. The code screen dismisses nothing, because
    # with no keyboard up Back walks the signup backwards.
    assert driver.keyboard_dismissals == 1


def test_a_code_screen_that_never_advances_gives_up_rather_than_retyping():
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
    ] + [SCREENS[signup.SCREEN_CODE]] * (signup.MAX_LOADING_WAITS + 4))
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_STUCK
    assert "did not advance" in result.detail
    # The generic repeat-guard (MAX_REPEATS = 4) ends the run well before a
    # separate wait counter ever could -- one safe recovery attempt fits
    # inside that budget, on the next-to-last try, not more.
    assert driver.enters_pressed == 1


def test_a_code_screen_that_recovers_after_enter_is_pressed():
    """`briangonzalezyi121@gmail.com`, 2026-08-25: the same six digits sat
    filled through four full waits with nothing ever pressed, and a code that
    had already arrived correctly was reported stuck. ENTER on the third read
    of the same screen is what a real recovery looks like -- the run must
    actually pick up from there, not just attempt it and give up anyway."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],
    ] + [SCREENS[signup.SCREEN_CODE]] * 3 + [
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_CREATED
    assert driver.enters_pressed == 1


# --- what comes after the account exists --------------------------------------
# All three read off `gmail test`, 2026-08-13, on the run that created
# @hanna.sommer33 through the email path.
ADD_PHONE = ("add a mobile number enter the mobile number where you can be "
             "contacted. no one will see this on your profile. de +49 mobile "
             "number you may receive sms")
PERMISSIONS_NEXT_ONLY = ("allow instagram to access your device? notifications "
                         "turning on notifications helps you keep up with your "
                         "friends. contacts contacts on this device will be "
                         "periodically synced. next, allow access or skip these "
                         "steps. you can change these settings anytime. next")
CONTACTS_DIALOG = "allow instagram to access your contacts? allow don't allow"
MESSAGE_NAG = ("only get message notifications turn on message notifications to "
               "keep up with chats and snooze everything else. turn on not now")


def test_the_post_creation_phone_prompt_is_not_a_finished_account():
    """It contains "no one will see this on your profile", which used to be
    read as evidence of a profile -- a false success, the worst kind here."""
    assert signup.classify_signup_screen(ADD_PHONE) == signup.SCREEN_ADD_PHONE


def test_no_signup_screen_is_mistaken_for_a_finished_account():
    for text in (ADD_PHONE, SCREENS[signup.SCREEN_EMAIL],
                 SCREENS[signup.SCREEN_PHONE]):
        assert signup.classify_signup_screen(text) != signup.SCREEN_DONE


def test_androids_own_permission_dialog_is_told_apart_from_instagrams_screen():
    assert signup.classify_signup_screen(CONTACTS_DIALOG) == \
        signup.SCREEN_PERMISSION_DIALOG
    assert signup.classify_signup_screen(PERMISSIONS_NEXT_ONLY) == \
        signup.SCREEN_PERMISSIONS


def test_the_message_notification_nag_is_skippable():
    assert signup.classify_signup_screen(MESSAGE_NAG) == \
        signup.SCREEN_INTERSTITIAL


def test_contacts_are_always_denied():
    """Contacts sync is what ties these accounts to one another."""
    driver = FakeDriver([CONTACTS_DIALOG, DONE])
    signup.run_signup(driver, FakeRouter([]), _identity(), sleep=lambda _s: None)
    assert ("DON'T ALLOW", "Don't allow", "Deny") in driver.taps


class HonestDriver(FakeDriver):
    """Taps only labels the screen really offers, matched exactly.

    The real driver compares against a node's whole text, which is why "Skip"
    must not be satisfied by the words "skip these steps" in a paragraph.
    """

    def __init__(self, screens_and_labels):
        super().__init__([text for text, _ in screens_and_labels])
        self._labels = [set(labels) for _, labels in screens_and_labels]
        self._current = set()

    def read_screen(self):
        self._current = self._labels.pop(0) if self._labels else set()
        return super().read_screen()

    def tap_label(self, labels):
        self.taps.append(tuple(labels))
        return any(label in self._current for label in labels)


def test_the_permissions_screen_advances_even_without_a_skip():
    """One variant offers Skip; the other only Next, which leads to the
    dialogs above -- where the answer is no."""
    driver = HonestDriver([
        (PERMISSIONS_NEXT_ONLY, {"Next"}),          # no Skip button at all
        (CONTACTS_DIALOG, {"DON'T ALLOW"}),
        (DONE, set()),
    ])
    signup.run_signup(driver, FakeRouter([]), _identity(), sleep=lambda _s: None)
    assert driver.taps[0] == signup._SKIP_LABELS      # tried Skip, not there
    assert driver.taps[1] == signup._SUBMIT_LABELS    # fell back to Next


# --- the email chain ----------------------------------------------------------
# Added 2026-08-16. The flow could only ever create accounts on rented SMS
# numbers, which is why the accounts it made cannot be recovered: no mailbox is
# attached to any of them. These exercise the other chain -- the
# `Sign up with email address` escape hatch, with the code read out of the Gmail
# app on the phone.

class FakeMailbox:
    """Stands in for `gmail_code.PhoneMailbox`."""

    def __init__(self, code="418902", raises=None):
        self._code = code
        self._raises = raises
        self.reads = 0
        self.e164 = ""

    def wait_for_code(self, timeout=None):
        self.reads += 1
        if self._raises is not None:
            raise self._raises
        return self._code

    def release(self, count_failure=False):
        return None


EMAIL_CODE_SCREEN = (
    "enter the confirmation code to confirm your profile, enter the 6-digit "
    "code we sent to hanna.sommer33@gmail.com. code input entry field next "
    "i didn't receive the code back")


def _email_identity(address="mia.berg1999@gmail.com"):
    identity = _identity()
    identity.email = address
    identity.email_password = "not-used-during-signup"
    return identity


def test_an_email_signup_never_rents_a_number():
    """The whole point: the SMS path spends money and leaves no way back in."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY],
        SCREENS[signup.SCREEN_PHONE],       # phone-first, take the escape hatch
        SCREENS[signup.SCREEN_EMAIL],
        EMAIL_CODE_SCREEN,
        SCREENS[signup.SCREEN_PASSWORD],
        SCREENS[signup.SCREEN_BIRTHDAY],
        SCREENS[signup.SCREEN_DATE_PICKER],
        SCREENS[signup.SCREEN_NAME],
        SCREENS[signup.SCREEN_USERNAME],
        SCREENS[signup.SCREEN_TERMS],
        DONE,
    ])
    router, mailbox = FakeRouter([]), FakeMailbox()
    result = signup.run_signup(driver, router, _email_identity(),
                               sleep=lambda _s: None, mailbox=mailbox)

    assert result.status == signup.RESULT_CREATED
    assert router.leased == 0                      # nothing was bought
    assert result.numbers_used == 0
    assert ("Sign up with email address", "Sign up with email") in driver.taps
    assert driver.filled["email address"] == "mia.berg1999@gmail.com"
    assert driver.filled["confirmation code"] == "418902"
    assert mailbox.reads == 1


def test_the_email_field_is_cleared_and_typed_not_accepted():
    """It arrives holding the phone's own Google account."""
    driver = FakeDriver([SCREENS[signup.SCREEN_EMAIL], EMAIL_CODE_SCREEN, DONE])
    signup.run_signup(driver, FakeRouter([]), _email_identity(),
                      sleep=lambda _s: None, mailbox=FakeMailbox())
    assert "email address" in driver.filled


def test_a_mailbox_that_never_delivers_says_so_rather_than_stalling():
    driver = FakeDriver([SCREENS[signup.SCREEN_EMAIL], EMAIL_CODE_SCREEN])
    result = signup.run_signup(driver, FakeRouter([]), _email_identity(),
                               sleep=lambda _s: None,
                               mailbox=FakeMailbox(code=""))
    assert result.status == signup.RESULT_MAILBOX
    assert "mia.berg1999@gmail.com" in result.detail


def test_a_mailbox_that_is_not_syncing_names_the_switch_to_turn_on():
    from adb_bot.automation.flows import gmail_code

    driver = FakeDriver([SCREENS[signup.SCREEN_EMAIL], EMAIL_CODE_SCREEN])
    broken = FakeMailbox(raises=gmail_code.MailboxNotReady(
        "mia.berg1999@gmail.com is not syncing -- turn 'Sync Gmail' on"))
    result = signup.run_signup(driver, FakeRouter([]), _email_identity(),
                               sleep=lambda _s: None, mailbox=broken)
    assert result.status == signup.RESULT_MAILBOX
    assert "Sync Gmail" in result.detail


def test_an_email_run_with_nothing_to_read_the_mailbox_with_stops():
    """Better than typing six digits from whatever inbox happens to be open."""
    driver = FakeDriver([SCREENS[signup.SCREEN_EMAIL], EMAIL_CODE_SCREEN])
    result = signup.run_signup(driver, FakeRouter([]), _email_identity(),
                               sleep=lambda _s: None, mailbox=None)
    assert result.status == signup.RESULT_ERROR
    assert "no way to read that mailbox" in result.detail


def test_an_sms_run_still_escapes_the_email_screen():
    """Without a mailbox the email screen is a dead end, so go back."""
    driver = FakeDriver([SCREENS[signup.SCREEN_EMAIL],
                         SCREENS[signup.SCREEN_PHONE],
                         SCREENS[signup.SCREEN_CODE], DONE])
    signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                      sleep=lambda _s: None)
    assert ("Sign up with mobile number",) in driver.taps


# --- a phone that already has an account --------------------------------------
def test_a_logged_in_phone_is_occupied_not_created():
    """`Default profile name (42)`, 2026-08-16.

    The runner offered it as an empty staging phone; it was running somebody's
    account, feed and all. Without this the first look reports `created` for an
    account that was never made.
    """
    driver = FakeDriver([DONE])
    result = signup.run_signup(driver, FakeRouter([]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_OCCUPIED
    assert result.status != signup.RESULT_CREATED
    assert driver.filled == {}          # nothing was typed on somebody's phone


def test_a_feed_reached_after_real_work_is_still_a_creation():
    """The guard must not refuse the accounts the flow actually makes."""
    driver = FakeDriver([
        SCREENS[signup.SCREEN_ENTRY], SCREENS[signup.SCREEN_PHONE],
        SCREENS[signup.SCREEN_CODE], SCREENS[signup.SCREEN_PASSWORD],
        SCREENS[signup.SCREEN_NAME], SCREENS[signup.SCREEN_USERNAME],
        SCREENS[signup.SCREEN_TERMS], DONE,
    ])
    result = signup.run_signup(driver, FakeRouter([FakeLease()]), _identity(),
                               sleep=lambda _s: None)
    assert result.status == signup.RESULT_CREATED


# What Instagram put in front of `@ida.sommer43` seconds after the `I agree`
# tap, 2026-08-18. The account existed: Instagram names it.
CHECKPOINT = ("menu confirm you're human to use your account, ida.sommer43 "
              "confirm you're human to use your account, ida.sommer43 "
              "continue continue")


def test_the_human_checkpoint_is_named_rather_than_unknown():
    assert signup.classify_signup_screen(CHECKPOINT) == signup.SCREEN_CHECKPOINT


def test_the_checkpoint_does_not_swallow_the_screens_it_shares_words_with():
    """It contains "account" and "continue", which several real screens do."""
    assert signup.classify_signup_screen(
        "join instagram get started") == signup.SCREEN_ENTRY
    assert signup.classify_signup_screen(
        "create a password continue") != signup.SCREEN_CHECKPOINT


# Meta's cookie consent, read off `Blank caio 2` on 2026-08-18. It arrives
# *after* the account is created -- it greets the new username by name -- and
# being unnamed it turned a finished signup into an `unknown_screen` failure.
COOKIES = ("more allow the use of cookies by instagram? allow the use of "
           "cookies by instagram? alina.sommer74, at meta, we use cookies and "
           "similar technologies decline optional cookies allow all cookies")


def test_the_cookie_consent_is_named_rather_than_unknown():
    """`@alina.sommer74` was created and reported as a failure on this screen,
    which also meant the verification step never ran on a real account."""
    assert signup.classify_signup_screen(COOKIES) == signup.SCREEN_COOKIES


def test_the_cookie_screen_is_not_read_as_the_terms():
    """Both are consent pages, and the terms handler's tap is what creates the
    account -- tapping it again here would be answering the wrong question."""
    assert signup.classify_signup_screen(COOKIES) != signup.SCREEN_TERMS


def test_the_screens_around_it_still_classify_as_themselves():
    assert signup.classify_signup_screen(
        "follow 5 or more people following isn't required") == signup.SCREEN_FOLLOW
    assert signup.classify_signup_screen(
        "add a profile photo") == signup.SCREEN_PHOTO_PROMPT
