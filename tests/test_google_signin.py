"""Naming the screens of the Play Store sign-in, off real dumps.

Every string here was read off `Blank caio 1` on 2026-08-16 (lowercased, as
`read_screen` returns them). The expensive failures this guards against are
both about *over*-matching: calling a half-drawn screen unknown and stopping a
run that was fine, and calling Google's generic "something went wrong" a
diagnosis when it is the same sentence Google uses for half a dozen unrelated
failures.
"""

from adb_bot.automation.flows import google_signin as g

PLAY_SIGNED_OUT = ("options sign in to find the latest android apps, games, "
                   "movies, music & more sign in")

EASE = ("sign in – google accounts sign in with ease we can search for "
        "accounts connected to this phone number by obtaining your number from "
        "your operator and exchanging device info, such as your sim card "
        "identifier (standard message and data rates may apply). skip next")

EMAIL = ("sign in use your google account. the account will be added to this "
         "device and available to other google apps.learn more about using "
         "your account forgot email? create account next")

LOOKUP_FAILED = ("something went wrong something went wrong we weren’t able to "
                 "check for accounts connected to your phone number. something "
                 "went wrong sign in another way")

SEARCHING = ("searching for accounts this will take just a moment searching "
             "for accounts this will take just a moment loading go back")


def test_the_play_store_signed_out_screen():
    assert g.classify_google_screen(PLAY_SIGNED_OUT) == g.SCREEN_PLAY_SIGNIN


def test_sign_in_with_ease_is_named_so_it_can_be_skipped():
    """It is a lookup by phone number, and these phones have no usable SIM."""
    assert g.classify_google_screen(EASE) == g.SCREEN_EASE


def test_the_google_email_screen():
    assert g.classify_google_screen(EMAIL) == g.SCREEN_EMAIL


def test_the_failed_phone_lookup_is_named_by_its_own_sentence():
    assert g.classify_google_screen(LOOKUP_FAILED) == g.SCREEN_EASE_FAILED


def test_a_bare_something_went_wrong_is_not_the_phone_lookup():
    """Google says this for everything. Treating it as one thing is how the
    2026-08-13 run concluded Google refuses these phones outright."""
    assert g.classify_google_screen(
        "something went wrong") != g.SCREEN_EASE_FAILED


SERVER_ERROR = ("something went wrong there was a problem communicating with "
                "google servers. try again later.")


def test_google_being_unreachable_is_named_not_unknown():
    """Verbatim from `Blank caio 1`, 2026-08-17. It stopped a whole launch."""
    assert g.classify_google_screen(SERVER_ERROR) == g.SCREEN_SERVER_ERROR


def test_the_server_error_is_matched_on_the_servers_sentence():
    """Not on the "something went wrong" heading it shares with the failed
    phone lookup -- which must still classify as itself."""
    assert g.classify_google_screen(LOOKUP_FAILED) == g.SCREEN_EASE_FAILED
    assert g.classify_google_screen(
        "something went wrong") != g.SCREEN_SERVER_ERROR


RETRY_PAGE = ("something went wrong sorry, something went wrong there. please "
              "try again. next")


def test_googles_retry_page_is_named_not_unknown():
    """It appeared right after the Terms on `Blank caio 1` -- by which point
    2FA had already passed, so stopping there threw away a finished sign-in."""
    assert g.classify_google_screen(RETRY_PAGE) == g.SCREEN_RETRY


def test_the_retry_page_is_not_confused_with_the_unreachable_servers_page():
    """Different pages, different handling: one has a button, one has none."""
    assert g.classify_google_screen(SERVER_ERROR) == g.SCREEN_SERVER_ERROR
    assert g.classify_google_screen(RETRY_PAGE) != g.SCREEN_SERVER_ERROR


RETRY_PAGE_WITH_STALE_ROBOT_CHECK_TEXT = (
    "verify that it’s you to help keep your account safe, google wants to "
    "make sure that it’s really you trying to sign in loading indeterminate, "
    "loading verify that it’s you to help keep your account safe, google "
    "wants to make sure that it’s really you trying to sign in "
    "lucas18anosff@gmail.com confirm that you're not a robot something went "
    "wrong something went wrong sorry, something went wrong there. please "
    "try again."
)


def test_a_retry_page_after_a_genuine_solve_is_not_read_as_a_fresh_robot_check():
    """`lucas18anosff@gmail.com`, 2026-08-25 (GeeLark/Android 16): the grid was
    genuinely solved -- "the reCAPTCHA challenge cleared" logged correctly --
    but the very next screen was Google's own "Sorry, something went wrong
    there" / Restart error page, with the reCAPTCHA widget's stale "confirm
    that you're not a robot" text still sitting in the same dump above it.
    Reading that as ROBOT_CHECK sent the flow searching for a checkbox that no
    longer existed and threw away a real solve; RETRY is the specific,
    handle-able page that is actually on screen."""
    assert (g.classify_google_screen(RETRY_PAGE_WITH_STALE_ROBOT_CHECK_TEXT)
           == g.SCREEN_RETRY)


SERVICES = ("google services cicirahmaputrimu@gmail.com tap to learn more "
            "about each service, such as how to turn it on or off later. data "
            "will be used according to google's privacy policy. backup back up "
            "device data automatically back up your data")


