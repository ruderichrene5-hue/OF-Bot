"""Put a Google account on a cloud phone, through the Play Store.

The email half of signup needs the mailbox readable on the device, and that
means the account has to be *on* the device. Two entry points exist and only
one works here:

* **Gmail's own `Add an email address` → `Google`** fails at the Terms step,
  every time, on two different phones including one with no Google account at
  all -- "Sorry, something went wrong there", and `dumpsys account` stays empty.
* **The Play Store's `Sign in`** walks the same `MinuteMaidActivity` screens
  with the same credentials and completes. Proven 2026-08-13 on the
  `gmail test` profile: `dumpsys account` then lists the address and Gmail
  opens on that mailbox.

The entry point is the entire difference. Do not re-derive this as "Google
refuses to provision on these phones" -- that conclusion was drawn once and was
wrong.

Every marker below was read off that run. What it cost to learn:

* **The account persists on the MLX profile**, so this is a one-off per phone
  and the next launch starts signed in.
* **A code is only valid inside its own thirty-second window** and each CLI
  round trip to these phones costs ~20s. Driven from one process the same
  secret was accepted every time; driven a command at a time it was rejected
  every time, and Google's error for a stale code is the same generic
  "something went wrong" it gives for everything else.
* **`monkey` starts nothing on these phones** -- three runs confirmed -- so
  apps are started by explicit intent.
* **Google's 2FA chooser reports stale bounds on the first dump**: the first
  tap only settles the view. Dump again and tap what the second dump says.
* **The chooser row is not tapped by its label.** The clickable node is the
  row (`View`), so it is tapped by centre.
* **The password-save prompt says `NOT NOW` the first time and `NEVER`
  afterwards**, so matching one label stalls; it is dismissed by either.
* **`input text` into the TOTP field prepends a stray digit** (`056293` became
  `2056293`). The field is cleared first and read back before submitting.
* Always skip **`sign in with ease`** -- it is a lookup *by phone number*, and
  these phones cannot receive SMS.

**This module's first live run is supervised.** The screens are recorded fact;
the ordering logic over them is new.
"""

from __future__ import annotations

import re
import time

from adb_bot.automation import totp

PLAY_PACKAGE = "com.android.vending"
GMAIL_PACKAGE = "com.google.android.gm"

# --- screens ------------------------------------------------------------------
SCREEN_PLAY_SIGNIN = "play_signin"        # Play Store, signed out
SCREEN_EASE = "sign_in_with_ease"         # a lookup by phone number -- skip
SCREEN_EASE_FAILED = "phone_lookup_failed"  # that lookup, having failed anyway
SCREEN_EMAIL = "google_email"             # "Sign in" + email field
SCREEN_PASSWORD = "google_password"       # "Welcome" + password field
SCREEN_2FA_CHOOSER = "two_factor_chooser"
SCREEN_TOTP = "totp"                      # "Enter code"
SCREEN_TERMS = "google_terms"
SCREEN_SAVE_PASSWORD = "save_password"
SCREEN_PLAY_HOME = "play_home"            # signed in -- done
SCREEN_WRONG_PASSWORD = "wrong_password"
SCREEN_LOADING = "loading"
SCREEN_UNKNOWN = "unknown"

_PLAY_SIGNIN_MARKERS = (
    # Read off `Blank caio 1`, 2026-08-16. The whole screen is:
    # "options sign in to find the latest android apps, games, movies, music &
    # more sign in".
    "sign in to find the latest android apps",
    "sign in to get the most out of google play",
    "sign in to your google account",
)
_EASE_MARKERS = ("sign in with ease", "use your phone number to sign in")

