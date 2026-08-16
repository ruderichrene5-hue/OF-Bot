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


LAUNCHER = ("search gallery gallery play store play store home telephone "
            "telephone messaging messaging music music chrome chrome camera "
            "camera")


def test_the_home_screen_is_named_so_the_app_can_be_started_again():
    """`Blank caio 2` ended on exactly this, called it unknown, and stopped a
    run whose only problem was that the Play Store had not come up."""
    assert g.classify_google_screen(LAUNCHER) == g.SCREEN_LAUNCHER


def test_the_home_screen_is_not_mistaken_for_the_play_store_being_signed_out():
    assert g.classify_google_screen(LAUNCHER) != g.SCREEN_PLAY_SIGNIN