def test_the_google_services_consent_page_is_named():
    """`Blank caio 2` reached it with 2FA and the Terms already behind it --
    the last screen before the Play Store, and it stopped the run."""
    assert g.classify_google_screen(SERVICES) == g.SCREEN_SERVICES


def test_the_services_page_is_not_read_as_the_terms():
    """Both are consent pages and the Terms handler taps "I agree", which is
    not what ends this one."""
    assert g.classify_google_screen(SERVICES) != g.SCREEN_TERMS


def test_the_services_page_taps_are_bounded():
    """It is a long scrolling page whose button stays "More" until the bottom,
    so it is exempt from the repeat guard and needs a bound of its own."""
    class ServicesDriver(_StubDriver):
        def input_hints(self):
            return []

    driver, adb = ServicesDriver(SERVICES), _StubAdb()
    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
                        sleep=lambda _s: None)

    assert verdict == g.RESULT_STUCK
    taps = [t for t in driver.taps if "Accept" in t]
    assert len(taps) == g.MAX_SERVICES_TAPS, \
        f"tapped the consent page {len(taps)} times"


PLAY_TIP = ("google is optimising app installs with your help google play "
           "makes apps faster to install, open and run based on what people "
           "are using most. the first time that you open an app after "
           "installing, google notes which parts of the app you use.")


def test_the_play_store_install_tip_is_named():
    """No address anywhere in this dump -- it is Play Store's own one-time
    tip, not part of the account's sign-in chain. Seen live 2026-08-25
    (unnikuttan114121@gmail.com) as the very first screen after `glogin`,
    where it used to read as `unknown_screen` before sign-in ever started."""
    assert g.classify_google_screen(PLAY_TIP) == g.SCREEN_PLAY_TIP


def test_the_install_tip_is_dismissed_and_sign_in_continues():
    """Tapping its `OK` clears it out of the way so the real sign-in chain
    (here: the email screen) gets a chance to run, instead of the flow
    giving up on the first screen it sees."""
    class TipThenEmailDriver(_StubDriver):
        def __init__(self):
            super().__init__(PLAY_TIP)

        def tap_label(self, labels):
            self.taps.append(labels)
            self.text = EMAIL
            return True

        def fill(self, hints, value, what, **kw):
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = TipThenEmailDriver(), _StubAdb()
    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
             sleep=lambda _s: None)

    assert any("OK" in t or "Ok" in t for t in driver.taps), \
        f"never tapped past the install tip: {driver.taps}"


def test_searching_for_accounts_is_loading_not_a_screen_to_act_on():
    assert g.classify_google_screen(SEARCHING) == g.SCREEN_LOADING


def test_a_dump_of_pure_chrome_is_still_drawing():
    """`skip next` alone stopped a run that was otherwise fine."""
    assert g.classify_google_screen("skip next") == g.SCREEN_LOADING
    assert g.classify_google_screen("next") == g.SCREEN_LOADING


# Read off `test claude OWN PROXY (2)` live, 2026-08-24, right after the
# password screen (brendv748@gmail.com): a risk-check loading step whose
# dump repeats the "verify that it's you" boilerplate around it and so runs
# well past 300 characters, which is what the plain `_LOADING_MARKERS` path
# requires to stay under. Read as an unnamed screen instead of loading.
RISK_CHECK_LOADING = (
    "this may take a few moments… to help keep your account safe, "
    "google wants to make sure that it’s really you trying to sign in "
    "this may take a few moments… to help keep your account safe, "
    "google wants to make sure that it’s really you trying to sign in "
    "brendv748@gmail.com loading this may take a few")


def test_a_long_risk_check_loading_screen_is_not_unnamed():
    assert g.classify_google_screen(RISK_CHECK_LOADING) == g.SCREEN_LOADING


def test_a_screen_with_real_content_is_never_dismissed_as_loading():
    assert g.classify_google_screen(
        "a screen nobody here has ever seen before") == g.SCREEN_UNKNOWN


def test_an_empty_read_is_unknown():
    assert g.classify_google_screen("") == g.SCREEN_UNKNOWN
    assert g.classify_google_screen(None) == g.SCREEN_UNKNOWN


def test_the_password_screen_does_not_swallow_the_2fa_chooser():
    """`_PASSWORD_MARKERS` carries "welcome", which is on several screens."""
    chooser = ("2-step verification welcome to keep your account secure, "
               "google wants to make sure it's really you. get a verification "
               "code from the google authenticator app try another way")
    assert g.classify_google_screen(chooser) == g.SCREEN_2FA_CHOOSER


# Verbatim from `Blank caio 1`, 2026-08-17. The chooser and the code screen
# say the same thing; only the second one has a field.
TWO_FA_BOTH = ("2-step verification to help keep your account safe, google "
               "wants to make sure that it's really you trying to sign in "
               "cicimuammark@gmail.com 2-step verification get a verification "
               "code from the google authenticator app try another way next")


def test_a_2fa_screen_with_a_code_field_is_the_code_screen():
    assert g.classify_google_screen(
        TWO_FA_BOTH, ["enter code totppin"]) == g.SCREEN_TOTP