# The same phone-number lookup, after it fails -- which it does on these phones,
# because they have no usable SIM. Seen on `Blank caio 1`, 2026-08-16, straight
# after the email was submitted, even though `sign in with ease` had already
# been skipped. It offers its own way out: `Sign in another way`.
#
# Matched on the specific sentence, never on the bare "something went wrong" --
# that string is Google's answer to half a dozen unrelated failures, and
# treating them as one thing is how the 2026-08-13 run concluded Google refuses
# to provision on these phones at all.
_EASE_FAILED_MARKERS = (
    "able to check for accounts connected to your phone number",
    "sign in another way",
)
_EMAIL_MARKERS = ("email or phone", "enter your email", "use your google account")
_PASSWORD_MARKERS = ("enter your password", "hi ", "welcome")
_2FA_MARKERS = (
    "2-step verification",
    "choose how you want to sign in",
    "verify it's you",
    "get a verification code from the google authenticator app",
)
# Narrow on purpose. The *chooser* offers "get a verification code from the
# Google Authenticator app", so "authenticator app" and "verification code"
# both match a screen with no code field on it -- and the flow would then type
# a code into nothing and call the run stuck. The code screen is the one that
# asks you to **enter** a code; its field hint is `enter code totppin`.
_TOTP_MARKERS = ("totppin", "enter code")
_TERMS_MARKERS = ("google terms of service", "by continuing, you agree",
                  "i agree")
_SAVE_PASSWORD_MARKERS = ("save password", "google password manager")
_PLAY_HOME_MARKERS = ("search apps & games", "search for apps & games",
                      "games apps", "for you top charts")
_WRONG_PASSWORD_MARKERS = ("wrong password", "couldn't sign you in",
                           "try again or click forgot password")

# Ordered: the specific before the general. `_PASSWORD_MARKERS` carries
# "welcome", which appears on several Google screens, so anything that can be
# named more precisely is named first.
_ORDERED = (
    (SCREEN_WRONG_PASSWORD, _WRONG_PASSWORD_MARKERS),
    (SCREEN_SAVE_PASSWORD, _SAVE_PASSWORD_MARKERS),
    (SCREEN_TOTP, _TOTP_MARKERS),
    (SCREEN_2FA_CHOOSER, _2FA_MARKERS),
    (SCREEN_TERMS, _TERMS_MARKERS),
    (SCREEN_EASE_FAILED, _EASE_FAILED_MARKERS),
    (SCREEN_EASE, _EASE_MARKERS),
    (SCREEN_EMAIL, _EMAIL_MARKERS),
    (SCREEN_PASSWORD, _PASSWORD_MARKERS),
    (SCREEN_PLAY_SIGNIN, _PLAY_SIGNIN_MARKERS),
    (SCREEN_PLAY_HOME, _PLAY_HOME_MARKERS),
)

_LOADING_MARKERS = ("just a moment", "loading", "checking info", "please wait",
                    "searching for accounts")

# Words that are never the *content* of a screen, only its furniture. A dump
# containing nothing but these was taken while the real screen was still
# drawing -- `skip next` stopped a run on 2026-08-16 that was otherwise fine.
# The signup flow learned the same lesson on its own screens.
_CHROME_ONLY_WORDS = frozenset({
    "next", "back", "skip", "loading", "ok", "continue", "done", "cancel",
    "google", "sign", "in",
})


def _still_drawing(haystack: str) -> bool:
    if any(marker in haystack for marker in _LOADING_MARKERS) and len(haystack) <= 300:
        return True
    words = [word for word in re.split(r"[^a-z]+", haystack) if word]
    return bool(words) and set(words) <= _CHROME_ONLY_WORDS


def classify_google_screen(text: str | None) -> str:
    """Name a screen in the Play Store sign-in chain."""
    if not text:
        return SCREEN_UNKNOWN
    haystack = text.lower()
    for kind, markers in _ORDERED:
        if any(marker in haystack for marker in markers):
            return kind
    # Deliberately last: a screen with any real content on it must never be
    # dismissed as "still loading".
    if _still_drawing(haystack):
        return SCREEN_LOADING
    return SCREEN_UNKNOWN


RESULT_SIGNED_IN = "signed_in"
RESULT_ALREADY = "already_signed_in"
RESULT_WRONG_PASSWORD = "wrong_password"
RESULT_STUCK = "stuck"
RESULT_UNKNOWN_SCREEN = "unknown_screen"

MAX_STEPS = 40
MAX_REPEATS = 4

