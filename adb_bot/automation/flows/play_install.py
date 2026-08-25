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
#
# `Next` is the Play Store's own first-run wizard step that can appear right
# after a fresh Google account is added to the device -- distinct from the
# per-app "complete account setup" sheet above. Missing it left the loop
# looking at a screen with nothing but a `NEXT` button for the full 240s
# timeout (`lucas18anosff@gmail.com`, 2026-08-23).
_DISMISS_LABELS = ("No thanks", "NO THANKS", "Skip", "SKIP", "Not now",
                   "NOT NOW", "Accept", "ACCEPT", "Continue", "CONTINUE",
                   "Got it", "GOT IT", "Next", "NEXT")

# What the listing says while it is working. Seeing any of these means wait
# rather than tap again -- a second tap on a downloading listing cancels it.
#
# Whole words, and no bare "%": a Play Store listing is pages of ratings,
# reviews and "data safety" text, and a lone percent sign somewhere in all that
# made Gmail's listing read as a download in progress. It sat there four
# minutes with an `Install` button on screen the whole time (2026-08-17).
_WORKING_MARKERS = ("pending", "downloading", "installing", "verifying",
                    "waiting for download")

# A listing that still offers a tappable `Install` has not started, whatever
# else is written on the page. This is the check that decides, because it looks
# at what is *actionable* rather than at prose.
_INSTALL_BUTTON_LABELS = {"install", "get"}

# Google re-challenging the account mid-session, not a per-app consent sheet.
# Its only button is `Next`, and `Next` is already in `_DISMISS_LABELS` for
# the first-run wizard case -- so without this check it looked exactly like a
# dismissable prompt. Tapping Next here lands on a password/2FA form this
# function cannot fill, and the next `open_listing()` call throws that away
# and lands back on this same "Verify it's you" screen -- an unproductive
# loop that burned 13 dismiss-cycles / ~260s on `unnikuttan114121@gmail.com`
# (2026-08-23) trying to install Gmail as the *second* app that session,
# before timing out as an uninformative `no_install_button`. Checked before
# the dismiss-tap fallback so it is never mistaken for one.
_REAUTH_MARKERS = ("verify it's you", "please sign in again to continue")
RESULT_REAUTH_REQUIRED = "reauth_required"

# A small modal, not the full-page reauth challenge above: "Error --
# Authentication is required. You need to sign in to your Google Account."
# with a single `OK`. Seen mid-download on `oukroaicha@gmail.com`'s Gmail
# install (2026-08-23) -- Google's session lapsed partway through, not at
# the start. `OK` only dismisses the dialog, it does not re-authenticate,
# so tapping it (it would otherwise match `_DISMISS_LABELS` down the line
# were "OK" ever added there) just reopens the same lapsed session and
# gets the same dialog back. Closing the Play Store and starting over is
# what actually clears it -- that's `install_with_retries`' job, not this
# function's; this only has to recognise it and stop quickly rather than
# spend the full 240s finding out "OK" leads nowhere.
_AUTH_ERROR_MARKERS = ("authentication is required",
                       "you need to sign in to your google account")
RESULT_AUTH_ERROR = "auth_error"


def offers_install(labels) -> bool:
    return any(str(label).strip().lower() in _INSTALL_BUTTON_LABELS
               for label in (labels or ()))


def packages_named(out: str) -> set:
    """The package names in `pm list packages` output, exactly."""
    return {line.split(":", 1)[1].strip()
            for line in (out or "").splitlines()
            if line.startswith("package:") and ":" in line}