def test_the_same_words_with_no_field_are_only_the_chooser():
    """Read by text alone this bounced chooser -> code -> chooser until the
    repeat guard stopped the run, having typed nothing."""
    assert g.classify_google_screen(
        TWO_FA_BOTH, []) == g.SCREEN_2FA_CHOOSER


def test_classification_still_works_with_no_field_information():
    """`field_hints` is optional; every existing caller passes nothing."""
    assert g.classify_google_screen(LOOKUP_FAILED) == g.SCREEN_EASE_FAILED


def test_a_code_field_does_not_rename_an_unrelated_screen():
    """The field is what distinguishes the 2FA pair, not a wildcard: a screen
    with no text at all is still unknown."""
    assert g.classify_google_screen("", ["enter code totppin"]) == g.SCREEN_UNKNOWN


def test_accounts_on_device_reads_dumpsys():
    class FakeAdb:
        def run_command(self, command):
            assert "dumpsys account" in command
            return ("Accounts: 1\n"
                    "  Account {name=cicimuammark@gmail.com, type=com.google}\n")

    assert g.accounts_on_device(FakeAdb(), "host:1") == ["cicimuammark@gmail.com"]


def test_no_accounts_reads_as_empty():
    class FakeAdb:
        def run_command(self, command):
            return "Accounts: 0\n"

    assert g.accounts_on_device(FakeAdb(), "host:1") == []


class _StubAdb:
    """Answers every shell command with nothing, and remembers the backs."""

    def __init__(self):
        self.commands = []
        self.backs = 0

    def run_command(self, command):
        self.commands.append(command)
        return "Accounts: 0" if "dumpsys account" in command else ""

    def shell_back(self, target):
        self.backs += 1
        return ""


class _StubDriver:
    """Shows one screen forever."""

    def __init__(self, text):
        self.text = text
        self.taps = []

    def read_screen(self):
        return self.text

    def tap_label(self, labels):
        self.taps.append(labels)
        return True


class _RetryPageAdb(_StubAdb):
    """Like `_StubAdb`, but `dumpsys account` starts empty and can start
    reporting the address once Play Store/Google Services are force-closed --
    manual signups found the account already signed in only *after* that
    restart, not before it (2026-08-22), so the stub must not show it any
    earlier than the real phone did."""

    def __init__(self, shows_up_after_restart: bool):
        super().__init__()
        self.shows_up_after_restart = shows_up_after_restart
        self.restarted = False

    def run_command(self, command):
        self.commands.append(command)
        if "force-stop" in command:
            self.restarted = True
        if "dumpsys account" in command:
            if self.restarted and self.shows_up_after_restart:
                return "Account {name=a@gmail.com, type=com.google}"
            return "Accounts: 0"
        return ""


def test_the_retry_page_checks_for_a_real_sign_in_before_tapping_anything():
    """The retry page is a Play Store UI glitch, not proof the sign-in
    failed -- so the real source of truth, `dumpsys account`, is checked
    before spending a retry on the on-screen button, which walks the whole
    flow from scratch and can lose a sign-in that already went through."""
    driver = _StubDriver(RETRY_PAGE)
    adb = _RetryPageAdb(shows_up_after_restart=True)

    result = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
                       sleep=lambda _s: None)

    assert result == g.RESULT_SIGNED_IN
    assert any("force-stop" in c and g.PLAY_PACKAGE in c for c in adb.commands)
    assert any("force-stop" in c and "com.google.android.gms" in c
              for c in adb.commands)
    assert not driver.taps, "tapped the retry button instead of trusting " \
                            "the real account state"


def test_the_retry_page_still_falls_back_to_tapping_when_nothing_landed():
    """If the account genuinely is not on the device, the old behaviour --
    tap through the retry page -- must still run."""
    driver = _StubDriver(RETRY_PAGE)
    adb = _RetryPageAdb(shows_up_after_restart=False)

    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
             sleep=lambda _s: None)

    assert driver.taps, "never fell back to the on-screen retry button"


def test_the_retry_pages_own_button_can_be_restart_not_just_retry():
    """`lucas18anosff@gmail.com`, 2026-08-25: the button on this variant of
    the page reads "Restart", not "Try again"/"Retry" -- the only labels the
    fallback used to know."""
    driver = _StubDriver(RETRY_PAGE)
    adb = _RetryPageAdb(shows_up_after_restart=False)

    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
             sleep=lambda _s: None)

    assert any("Restart" in labels for labels in driver.taps)


def test_the_code_screen_falls_back_to_the_keyboards_own_action(monkeypatch):
    """`NEXT` does not submit Google's forms -- four fresh codes were typed and
    tapped in on 2026-08-17 and the screen simply redrew each time. The email
    form needed the same fallback."""
    monkeypatch.setattr(g.totp, "fresh_code", lambda secret: ("123456", 30))

    class CodeDriver(_StubDriver):
        def __init__(self):
            super().__init__(TWO_FA_BOTH)
            self.filled = []

        def input_hints(self):
            return ["enter code totppin"]

        def fill(self, hints, value, what, **kw):
            self.filled.append(value)
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = CodeDriver(), _StubAdb()
    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
              sleep=lambda _s: None)

    enters = [c for c in adb.commands if "keyevent 66" in c]
    assert enters, "never tried the IME action, which is the one thing that works"
    assert len(driver.filled) > 1, "a retry must type a fresh code, not resubmit"


