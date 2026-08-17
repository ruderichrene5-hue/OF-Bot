"""Install an app on one cloud phone, from the Play Store.

**Why not MLX's own installer.** `POST /mobile_profiles/app/install` exists and
takes `install_group_ids`, but a mobile profile group is the whole workspace --
`group/get_or_create` returns exactly one group and all 210 profiles are in it.
So that endpoint installs across every phone in the fleet, live model accounts
included, with no way to name one phone. An app reinstall can log an account
out, and eighteen profiles already carry a `logged out` tag. Not worth it to
save a few taps.

**The deep link, not the search box.** `market://details?id=<package>` opens the
listing directly, which turns a five-screen chain (home, search, type, results,
listing) into one screen with one button on it. Fewer screens is fewer marker
lists that can be wrong.

**`pm list packages` decides, not the screen.** The Play Store shows `Open` for
an app that is still unpacking and shows the same green button in half a dozen
states; the package manager either has the package or does not.
"""

from __future__ import annotations

import re
import time


def says_any(text: str, words) -> bool:
    """Is any of `words` on the screen as a whole word?"""
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)

PLAY_PACKAGE = "com.android.vending"

# The listing's action button, in the order we prefer to find it. `Update` is
# deliberately absent: this is for putting an app on a phone that has none.
_INSTALL_LABELS = ("Install", "INSTALL", "Get", "GET")
_OPEN_LABELS = ("Open", "OPEN", "Play")

# What counts as "the listing now offers Open" when reading the *text* of the
# screen -- which is not the same as what to tap.
#
# `Play` cannot be one of them. Every screen in the Play Store says "Google
# Play" somewhere, and matching it as a substring made `Blank caio 2` sit for
# five minutes in front of a "Try Google Play Pass" promo sheet, reading it as
# a finished install (2026-08-17). Whole words only, for the same reason.
_OPEN_TEXT_WORDS = ("open",)

# The Play Store asks about backups and email updates on a fresh account. Both
# are declined; neither blocks the install for long if missed.
_DISMISS_LABELS = ("No thanks", "NO THANKS", "Skip", "SKIP", "Not now",
                   "NOT NOW", "Accept", "ACCEPT", "Continue", "CONTINUE",
                   "Got it", "GOT IT")

# What the listing says while it is working. Seeing any of these means wait
# rather than tap again -- a second tap on a downloading listing cancels it.
_WORKING_MARKERS = ("pending", "downloading", "installing", "verifying",
                    "waiting for", "%")


def is_installed(adb_client, target: str, package: str) -> bool:
    """The only trustworthy answer to "is it on the phone"."""
    out = adb_client.run_command(
        f"adb -s {target} shell pm list packages {package}") or ""
    return package in out


def open_listing(adb_client, target: str, package: str) -> None:
    """Open the app's Play Store page directly."""
    adb_client.run_command(
        f"adb -s {target} shell am start -a android.intent.action.VIEW "
        f"-d 'market://details?id={package}'")


RESULT_INSTALLED = "installed"
RESULT_ALREADY = "already_installed"
RESULT_NO_BUTTON = "no_install_button"
RESULT_TIMEOUT = "timed_out"

MAX_TAPS = 4


def install(driver, adb_client, target: str, package: str, logger=None,
            timeout: int = 240, sleep=time.sleep) -> str:
    """Put `package` on the phone and wait until the package manager agrees."""
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)("play_install: " + message, *args)

    if is_installed(adb_client, target, package):
        log("info", "%s is already installed", package)
        return RESULT_ALREADY

    open_listing(adb_client, target, package)
    sleep(9)

    deadline = time.monotonic() + timeout
    taps = 0
    while time.monotonic() < deadline:
        if is_installed(adb_client, target, package):
            log("info", "%s installed", package)
            return RESULT_INSTALLED

        text = (driver.read_screen() or "").lower()

        if any(marker in text for marker in _WORKING_MARKERS):
            log("info", "still working; waiting")
            sleep(10)
            continue

        if says_any(text, _OPEN_TEXT_WORDS) and taps:
            # `Open` after we asked for the install: give the package manager a
            # moment to catch up rather than believing the button.
            sleep(8)
            continue

        if taps < MAX_TAPS and driver.tap_label(_INSTALL_LABELS):
            taps += 1
            log("info", "tapped install (%d/%d)", taps, MAX_TAPS)
            sleep(12)
            continue

        # Whatever is in front is not the listing -- a consent sheet, a
        # "complete account setup" prompt. Clear it and look again.
        if driver.tap_label(_DISMISS_LABELS):
            log("info", "dismissed a prompt on the way to the listing")
            sleep(6)
            open_listing(adb_client, target, package)
            sleep(8)
            continue

        if taps >= MAX_TAPS:
            log("warning", "tapped install %d times and it is still not on the "
                           "phone", taps)
            return RESULT_NO_BUTTON

        log("info", "nothing to tap yet; waiting")
        sleep(8)

    return RESULT_TIMEOUT if taps else RESULT_NO_BUTTON
