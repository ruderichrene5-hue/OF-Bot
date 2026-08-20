"""Log an *existing* Instagram account into a phone.

This did not exist before. The bot could create accounts (`flows/signup.py`) and
switch between accounts already signed in on a phone
(`instagram_reel._ensure_account_state_u2`), but nothing could take a handle and
a password and sign it in. Moving the fleet to a different cloud-phone host
needs exactly that: every phone is new, so every account has to be logged in
again.

Built in the idiom of `flows/google_signin.py` -- read the screen, classify it
against ordered markers, act, repeat -- and driven by `AdbSignupDriver`, whose
`fill()` reads the field back after typing.

**That read-back is not optional here.** The first real run of this typed a
password containing `$` straight into `adb shell input text`; the *device* shell
expanded it as a variable and swallowed it plus the characters after it, so 9 of
12 characters landed and Instagram answered "the password you entered is
incorrect". Nothing about that looked like an escaping bug -- it looked like a
wrong password, which is the sort of thing that gets an account written off.
`core.adb_commands.escape_text_for_input` is what makes it safe, and comparing
the masked field's length to the password's is what proves it.

**Expect a challenge, not a feed.** Signing a known-good account in from a new
device on a new IP was answered with an emailed confirmation code, not the home
feed -- the account was never "wrong", it was unrecognised. Any migration that
assumes login lands on the feed will read a normal security check as a failure.
Reaching the code screen is therefore its own result (`RESULT_EMAIL_CODE`),
handed back for the caller's mailbox to answer rather than treated as an error.

Markers below marked "observed" were read off a real run on 2026-08-20. The rest
are anticipated and have **not** been seen; treat them as unproven until a run
records them.
"""

from __future__ import annotations

import re
import time

# --- screens ------------------------------------------------------------------
SCREEN_JOIN = "join_instagram"        # observed: "Get started" / "I already have a profile"
SCREEN_FORM = "login_form"            # observed: username + password + "Log in"
SCREEN_EMAIL_CODE = "email_code"      # observed: "Check your email" + "Enter code"
SCREEN_WRONG_PASSWORD = "wrong_password"   # observed
SCREEN_SMS_CODE = "sms_code"          # anticipated
SCREEN_TWO_FACTOR = "two_factor"      # anticipated
SCREEN_SAVE_LOGIN = "save_login"      # anticipated
SCREEN_NOTIFICATIONS = "notifications"     # anticipated
SCREEN_SUSPENDED = "suspended"        # anticipated
SCREEN_GONE = "account_gone"          # observed
SCREEN_FEED = "feed"                  # anticipated
SCREEN_LOADING = "loading"
SCREEN_UNKNOWN = "unknown"

# --- results ------------------------------------------------------------------
RESULT_LOGGED_IN = "logged_in"
RESULT_EMAIL_CODE = "email_code_required"
RESULT_SMS_CODE = "sms_code_required"
RESULT_TWO_FACTOR = "two_factor_required"
RESULT_WRONG_PASSWORD = "wrong_password"
RESULT_SUSPENDED = "suspended"
RESULT_ACCOUNT_GONE = "account_no_longer_exists"
RESULT_UNKNOWN_SCREEN = "unknown_screen"
RESULT_STUCK = "stuck"

MAX_STEPS = 25
MAX_REPEATS = 4
SETTLE_SECONDS = 6

# Instagram leaves the login form on screen while it works. Measured on a real
# login: the form was still showing six seconds after Log in was tapped, and the
# answer arrived about twenty seconds in. Treating the first re-appearance of
# the form as "the submit did not take" gave up on four perfectly good accounts
# in a row, so the form is now allowed to persist for a while before that
# conclusion is drawn.
# The Log in button moves ~230px when the keyboard closes; give the layout
# time to settle before reading its position.
KEYBOARD_SETTLE_SECONDS = 3

MAX_FORM_WAITS = 6
FORM_WAIT_SECONDS = 6

# Observed 2026-08-20. Instagram shows one of two entry screens; this is the one
# that needs a tap to reach the form.
_JOIN_MARKERS = (
    "i already have a profile",
    "join instagram",
)