def test_a_code_still_being_checked_is_not_retyped(monkeypatch):
    """The one run where Google accepted the code, the next dump still showed
    the field -- with a spinner beside it -- and the flow typed a fresh code
    straight over the submission in flight (2026-08-17, 14:01:15)."""
    codes = iter(f"{n:06d}" for n in range(1, 99))
    monkeypatch.setattr(g.totp, "fresh_code", lambda s: (next(codes), 30))

    class HoldingDriver(_StubDriver):
        """Keeps whatever was typed, exactly as the real screen did."""

        def __init__(self):
            super().__init__(TWO_FA_BOTH)
            self.filled = []

        def input_hints(self):
            return ["enter code totppin"]

        def input_values(self):
            return [self.filled[-1]] if self.filled else [""]

        def fill(self, hints, value, what, **kw):
            self.filled.append(value)
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = HoldingDriver(), _StubAdb()
    slept = []
    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
              sleep=slept.append)

    # The invariant: every code after the first is preceded by a full wait, so
    # nothing is ever typed over a submission Google is still checking.
    waits = slept.count(g.CODE_WAIT_SECONDS)
    assert waits >= (len(driver.filled) - 1) * g.MAX_CODE_WAITS, \
        f"typed {len(driver.filled)} codes but only waited {waits} times"
    assert waits, "never waited for Google to answer the code it was given"


def test_a_dump_that_comes_back_empty_is_looked_at_again():
    """`Blank caio 3` ended a run two steps in on a dump that returned nothing.
    Nothing is a failed read, not a screen that cannot be named."""
    class Flaky(_StubDriver):
        def __init__(self):
            super().__init__("")
            self.reads = 0

        def read_screen(self):
            self.reads += 1
            # Empty twice, then the Play Store is there all along.
            return "" if self.reads <= 2 else PLAY_SIGNED_OUT

    driver, adb = Flaky(), _StubAdb()
    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
              sleep=lambda _s: None)

    assert driver.taps, "gave up on an empty dump instead of looking again"


def test_a_phone_that_never_answers_still_stops():
    driver, adb = _StubDriver(""), _StubAdb()
    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
                        sleep=lambda _s: None)
    assert verdict == g.RESULT_STUCK


def test_a_screen_with_nothing_on_it_gets_the_phone_woken():
    """Nothing to dump and no focused window is what a sleeping phone looks
    like, and waking it costs two commands."""
    driver, adb = _StubDriver(""), _StubAdb()
    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "S",
              sleep=lambda _s: None)

    assert any("keyevent 224" in c for c in adb.commands), "never woke it"
    # Not POWER, which would switch off a screen that is already on.
    assert not any("keyevent 26" in c for c in adb.commands)


def test_google_being_unreachable_gives_up_rather_than_looping():
    """The screen has no buttons, so a retry that never stops would spend the
    phone's whole ~15-minute life backing out of the same page."""
    driver = _StubDriver(SERVER_ERROR)
    adb = _StubAdb()
    slept = []

    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
                        sleep=slept.append)

    assert verdict == g.RESULT_GOOGLE_UNREACHABLE
    assert adb.backs == g.MAX_SERVER_ERRORS
    assert sum(slept) < 15 * 60


LAUNCHER = ("search gallery gallery play store play store home telephone "
            "telephone messaging messaging music music chrome chrome camera "
            "camera")


def test_the_home_screen_is_named_so_the_app_can_be_started_again():
    """`Blank caio 2` ended on exactly this, called it unknown, and stopped a
    run whose only problem was that the Play Store had not come up."""
    assert g.classify_google_screen(LAUNCHER) == g.SCREEN_LAUNCHER


def test_the_home_screen_is_not_mistaken_for_the_play_store_being_signed_out():
    assert g.classify_google_screen(LAUNCHER) != g.SCREEN_PLAY_SIGNIN


# Read off `Blank caio 2` on 2026-08-18, for `hasan428483@gmail.com` -- an
# unused pool mailbox Google challenged straight after the address, before it
# had asked for a password at all.
ROBOT_CHECK = (
    "verify that it’s you to help keep your account safe, google wants to "
    "make sure that it’s really you trying to sign in verify that it’s "
    "you to help keep your account safe, google wants to make sure that it’s "
    "really you trying to sign in hasan428483@gmail.com confirm that you're not "
    "a robot try another way")


def test_a_robot_check_is_named_rather_than_left_unknown():
    """It reported `unknown_screen`, which reads as "the flow got confused" and
    invites a retry -- and a retry costs a launch and lands here again. The
    mailbox is the thing that has to change, so the screen has to say so."""
    assert g.classify_google_screen(ROBOT_CHECK) == g.SCREEN_ROBOT_CHECK


def test_a_robot_check_is_not_mistaken_for_a_wrong_password():
    """Google has not asked for a password yet, so blaming the credentials
    would send the next run at a mailbox whose password is fine."""
    assert g.classify_google_screen(ROBOT_CHECK) != g.SCREEN_WRONG_PASSWORD


def test_a_robot_check_stops_the_run_with_its_own_verdict():
    driver, adb = _StubDriver(ROBOT_CHECK), _StubAdb()

    verdict = g.sign_in(driver, adb, "host:1", "hasan428483@gmail.com", "pw",
                        "SECRET", sleep=lambda _s: None)

    assert verdict == g.RESULT_ROBOT_CHECK
    assert not driver.taps, "there is nothing on a captcha worth tapping"