def is_installed(adb_client, target: str, package: str,
                 attempts: int = 3, sleep=time.sleep) -> bool:
    """The only trustworthy answer to "is it on the phone".

    Exact names, never a substring. `pm list packages com.google.android.gm`
    matches **com.google.android.gms** -- Google Play Services, which is on
    every phone -- so a substring test says Gmail is installed on a phone that
    has never had it. That answer sent three launches looking for a mail app
    that was not there (2026-08-17).

    **An empty answer is ambiguous and must not be read as "no".** On a phone
    that has just booted, the shell answers before the package manager does,
    and `pm list packages <name>` comes back empty for an app that is sitting
    right there. So an empty result is checked against the *unfiltered* list:
    if that is empty too, it is the package manager that is missing, not the
    package. Believing the first answer sent Geelark phones to the Play Store
    to install an Instagram they already had, at 100-200 seconds a time out of
    a phone that lives about fifteen minutes.
    """
    for attempt in range(1, attempts + 1):
        out = adb_client.run_command(
            f"adb -s {target} shell pm list packages {package}") or ""
        if package in packages_named(out):
            return True
        everything = adb_client.run_command(
            f"adb -s {target} shell pm list packages") or ""
        if packages_named(everything):
            return False        # the package manager answered; it is not here
        if attempt < attempts:
            sleep(5)
    return False


def open_listing(adb_client, target: str, package: str) -> None:
    """Open the app's Play Store page directly."""
    adb_client.run_command(
        f"adb -s {target} shell am start -a android.intent.action.VIEW "
        f"-d 'market://details?id={package}'")


# The Play Store having lost the network. Its "Try again" is not a clickable
# node in the dump -- the screen reports zero clickable labels -- so there is
# nothing to tap, and the way back is to open the listing again. Instagram was
# talking to its own servers happily either side of this on 2026-08-17, so it
# is the Play Store's connection, not the phone's.
_OFFLINE_MARKERS = ("no internet connection", "check your connection",
                    "you're offline",
                    # A queued/paused download, not a missing button.
                    # Confirmed live 2026-08-25 (jgjfjfcjjvjfjcncncg@gmail.com,
                    # GeeLark/Android 16): the listing read "Gmail Waiting for
                    # connection... Download will begin once restored" on
                    # every one of 5 attempts (~21 minutes) with no Install/Get
                    # button ever on screen -- correctly a connectivity
                    # problem, but read as `no_install_button` since this
                    # specific Play Store phrasing wasn't in the marker list.
                    "waiting for connection")

RESULT_OFFLINE = "no_network"
RESULT_INSTALLED = "installed"
RESULT_ALREADY = "already_installed"
RESULT_NO_BUTTON = "no_install_button"
RESULT_TIMEOUT = "timed_out"

MAX_TAPS = 4

# Enough to ride out a hiccup, few enough that a Play Store which simply
# cannot reach Google gives the phone's remaining life back.
MAX_OFFLINE = 4