# Google's sign-in is slow through these proxies -- every request leaves via a
# German mobile exit. `Blank caio 2` sat on `checking info…` for a full minute
# and was given up on at eight waits, which is a flow that quit on a screen that
# was still working. Twenty waits of eight seconds is a bit over two and a half
# minutes, still comfortably inside the phone's ~15-minute life.
MAX_LOADING_WAITS = 20
LOADING_WAIT_SECONDS = 8

_SKIP = ("Skip", "SKIP", "Not now", "NOT NOW", "Never", "NEVER")
_NEXT = ("Next", "NEXT", "Continue", "CONTINUE")


def _press_enter(adb_client, target: str) -> None:
    """Submit the focused field with the keyboard's own action key.

    Keyevent 66 is `ENTER`. On Google's sign-in form this is the submit the
    button tap cannot reproduce: the tap is delivered to the right bounds and
    the form simply redraws.
    """
    adb_client.run_command(f"adb -s {target} shell input keyevent 66")


def accounts_on_device(adb_client, target: str) -> list[str]:
    """Every Google account already on the phone.

    `dumpsys account` is the only honest answer to "is the mailbox on here" --
    Gmail showing an inbox proves nothing about *which* mailbox.
    """
    out = adb_client.run_command(
        f"adb -s {target} shell dumpsys account") or ""
    return re.findall(r"Account\s*\{name=([^,]+),\s*type=com\.google", out)