DEVICE_VERIFICATION = (
    "verifying your phone number to help keep your account safe, google "
    "wants to make sure that it’s really you trying to sign in verifying "
    "your phone number to help keep your account safe, google wants to "
    "make sure that it’s really you trying to sign in mdr147391@gmail.com "
    "google needs to verify your device and phone number for security "
    "reasons. this number will be stored and used only for security "
    "purposes. try another way")


def test_a_device_verification_gate_is_named_rather_than_left_unknown():
    """`mdr147391@gmail.com`, 2026-08-25 (GeeLark/Android 16): Google asking
    for a phone number is a different gate from the robot check -- no
    captcha, nothing to solve -- but just as unautomatable, and it used to
    fall through to `unknown_screen` and read as a mystery bug."""
    assert (g.classify_google_screen(DEVICE_VERIFICATION)
           == g.SCREEN_DEVICE_VERIFICATION)


def test_a_device_verification_gate_is_not_mistaken_for_a_robot_check():
    """Different gates, both dead ends, but conflating them would hide which
    one an account actually hit."""
    assert (g.classify_google_screen(DEVICE_VERIFICATION)
           != g.SCREEN_ROBOT_CHECK)


def test_a_device_verification_gate_stops_the_run_with_its_own_verdict():
    driver, adb = _StubDriver(DEVICE_VERIFICATION), _StubAdb()

    verdict = g.sign_in(driver, adb, "host:1", "mdr147391@gmail.com", "pw",
                        "SECRET", sleep=lambda _s: None)

    assert verdict == g.RESULT_DEVICE_VERIFICATION
    assert not driver.taps, "there is no button here that leads anywhere"


# The Play Store home -- the screen a phone that already carries a Google
# account opens on. Assembled from the module's own markers rather than read off
# a phone, so it proves the branch, not the wording.
PLAY_HOME = ("google play games apps movies books search for apps & games "
             "top charts for you")

# Read off `test claude ML PROXY (4)` live, 2026-08-24, right after a real
# sign-in (zaxko530@gmail.com) had genuinely succeeded: password accepted,
# Terms agreed, this screen on top. Misclassified as SCREEN_PASSWORD anyway
# -- that screen's own bare "welcome" marker matched "Welcome to Play" and
# was checked first -- so a completed sign-in was reported RESULT_STUCK on a
# password field that was never there ("no input field on screen").
PLAY_HOME_WELCOME_VARIANT = (
    "welcome to play quickly find new apps to love view view sponsored "
    "suggested for you more options hinge dating app: match & date dating "
    "star rating: 3,6 trip.com: flight, hotel, train travel & local flights "
    "accommodation star rating: 4,6 show notifications and offers. signed "
    "in as zaxko530@gmail.com account and settings. for you top charts "
    "children categories games apps search books")


def test_a_welcome_to_play_variant_is_not_read_as_the_password_screen():
    """The exact real regression: this variant's own "welcome" heading must
    not win over its own, more specific Play Store markers."""
    assert (g.classify_google_screen(PLAY_HOME_WELCOME_VARIANT)
           == g.SCREEN_PLAY_HOME)


def test_a_completed_sign_in_on_this_variant_is_reported_signed_in():
    class Adb(_StubAdb):
        def __init__(self):
            super().__init__()
            self.account_checks = 0

        def run_command(self, command):
            self.commands.append(command)
            if "dumpsys account" in command:
                self.account_checks += 1
                # Empty on the very first check (sign_in()'s own early
                # "already on the phone?" shortcut, a different, already-
                # correct code path this test is not about) -- present
                # from the second check on, matching the real run: the
                # account had only just finished being added when the flow
                # first reaches this screen mid-session.
                if self.account_checks == 1:
                    return "Accounts: 0"
                return ("Accounts: 1\n"
                        "  Account {name=zaxko530@gmail.com, "
                        "type=com.google}\n")
            return ""

    driver, adb = _StubDriver(PLAY_HOME_WELCOME_VARIANT), Adb()

    verdict = g.sign_in(driver, adb, "host:1", "zaxko530@gmail.com", "pw",
                        "SECRET", sleep=lambda _s: None)

    assert verdict == g.RESULT_SIGNED_IN


def test_a_second_mailbox_goes_on_through_androids_own_add_account_wizard():
    """The Play Store's `Sign in` button only exists while the phone carries no
    Google account, so a phone that already has one opens on its home screen
    and there is nothing to press. That read as `stuck`, which made "this phone
    is spent" look like a fleet fault rather than one missing intent."""
    class Adb(_StubAdb):
        def run_command(self, command):
            self.commands.append(command)
            if "dumpsys account" in command:
                return ("Accounts: 1\n"
                        "  Account {name=someone.else@gmail.com, "
                        "type=com.google}\n")
            return ""

    driver, adb = _StubDriver(PLAY_HOME), Adb()

    verdict = g.sign_in(driver, adb, "host:1", "wanted@gmail.com", "pw",
                        "SECRET", sleep=lambda _s: None)

    adds = [c for c in adb.commands if "ADD_ACCOUNT_SETTINGS" in c]
    assert adds, "never opened the wizard, so the second mailbox cannot go on"
    assert all("account_types com.google" in c for c in adds), \
        "without the type pinned the wizard stops on a picker"
    # The stub shows the same home screen forever, so it must still give up.
    assert verdict == g.RESULT_STUCK
    assert len(adds) == g.MAX_ADD_ACCOUNT_STARTS, \
        "an unbounded retry would spend the phone's whole life on it"