# Observed. Note "create new account" appears here too -- the signup flow keys
# off that same string, which is why signup classifies this screen as its entry.
_FORM_MARKERS = (
    "username, email or mobile number",
    "forgot password?",
)

# Observed, in full: "the password you entered is incorrect. to log in, enter
# the code we sent to c*******a@gmail.com". Instagram folds a wrong password
# into the *same* screen as the email challenge, so this must be tested BEFORE
# the email-code markers or a bad password reads as a routine security check.
_WRONG_PASSWORD_MARKERS = (
    "password you entered is incorrect",
    "incorrect password",
)

# Observed: "check your email | enter the code we sent to ... | enter code |
# get a new code | continue".
_EMAIL_CODE_MARKERS = (
    "check your email",
    "enter the code we sent to",
)

_SMS_CODE_MARKERS = (            # anticipated
    "enter the code we sent to +",
    "we sent a code to your phone",
)

_TWO_FACTOR_MARKERS = (          # anticipated
    "two-factor authentication",
    "enter the code from your authentication app",
)

# Observed verbatim on a real login: "recover your account -- it looks like that
# login info is no longer connected to an account. we'll use a secure process to
# help you get back in." The credentials are fine as *data*; the account behind
# them is gone. That is a migration finding, not a login failure, and conflating
# the two would have somebody re-testing a dead account for ever.
_GONE_MARKERS = (
    "no longer connected to an account",
    "recover your account",
)

_SUSPENDED_MARKERS = (           # anticipated
    "we suspended your account",
    "your account has been disabled",
    "account suspended",
)

_SAVE_LOGIN_MARKERS = (          # anticipated
    "save your login info",
    "save login info",
)

_NOTIFICATION_MARKERS = (        # anticipated
    "turn on notifications",
    "allow notifications",
)

# Anticipated. Deliberately specific: "for you" alone matched the *email code*
# screen's own prose ("it may take a few minutes for you to get this code") and
# reported a successful login on a phone that was sitting on a challenge. The
# fleet has been bitten by exactly this shape of feed check before.
_FEED_MARKERS = (
    "what's on your mind",
    "your story",
    "suggested for you",
)

_LOADING_MARKERS = ("loading", "please wait", "just a moment")

_ORDERED = (
    (SCREEN_GONE, _GONE_MARKERS),
    (SCREEN_SUSPENDED, _SUSPENDED_MARKERS),
    (SCREEN_WRONG_PASSWORD, _WRONG_PASSWORD_MARKERS),
    (SCREEN_TWO_FACTOR, _TWO_FACTOR_MARKERS),
    (SCREEN_SMS_CODE, _SMS_CODE_MARKERS),
    (SCREEN_EMAIL_CODE, _EMAIL_CODE_MARKERS),
    (SCREEN_SAVE_LOGIN, _SAVE_LOGIN_MARKERS),
    (SCREEN_NOTIFICATIONS, _NOTIFICATION_MARKERS),
    (SCREEN_JOIN, _JOIN_MARKERS),
    (SCREEN_FORM, _FORM_MARKERS),
    (SCREEN_FEED, _FEED_MARKERS),
)

_CHROME_ONLY_WORDS = frozenset({
    "back", "help", "continue", "next", "ok", "done", "cancel", "instagram",
    "from", "meta", "loading",
})


def _still_drawing(haystack: str) -> bool:
    if any(marker in haystack for marker in _LOADING_MARKERS) and len(haystack) <= 300:
        return True
    words = [word for word in re.split(r"[^a-z']+", haystack) if word]
    return bool(words) and set(words) <= _CHROME_ONLY_WORDS


def classify_login_screen(text: str | None) -> str:
    """Name a screen in the Instagram login chain.

    Order matters: a wrong password and an email challenge share a screen, and
    the wrong-password test has to win.
    """
    haystack = (text or "").lower()
    if not haystack.strip():
        return SCREEN_UNKNOWN
    if _still_drawing(haystack):
        return SCREEN_LOADING
    for screen, markers in _ORDERED:
        if any(marker in haystack for marker in markers):
            return screen
    return SCREEN_UNKNOWN


