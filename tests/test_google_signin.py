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


def test_searching_for_accounts_is_loading_not_a_screen_to_act_on():
    assert g.classify_google_screen(SEARCHING) == g.SCREEN_LOADING


def test_a_dump_of_pure_chrome_is_still_drawing():
    """`skip next` alone stopped a run that was otherwise fine."""
    assert g.classify_google_screen("skip next") == g.SCREEN_LOADING
    assert g.classify_google_screen("next") == g.SCREEN_LOADING


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


# The Play Store home -- the screen a phone that already carries a Google
# account opens on. Assembled from the module's own markers rather than read off
# a phone, so it proves the branch, not the wording.
PLAY_HOME = ("google play games apps movies books search for apps & games "
             "top charts for you")


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