def test_the_wanted_mailbox_already_being_on_the_phone_is_not_an_add():
    """`already_signed_in` is the cheap path -- roughly nine minutes of cold
    sign-in saved -- and it must not be spent re-adding what is there."""
    class Adb(_StubAdb):
        def run_command(self, command):
            self.commands.append(command)
            if "dumpsys account" in command:
                return ("Accounts: 1\n"
                        "  Account {name=wanted@gmail.com, type=com.google}\n")
            return ""

    driver, adb = _StubDriver(PLAY_HOME), Adb()

    verdict = g.sign_in(driver, adb, "host:1", "wanted@gmail.com", "pw",
                        "SECRET", sleep=lambda _s: None)

    assert verdict == g.RESULT_ALREADY
    assert not [c for c in adb.commands if "ADD_ACCOUNT_SETTINGS" in c]


# The password form as `Blank caio 2` dumped it on 2026-08-18, one pass after
# `NEXT` was tapped: the password still in the field, and Google's own spinner
# drawn over the form. Google was checking; the form had not failed.
PASSWORD_CHECKING = ("welcome loading indeterminate, loading welcome "
                     "cicireynaamelia@gmail.com akunbaru123@ show password "
                     "show password forgot password? next")


def test_a_password_being_checked_is_waited_out_rather_than_retyped(monkeypatch):
    """The run spent its whole repeat budget retyping a password that was
    already submitted, and reported `stuck` 356s in with the sign-in fine. The
    code screen has had this wait since 2026-08-17; the password form needs the
    same one -- so a form that settles must be reached *without* a second
    typing."""
    monkeypatch.setattr(g.totp, "fresh_code", lambda secret: ("123456", 30))

    class SettlingDriver(_StubDriver):
        """Spins over the password form three times, then moves on."""

        def __init__(self):
            super().__init__(PASSWORD_CHECKING)
            self.fills, self.reads = 0, 0

        def read_screen(self):
            self.reads += 1
            return PASSWORD_CHECKING if self.reads <= 4 else TWO_FA_BOTH

        def input_hints(self):
            return (["enter your password"] if self.reads <= 4
                    else ["enter code totppin"])

        def input_values(self):
            return ["akunbaru123@"] if self.fills else []

        def fill(self, hints, value, what, **kw):
            if "code" in hints or "totppin" in hints:
                return True
            self.fills += 1
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = SettlingDriver(), _StubAdb()
    slept = []

    g.sign_in(driver, adb, "host:1", "cicireynaamelia@gmail.com",
              "akunbaru123@", "SECRET", sleep=slept.append)

    assert driver.fills == 1, (
        f"typed the password {driver.fills} times; a form Google is still "
        f"checking has to be waited out, not filled again")
    assert g.PASSWORD_WAIT_SECONDS in slept, "never actually waited"
    assert sum(slept) < 15 * 60, "a wait must not outlive the phone"


def test_a_password_that_never_answers_still_gives_up():
    """The wait is a budget, not a hang: a form that spins forever has to end
    the run rather than sit on the phone until it dies."""
    class StuckDriver(_StubDriver):
        def __init__(self):
            super().__init__(PASSWORD_CHECKING)
            self.fills = 0

        def input_hints(self):
            return ["enter your password"]

        def input_values(self):
            return ["akunbaru123@"] if self.fills else []

        def fill(self, hints, value, what, **kw):
            self.fills += 1
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = StuckDriver(), _StubAdb()
    slept = []

    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "akunbaru123@",
                        "SECRET", sleep=slept.append)

    assert verdict == g.RESULT_STUCK
    assert sum(slept) < 15 * 60, "spent longer than the phone lives"


def test_a_cleared_password_field_is_retyped_rather_than_waited_on():
    """Google clearing the field is it asking again, not still thinking -- and
    waiting through that would spend the phone's life on a form that wants
    input."""
    class ClearingDriver(_StubDriver):
        def __init__(self):
            super().__init__(PASSWORD_CHECKING)
            self.fills = 0

        def input_hints(self):
            return ["enter your password"]

        def input_values(self):
            return []          # never holds what we typed

        def fill(self, hints, value, what, **kw):
            self.fills += 1
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = ClearingDriver(), _StubAdb()
    g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
              sleep=lambda _s: None)

    assert driver.fills > 1, "a field Google emptied has to be filled again"


