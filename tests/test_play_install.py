"""The Play Store listing, and the sheets that cover it."""

from adb_bot.automation.flows import play_install as p

PACKAGE = "com.instagram.android"

# Verbatim from `Blank caio 2`, 2026-08-17: the promo Google put over the
# listing straight after Install was tapped.
PLAY_PASS = ("try google play pass free for 1 month enjoy hundreds of games "
             "and apps, with no ads and in-app purchases. try for 1 month, "
             "then hk$29.00/month. take a look not now")

LISTING = ("instagram instagram contains ads in-app purchases average rating "
           "3.9 stars install")


def test_google_play_is_not_an_open_button():
    """The bug this file exists for: "Play" matched as a substring of "Google
    Play Pass", so a promo sheet read as a finished install and the flow waited
    five minutes in front of it."""
    assert not p.says_any(PLAY_PASS, p._OPEN_TEXT_WORDS)


def test_an_actual_open_button_is_still_recognised():
    assert p.says_any("instagram open uninstall", p._OPEN_TEXT_WORDS)


def test_open_is_matched_as_a_whole_word():
    """"Opening", "reopen" and friends are not the button."""
    assert not p.says_any("opening the store", p._OPEN_TEXT_WORDS)


def test_play_services_is_not_gmail():
    """`pm list packages com.google.android.gm` matches com.google.android.gm*S*
    -- Play Services, on every phone. Read as a substring it says Gmail is
    installed on a phone that has never had it, which is what sent three
    launches looking for a mail app that was not there."""
    play_services_only = "package:com.google.android.gms"
    assert "com.google.android.gm" not in p.packages_named(play_services_only)
    assert "com.google.android.gms" in p.packages_named(play_services_only)


def test_gmail_itself_is_still_found():
    out = "package:com.google.android.gms\npackage:com.google.android.gm\n"
    assert "com.google.android.gm" in p.packages_named(out)


class _Adb:
    """`pm list packages` answers empty until the install is let through.

    Play Services is always listed, because it always is on the phone -- a
    substring test would call every com.google.android.* package installed.
    """

    def __init__(self):
        self.installed = False
        self.commands = []

    def run_command(self, command):
        self.commands.append(command)
        if "pm list packages" in command:
            lines = ["package:com.google.android.gms"]
            if self.installed:
                lines.append(f"package:{PACKAGE}")
            return "\n".join(lines)
        return ""


class _Driver:
    def __init__(self, adb, text):
        self.adb = adb
        self.text = text
        self.dismissed = False

    def read_screen(self):
        return self.text

    def tap_label(self, labels):
        if any(label in ("Not now", "NOT NOW", "No thanks") for label in labels):
            # Clearing the sheet is what lets the download proceed.
            self.dismissed = True
            self.adb.installed = True
            return True
        return False


def test_a_sheet_over_the_listing_is_dismissed_not_waited_out():
    adb = _Adb()
    driver = _Driver(adb, PLAY_PASS)

    verdict = p.install(driver, adb, "host:1", PACKAGE, sleep=lambda _s: None)

    assert driver.dismissed, "sat in front of the promo instead of clearing it"
    assert verdict == p.RESULT_INSTALLED


def test_a_percent_sign_somewhere_on_the_page_is_not_a_download():
    """A listing is pages of ratings, reviews and data-safety text. A lone "%"
    in all that made Gmail's listing read as a download in progress, and it sat
    there four minutes with `Install` on screen the whole time."""
    listing = "gmail 3.9 stars 97% of reviewers liked this install"
    assert not p.says_any(listing, p._WORKING_MARKERS)


def test_a_real_download_still_reads_as_working():
    assert p.says_any("downloading 42", p._WORKING_MARKERS)
    assert p.says_any("installing", p._WORKING_MARKERS)


def test_a_tappable_install_means_it_has_not_started():
    assert p.offers_install(["Google LLC", "Install"])
    # "Install on more devices" stays on the page while a download runs, so it
    # must not count as the button.
    assert not p.offers_install(["Install on more devices", "Cancel"])


OFFLINE = ("something went wrong no internet connection. make sure that wi-fi "
           "or mobile data is turned on, then try again. try again navigate up")


def test_a_play_store_with_no_network_reopens_the_listing():
    """Its "Try again" is not a clickable node -- the dump reports zero
    clickable labels -- so there is nothing to tap and the listing has to be
    opened again. This burned 475 seconds doing nothing on 2026-08-17."""
    adb = _Adb()
    driver = _Driver(adb, OFFLINE)

    verdict = p.install(driver, adb, "host:1", PACKAGE, sleep=lambda _s: None)

    assert verdict == p.RESULT_OFFLINE
    opened = [c for c in adb.commands if "market://details" in c]
    assert len(opened) == p.MAX_OFFLINE + 1, "did not reopen the listing"


def test_an_app_already_on_the_phone_is_left_alone():
    """A reinstall can log an account out, and this runs on phones that have
    just been signed in."""
    adb = _Adb()
    adb.installed = True
    driver = _Driver(adb, LISTING)

    assert p.install(driver, adb, "host:1", PACKAGE,
                     sleep=lambda _s: None) == p.RESULT_ALREADY
    assert not any("am start" in c for c in adb.commands)