def sign_in(driver, adb_client, target: str, address: str, password: str,
            totp_secret: str, logger=None, sleep=time.sleep) -> str:
    """Sign `address` into the phone through the Play Store.

    Returns one of the `RESULT_*` constants. Never force-stops anything: this
    may run on a phone with a signup in flight.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)("google_signin: " + message, *args)

    already = accounts_on_device(adb_client, target)
    if any(address.lower() == name.lower() for name in already):
        log("info", "%s is already on the phone", address)
        return RESULT_ALREADY
    if already:
        log("warning", "the phone already carries %s -- adding a second "
                       "account", already)

    adb_client.run_command(
        f"adb -s {target} shell monkey -p {PLAY_PACKAGE} "
        f"-c android.intent.category.LAUNCHER 1")
    # `monkey` is the fallback that does not need an activity name; the
    # explicit intent is what actually works on these phones.
    resolved = adb_client.run_command(
        f"adb -s {target} shell cmd package resolve-activity --brief "
        f"{PLAY_PACKAGE}") or ""
    activity = ""
    for line in resolved.splitlines():
        if "/" in line and PLAY_PACKAGE in line:
            activity = line.strip()
    if activity:
        adb_client.run_command(f"adb -s {target} shell am start -n {activity}")
    sleep(10)

    last, repeats, loading_waits = None, 0, 0
    # The failed phone lookup returns to the email form, so email -> lookup ->
    # failure -> email is a *cycle of different screens* that the repeat guard
    # cannot see. Bounded separately.
    lookup_failures = 0
    MAX_LOOKUP_FAILURES = 2
    email_submits = 0
    password_submits = 0

    for step in range(MAX_STEPS):
        text = driver.read_screen() or ""
        screen = classify_google_screen(text)
        log("info", "step %d: %s", step + 1, screen)

        if screen == SCREEN_LOADING:
            loading_waits += 1
            if loading_waits > MAX_LOADING_WAITS:
                log("warning", "still loading after %d waits", loading_waits)
                return RESULT_STUCK
            sleep(LOADING_WAIT_SECONDS)
            continue
        loading_waits = 0

        if screen == last:
            repeats += 1
            if repeats >= MAX_REPEATS:
                log("warning", "%s did not advance in %d tries", screen,
                    MAX_REPEATS)
                return RESULT_STUCK
        else:
            repeats = 0
        last = screen

        if screen == SCREEN_PLAY_HOME:
            # Believe `dumpsys`, not the screen: the Play Store home renders
            # the same whether or not the account we wanted went on.
            names = accounts_on_device(adb_client, target)
            if any(address.lower() == name.lower() for name in names):
                log("info", "%s is on the phone", address)
                return RESULT_SIGNED_IN
            log("warning", "Play Store looks signed in but dumpsys says %s",
                names or "nothing")
            return RESULT_STUCK

        if screen == SCREEN_WRONG_PASSWORD:
            return RESULT_WRONG_PASSWORD

        if screen == SCREEN_UNKNOWN:
            log("warning", "unnamed screen: %s", (text or "")[:300])
            return RESULT_UNKNOWN_SCREEN

        if screen == SCREEN_PLAY_SIGNIN:
            driver.tap_label(("Sign in", "SIGN IN"))
            sleep(8)

        elif screen == SCREEN_EASE:
            # A lookup by phone number. These phones cannot receive SMS.
            if not driver.tap_label(_SKIP):
                driver.tap_label(_NEXT)
            sleep(6)

        elif screen == SCREEN_EASE_FAILED:
            # Its own escape hatch, and the only thing on the screen worth
            # tapping. Skipping the lookup earlier does not stop it running.
            lookup_failures += 1
            if lookup_failures > MAX_LOOKUP_FAILURES:
                log("warning", "Google's phone-number lookup failed %d times; "
                               "it is not going to succeed on a phone with no "
                               "usable SIM", lookup_failures)
                return RESULT_STUCK
            if not driver.tap_label(("Sign in another way",
                                     "SIGN IN ANOTHER WAY", "Try again")):
                log("warning", "no way off the failed phone-lookup screen")
                return RESULT_STUCK
            sleep(8)

        elif screen == SCREEN_EMAIL:
            # **Always fill, never "we already typed that".** Google's failed
            # phone-number lookup hands back a *fresh, empty* email form, and a
            # once-only guard here made the flow tap NEXT on an empty field
            # four times and call itself stuck (2026-08-16, `Blank caio 1`).
            # `fill` clears, types and reads back, so doing it again is safe.
            if not driver.fill(("email", "phone"), address, "google email"):
                log("warning", "the address would not stay in the field")
                return RESULT_STUCK
            email_submits += 1
            if email_submits == 1:
                driver.dismiss_keyboard()
                driver.tap_label(_NEXT)
            else:
                # The tap lands -- the dump reports the same `NEXT` bounds every
                # time and the address stays in the field -- and Google simply
                # redraws the form (`Blank caio 1`, four times running). So the
                # second attempt submits the field itself with the IME action
                # instead, **with the keyboard still up**, because that is the
                # thing the tap route cannot do.
                log("info", "tapping NEXT did not move the email screen; "
                            "submitting with the keyboard's own action")
                _press_enter(adb_client, target)
            sleep(9)

        elif screen == SCREEN_PASSWORD:
            # Same reasoning as the email screen, including the fallback.
            if not driver.fill(("password",), password, "google password"):
                log("warning", "the password would not stay in the field")
                return RESULT_STUCK
            password_submits += 1
            if password_submits == 1:
                driver.dismiss_keyboard()
                driver.tap_label(_NEXT)
            else:
                _press_enter(adb_client, target)
            sleep(10)

        elif screen == SCREEN_2FA_CHOOSER:
            # The exact-label tap does not advance -- the clickable node is the
            # whole row -- and the first dump's bounds are stale, so this reads
            # the screen again before tapping.
            sleep(2)
            driver.read_screen()
            if not driver.tap_label((
                    "Get a verification code from the Google Authenticator app",
                    "Google Authenticator", "Use your authenticator app",
                    "Try another way")):
                log("warning", "no authenticator row on the 2FA chooser")
                return RESULT_STUCK
            sleep(6)

        elif screen == SCREEN_TOTP:
            # Generated and typed inside one window, from this process.
            code, left = totp.fresh_code(totp_secret)
            log("info", "code with %ds left", left)
            if not driver.fill(("code", "totppin"), code, "2fa code"):
                log("warning", "the code did not land in the field")
                return RESULT_STUCK
            driver.dismiss_keyboard()
            driver.tap_label(_NEXT)
            sleep(10)

        elif screen == SCREEN_TERMS:
            driver.tap_label(("I agree", "I AGREE", "Accept", "ACCEPT"))
            sleep(10)

        elif screen == SCREEN_SAVE_PASSWORD:
            # "NOT NOW" the first time, "NEVER" after -- both are in `_SKIP`.
            if not driver.tap_label(_SKIP):
                driver.dismiss_keyboard()
            sleep(4)

        sleep(2)

    return RESULT_STUCK