def test_an_unnamed_last_screen_asks_the_phone_before_calling_it_a_failure():
    """`Blank caio 2` ended a 750-second sign-in on Google's own confirmation --
    "signed in as cicireynaamelia@gmail.com" -- and reported `unknown_screen`,
    which spent a launch and read as the mailbox being unusable. The screen at
    the end of this chain is the one part we cannot enumerate, so `dumpsys` has
    the last word."""
    class Adb(_StubAdb):
        """Empty at the start of the run -- the account lands during it."""

        def __init__(self):
            super().__init__()
            self.account_reads = 0

        def run_command(self, command):
            self.commands.append(command)
            if "dumpsys account" in command:
                self.account_reads += 1
                if self.account_reads == 1:
                    return "Accounts: 0\n"
                return ("Accounts: 1\n"
                        "  Account {name=cicireynaamelia@gmail.com, "
                        "type=com.google}\n")
            return ""

    driver, adb = _StubDriver("signed in as cicireynaamelia@gmail.com"), Adb()

    verdict = g.sign_in(driver, adb, "host:1", "cicireynaamelia@gmail.com",
                        "pw", "SECRET", sleep=lambda _s: None)

    assert verdict == g.RESULT_SIGNED_IN


def test_an_unnamed_screen_with_no_account_on_the_phone_is_still_a_failure():
    """The check has to be the phone's answer, not a shortcut that turns every
    screen nobody has named into a success."""
    driver, adb = _StubDriver("a screen nobody here has ever seen"), _StubAdb()

    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
                        sleep=lambda _s: None)

    assert verdict == g.RESULT_UNKNOWN_SCREEN


# --- adaptive waiting -------------------------------------------------------

class _Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Screens:
    """Hands back a scripted sequence of screen reads, then repeats the last."""

    def __init__(self, *screens):
        self.screens = list(screens)
        self.reads = 0

    def read_screen(self):
        self.reads += 1
        i = min(self.reads - 1, len(self.screens) - 1)
        return self.screens[i]


def test_a_screen_that_moves_ends_the_wait_early():
    """The whole point: a form that settles in one look no longer costs ten
    seconds, and this chain has about a dozen such waits."""
    clock = _Clock()
    driver = _Screens(EMAIL, PASSWORD_CHECKING)

    out = g.settle(driver, EMAIL, 10, sleep=clock.sleep, clock=clock)

    assert out == PASSWORD_CHECKING
    assert clock.now < 10, "waited the full budget on a screen that had moved"


def test_a_screen_that_does_not_move_is_waited_out_in_full():
    """Early exit only on change, so the caller's repeat guard counts exactly
    what it counted before -- a stuck run must not look different."""
    clock = _Clock()
    driver = _Screens(EMAIL)

    out = g.settle(driver, EMAIL, 10, sleep=clock.sleep, clock=clock)

    assert out == EMAIL
    assert clock.now >= 10


def test_a_half_drawn_screen_does_not_end_the_wait():
    """Google's forms redraw in stages and a tap into one mid-draw lands on
    nothing or on the wrong control -- the fixed sleeps existed for this."""
    clock = _Clock()
    driver = _Screens("skip next", "skip next", PASSWORD_CHECKING)

    out = g.settle(driver, EMAIL, 10, sleep=clock.sleep, clock=clock)

    assert g.classify_google_screen("skip next") == g.SCREEN_LOADING
    assert out == PASSWORD_CHECKING, "settled on the spinner instead of the form"


def test_a_failed_dump_never_ends_the_wait():
    """An empty read is a dump that failed, not a screen that changed."""
    clock = _Clock()
    driver = _Screens("", "", "")

    out = g.settle(driver, EMAIL, 10, sleep=clock.sleep, clock=clock)

    assert out == EMAIL
    assert clock.now >= 10


def test_whitespace_and_case_are_not_a_change():
    """The same screen dumps with different spacing depending on how far a
    layout has settled; treating that as movement would defeat the guard."""
    clock = _Clock()
    driver = _Screens(EMAIL.upper().replace(" ", "  "))

    out = g.settle(driver, EMAIL, 6, sleep=clock.sleep, clock=clock)

    assert clock.now >= 6


def test_the_wait_is_bounded_even_when_the_clock_never_moves():
    """The tests inject a sleep that does not sleep. Without the look bound
    this spins on the driver for a wall-clock second."""
    driver = _Screens(EMAIL)

    g.settle(driver, EMAIL, 10, sleep=lambda _s: None, clock=lambda: 0.0)

    assert driver.reads <= int(10 / g.POLL_SECONDS) + 1


def test_a_driver_that_raises_hands_back_what_it_had():
    class Broken:
        def read_screen(self):
            raise RuntimeError("adb died")

    assert g.settle(Broken(), EMAIL, 6, sleep=lambda _s: None,
                    clock=lambda: 0.0) == EMAIL


def test_a_zero_budget_does_not_look_at_all():
    driver = _Screens(EMAIL)

    assert g.settle(driver, EMAIL, 0) == EMAIL
    assert driver.reads == 0


# --- the email/password submit no longer dismisses the keyboard first --------
class _TapScriptDriver:
    """Scripted tap_label results, and a dismiss counter -- enough to prove
    `_submit_after_typing` tries the direct tap before ever touching the
    keyboard."""

    def __init__(self, tap_results=None):
        self.dismissals = 0
        self.taps = []
        self._tap_results = list(tap_results) if tap_results is not None else None

    def tap_label(self, labels):
        self.taps.append(labels)
        if self._tap_results is not None:
            return self._tap_results.pop(0) if self._tap_results else True
        return True

    def dismiss_keyboard(self):
        self.dismissals += 1