def log_in(driver, username: str, password: str, logger=None,
           sleep=time.sleep) -> str:
    """Sign `username` in on the phone `driver` is attached to.

    Returns a `RESULT_*` constant. Reaching a code screen is a *result*, not a
    failure: the account is fine and the caller's mailbox or SMS provider is
    what answers it.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)("instagram_login: " + message, *args)

    last, repeats = None, 0
    submitted = False
    form_waits = 0

    for step in range(MAX_STEPS):
        text = driver.read_screen() or ""
        screen = classify_login_screen(text)

        # Waiting out the login form after submitting is deliberate, and is
        # bounded by its own counter below -- the generic repeat guard must not
        # cut that short, or the flow gives up while Instagram is still working.
        waiting_on_submit = submitted and screen == SCREEN_FORM

        if screen == last and not waiting_on_submit:
            repeats += 1
            if repeats >= MAX_REPEATS:
                log("warning", "stuck on %s after %s reads", screen, repeats)
                return RESULT_STUCK
        elif screen != last:
            last, repeats = screen, 0

        log("info", "step %s: %s", step, screen)

        if screen == SCREEN_FEED:
            return RESULT_LOGGED_IN
        if screen == SCREEN_WRONG_PASSWORD:
            # Before believing this, note what caused it the first time: an
            # unescaped character eaten by the device shell, not a bad password.
            log("warning", "Instagram rejected the password for %s", username)
            return RESULT_WRONG_PASSWORD
        if screen == SCREEN_SUSPENDED:
            return RESULT_SUSPENDED
        if screen == SCREEN_EMAIL_CODE:
            return RESULT_EMAIL_CODE
        if screen == SCREEN_SMS_CODE:
            return RESULT_SMS_CODE
        if screen == SCREEN_TWO_FACTOR:
            return RESULT_TWO_FACTOR

        if screen == SCREEN_LOADING:
            sleep(SETTLE_SECONDS)
            continue

        if screen == SCREEN_JOIN:
            driver.tap_label(("I already have a profile",))
            sleep(SETTLE_SECONDS)
            continue

        if screen == SCREEN_SAVE_LOGIN:
            driver.tap_label(("Not now", "Not Now"))
            sleep(SETTLE_SECONDS)
            continue

        if screen == SCREEN_NOTIFICATIONS:
            driver.tap_label(("Not now", "Not Now", "Don't allow", "Skip"))
            sleep(SETTLE_SECONDS)
            continue

        if screen == SCREEN_GONE:
            log("warning", "Instagram says this login is not connected to an "
                           "account any more")
            return RESULT_ACCOUNT_GONE

        if screen == SCREEN_FORM:
            if submitted:
                # The form stays up while Instagram works, so its presence is
                # not evidence the submit missed -- only its *persistence* is.
                # Never retype the credentials: the fields still hold them, and
                # typing again would append to what is there.
                form_waits += 1
                if form_waits <= MAX_FORM_WAITS:
                    log("info", "still on the login form (%s/%s); waiting",
                        form_waits, MAX_FORM_WAITS)
                    sleep(FORM_WAIT_SECONDS)
                    continue
                log("warning", "login form still up after %ss; the submit did "
                               "not take", MAX_FORM_WAITS * FORM_WAIT_SECONDS)
                return RESULT_STUCK
            driver.fill(("username, email or mobile number", "username"),
                        username, "instagram username")
            driver.fill(("password",), password, "instagram password")
            driver.dismiss_keyboard()
            sleep(KEYBOARD_SETTLE_SECONDS)
            # Re-read the screen before tapping, and do NOT remove this.
            # `tap_label` taps using the driver's *cached* dump and only
            # re-reads when it has none -- so without this it taps a position
            # captured while the keyboard was still open. The Log in button
            # moves ~230px when the keyboard closes (measured: y=542 up,
            # y=775 down on a 1440-tall screen), so the tap lands on nothing,
            # and the form then sits there fully filled while the flow waits
            # for an answer that was never asked for. Four accounts in a row
            # were reported "stuck" by exactly this.
            driver.read_screen()
            driver.tap_label(("Log in", "Log In"))
            submitted = True
            sleep(SETTLE_SECONDS)
            continue

        log("warning", "unnamed screen: %s", text[:200])
        return RESULT_UNKNOWN_SCREEN

    return RESULT_STUCK