# How many unchanged "pending…" reads before cancelling and asking again.
# About a minute -- long enough that a queue which is merely busy gets to
# clear itself, short enough to try twice inside one phone.
PENDING_BEFORE_RETRY = 6


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
    offline = 0
    pending = 0
    while time.monotonic() < deadline:
        if is_installed(adb_client, target, package):
            log("info", "%s installed", package)
            return RESULT_INSTALLED

        text = (driver.read_screen() or "").lower()

        if any(marker in text for marker in _OFFLINE_MARKERS):
            offline += 1
            if offline > MAX_OFFLINE:
                log("warning", "the Play Store reported no network %d times",
                    offline - 1)
                return RESULT_OFFLINE
            log("info", "the Play Store has no network; reopening the listing "
                        "(%d/%d)", offline, MAX_OFFLINE)
            sleep(10)
            open_listing(adb_client, target, package)
            sleep(8)
            continue

        labels = (driver.clickable_labels()
                  if hasattr(driver, "clickable_labels") else [])
        # Google's "complete account setup" sheet reads "...to continue
        # **installing** apps on Google Play" -- an unrelated sentence that
        # still contains the word a real progress screen uses. That false
        # match made the loop wait out a `Continue` button for 565 seconds
        # (`lucas18anosff@gmail.com`, 2026-08-22) instead of tapping it: a
        # real download in progress never offers a dismiss button, so seeing
        # one is proof the "working" marker is a false hit, not real progress.
        has_dismiss_button = any(str(label).strip() in _DISMISS_LABELS
                                 for label in (labels or ()))
        if (says_any(text, _WORKING_MARKERS) and not offers_install(labels)
                and not has_dismiss_button):
            # "pending…" is the Play Store's queue, not a download. Gmail sat
            # in it for a full four minutes without ever starting
            # (2026-08-17). Waiting longer does not clear it; cancelling and
            # asking again does.
            if "pending" in text:
                pending += 1
                if pending >= PENDING_BEFORE_RETRY and taps < MAX_TAPS:
                    log("info", "stuck in the download queue; cancelling and "
                                "asking again")
                    pending = 0
                    driver.tap_label(("Cancel", "CANCEL"))
                    sleep(6)
                    open_listing(adb_client, target, package)
                    sleep(8)
                    continue
            else:
                pending = 0
            log("info", "still working; waiting")
            sleep(10)
            continue
        pending = 0

        if says_any(text, _OPEN_TEXT_WORDS) and taps:
            # `Open` after we asked for the install: give the package manager a
            # moment to catch up rather than believing the button.
            sleep(8)
            continue

        # require_clickable=False: on `oukroaicha@gmail.com`'s Gmail listing
        # (2026-08-23) the button's label sat in `content-desc` on a node
        # marked `clickable="false"`, with no clickable ancestor within the
        # usual 6-level walk -- yet the screenshot showed a completely
        # normal, tappable blue Install button. The strict rule is right
        # when tapping the wrong thing is expensive (a phone number field);
        # here the labeled node's own bounds ARE the button visually, so a
        # tap Android delivers there is the safe direction to guess, not
        # the ancestor search coming up empty for six straight polls of a
        # button that plainly is on screen.
        if taps < MAX_TAPS and driver.tap_label(_INSTALL_LABELS,
                                                require_clickable=False):
            taps += 1
            log("info", "tapped install (%d/%d)", taps, MAX_TAPS)
            sleep(12)
            continue

        if says_any(text, _REAUTH_MARKERS):
            log("warning", "Google wants the account re-verified before "
                           "this install can continue -- not automated, "
                           "stopping rather than looping on it")
            return RESULT_REAUTH_REQUIRED

        if says_any(text, _AUTH_ERROR_MARKERS):
            log("warning", "Google's session lapsed mid-install ('OK' does "
                           "not fix this) -- stopping rather than looping "
                           "on a dialog tapping OK cannot clear")
            return RESULT_AUTH_ERROR

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


# Results worth a fresh Play Store rather than accepting as final -- a listing
# that never rendered anything tappable, or a session that lapsed mid-install.
# `RESULT_OFFLINE` is deliberately absent: `install` already retries that
# itself (`MAX_OFFLINE`) before giving up, so seeing it out here means the
# network problem outlasted that retry too, and closing the app again is not
# going to reach further than the network does.
_RETRYABLE_RESULTS = (RESULT_NO_BUTTON, RESULT_TIMEOUT, RESULT_AUTH_ERROR)

DEFAULT_INSTALL_RETRIES = 5


def install_with_retries(driver, adb_client, target: str, package: str,
                         logger=None, max_attempts: int = DEFAULT_INSTALL_RETRIES,
                         timeout: int = 240, sleep=time.sleep) -> str:
    """`install`, closing the Play Store and starting over on a retryable
    result, up to `max_attempts` times total.

    Force-stopping and reopening is the fix for exactly one shape of
    failure: state stuck in the *app*, not in the account or the network.
    `RESULT_REAUTH_REQUIRED` and `RESULT_ROBOT_CHECK`-shaped account
    problems are not in `_RETRYABLE_RESULTS` for that reason -- reopening
    Play Store does not make Google re-verify the account any faster, it
    just spends another `timeout` seconds finding the same wall again.
    """
    verdict = RESULT_NO_BUTTON
    for attempt in range(1, max_attempts + 1):
        verdict = install(driver, adb_client, target, package, logger=logger,
                          timeout=timeout, sleep=sleep)
        if verdict in (RESULT_INSTALLED, RESULT_ALREADY):
            return verdict
        if verdict not in _RETRYABLE_RESULTS:
            return verdict
        if logger is not None:
            logger.info("play_install: attempt %d/%d for %s ended %s -- "
                        "closing the Play Store and trying again",
                        attempt, max_attempts, package, verdict)
        if attempt < max_attempts:
            adb_client.run_command(f"adb -s {target} shell am force-stop "
                                   f"{PLAY_PACKAGE}")
            sleep(6)
    return verdict