def test_submit_after_typing_taps_directly_when_the_button_is_reachable():
    driver = _TapScriptDriver()
    assert g._submit_after_typing(driver, g._NEXT) is True
    assert driver.dismissals == 0
    assert driver.taps == [g._NEXT]


def test_submit_after_typing_falls_back_to_dismissing_if_the_first_tap_misses():
    """The case this exists for on some devices: BACK (what dismiss_keyboard
    sends) is not reliably consumed by the IME, and steps the sign-in flow
    itself back a screen -- so this must never dismiss unless the direct tap
    genuinely could not find the button."""
    driver = _TapScriptDriver(tap_results=[False, True])
    assert g._submit_after_typing(driver, g._NEXT) is True
    assert driver.dismissals == 1
    assert driver.taps == [g._NEXT, g._NEXT]


EMAIL_LOADING = EMAIL + " loading indeterminate, loading"


def test_an_email_screen_still_loading_is_waited_out_not_refilled():
    """`unnikuttan114121@gmail.com`, 2026-08-23: `_still_drawing`'s loading
    check only fires for a *bare* "just a moment"-style screen (<=300
    chars), so it never caught this -- a full email form, correctly filled,
    with a small loading indicator drawn over a disabled Next. Classified as
    plain `SCREEN_EMAIL`, that got re-filled and re-tapped on every pass and
    hit `MAX_REPEATS` (4 tries) in well under a minute, while the real Play
    Store sign-in can sit here past a minute."""
    class StillLoadingDriver(_StubDriver):
        def __init__(self):
            super().__init__(EMAIL_LOADING)
            self.fills = 0

        def input_hints(self):
            return ["email or phone"]

        def input_values(self):
            return ["a@gmail.com"] if self.fills else []

        def fill(self, hints, value, what, **kw):
            self.fills += 1
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = StillLoadingDriver(), _StubAdb()
    slept = []

    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
                        sleep=slept.append)

    # Bounded (the stub shows the same loading screen forever), but only
    # after the loading budget, not the much stingier repeat guard: at
    # MAX_REPEATS=4 with no wait at all this would have given up in a
    # handful of reads, well under LOADING_WAIT_SECONDS of sleep.
    assert verdict == g.RESULT_STUCK
    assert driver.fills == 1, (
        f"typed the email {driver.fills} times; a form that is still "
        f"loading has to be waited out, not filled again")
    assert sum(slept) >= (g.MAX_LOADING_WAITS - 1) * g.LOADING_WAIT_SECONDS
    assert sum(slept) < 15 * 60, "a wait must not outlive the phone"


def test_an_email_screen_that_actually_stops_advancing_still_gives_up_fast():
    """The fix must not turn every stalled email screen into a two-minute
    wait -- only one that both still shows the address AND still says it is
    loading. No loading text here, so the ordinary repeat guard still
    applies."""
    class PlainEmailDriver(_StubDriver):
        def __init__(self):
            super().__init__(EMAIL)

        def fill(self, hints, value, what, **kw):
            return True

        def dismiss_keyboard(self):
            pass

    driver, adb = PlainEmailDriver(), _StubAdb()

    verdict = g.sign_in(driver, adb, "host:1", "a@gmail.com", "pw", "SECRET",
                        sleep=lambda _s: None)

    assert verdict == g.RESULT_STUCK


def test_sign_in_with_retries_closes_the_app_and_tries_again():
    """`oukroaicha@gmail.com`, 2026-08-23: tapping "Get a verification code
    from the Google Authenticator app" on the 2-step chooser highlighted the
    row blue and went nowhere -- an app-state glitch closing Play
    Store/GMS and starting over is the same fix already proven for the
    equivalent install-side stalls."""
    class BlankDriver(_StubDriver):
        def __init__(self):
            super().__init__("")

    class FlakyThenSignedIn(_StubAdb):
        def __init__(self):
            super().__init__()
            self.force_stops = 0

        def run_command(self, command):
            self.commands.append(command)
            if "force-stop" in command:
                self.force_stops += 1
                return ""
            if "dumpsys account" in command:
                if self.force_stops >= 4:
                    return "Account {name=a@gmail.com, type=com.google}"
                return "Accounts: 0"
            return ""

    driver, adb = BlankDriver(), FlakyThenSignedIn()

    verdict = g.sign_in_with_retries(driver, adb, "host:1", "a@gmail.com",
                                     "pw", "SECRET", sleep=lambda _s: None,
                                     max_attempts=5)

    assert verdict == g.RESULT_ALREADY
    assert adb.force_stops == 4, (
        "should close Play Store AND GMS between each of the two failed "
        "attempts (2 apps x 2 retries = 4)")


def test_sign_in_with_retries_does_not_retry_a_robot_check():
    """Reopening Play Store does not make Google re-verify an account any
    faster -- retrying a robot check would just spend another full walk of
    the chain finding the same wall again."""
    driver = _StubDriver(("verify it's you unnikuttan114121@gmail.com "
                         "confirm you're not a robot next try another way"))
    adb = _StubAdb()

    verdict = g.sign_in_with_retries(driver, adb, "host:1", "a@gmail.com",
                                     "pw", "SECRET", sleep=lambda _s: None,
                                     max_attempts=5)

    assert verdict == g.RESULT_ROBOT_CHECK
    assert not any("force-stop" in c for c in adb.commands), \
        "retried an account-shaped result"
