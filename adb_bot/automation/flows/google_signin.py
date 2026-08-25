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
from adb_bot.automation.flows import recaptcha_grid
from adb_bot.automation.flows.verification import looks_like_launcher

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
SCREEN_LAUNCHER = "launcher"              # the phone's home screen
SCREEN_WRONG_PASSWORD = "wrong_password"
SCREEN_ROBOT_CHECK = "google_robot_check"    # a captcha; needs a person
SCREEN_DEVICE_VERIFICATION = "device_verification"  # wants a phone number; needs a person
SCREEN_SERVER_ERROR = "google_server_error"  # transient; retryable
SCREEN_RETRY = "try_again"                   # Google's own retry page
SCREEN_SERVICES = "google_services"          # backup/location consents
SCREEN_PLAY_TIP = "play_install_tip"         # Play Store's one-time install tip
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

# The consent list that follows the Terms -- backup, location, diagnostics --
# and the last screen before the Play Store itself. `Blank caio 2` reached it
# on 2026-08-17 with 2FA and the Terms already behind it.
#
# It is a long scrolling page whose only button is `More` until you reach the
# bottom, where it becomes `Accept`, so the flow taps its way down.
_SERVICES_MARKERS = (
    "google services",
    "back up device data",
    "tap to learn more about each service",
)
_SAVE_PASSWORD_MARKERS = ("save password", "google password manager")

# The Play Store's own one-time "how installs work" tip -- unrelated to
# sign-in, but it can be the very first thing on screen after `glogin` and
# has nothing to do with the account. Seen live 2026-08-25
# (unnikuttan114121@gmail.com): a single `OK` button, no address anywhere in
# the dump, so `accounts_on_device` found nothing and it read as
# `unknown_screen` before the flow ever got a chance to start signing in.
_PLAY_TIP_MARKERS = (
    "optimising app installs",
    "optimizing app installs",
    "google play makes apps faster to install",
)
_PLAY_HOME_MARKERS = ("search apps & games", "search for apps & games",
                      "games apps", "for you top charts")
_WRONG_PASSWORD_MARKERS = ("wrong password", "couldn't sign you in",
                           "try again or click forgot password")

# Google challenging the *address*, before it has asked for a password at all.
# Read off `Blank caio 2` on 2026-08-18 for `hasan428483@gmail.com`, an unused
# pool mailbox: "verify that it's you ... confirm that you're not a robot".
# There is no automating past this -- it is a captcha -- so it must be told
# apart from an unnamed screen, which reads as "the flow got confused" and
# invites a retry that will land here again.
_ROBOT_CHECK_MARKERS = (
    "not a robot",
    "confirm you're not a robot",
    "confirm that you're not a robot",
)

# A different security gate from the robot check -- Google wants a phone
# number to verify the device, not a captcha solved. Equally unautomatable
# (needs a real number that can receive an SMS/call), so it must be told
# apart from an unnamed screen the same way the robot check already is.
# Seen live 2026-08-25 (mdr147391@gmail.com, GeeLark/Android 16): "Verifying
# your phone number ... Google needs to verify your device and phone number
# for security reasons" -- previously fell through to SCREEN_UNKNOWN and read
# as a mystery bug rather than the named dead end it actually is.
_DEVICE_VERIFICATION_MARKERS = (
    "verifying your phone number",
    "needs to verify your device",
)

# Google could not be reached at all. Seen on `Blank caio 1`, 2026-08-17,
# straight after `checking info…`: a bare page carrying **no buttons** -- not
# even "Try again" -- so there is nothing to tap and the only way out is to
# back out and start the Play Store again.
#
# Matched on "communicating with google servers", not on the "something went
# wrong" heading above it, for the same reason as `_EASE_FAILED_MARKERS`: that
# heading sits on top of half a dozen unrelated failures. This one says the
# request never arrived, which is worth retrying; the others are not.
_SERVER_ERROR_MARKERS = (
    "problem communicating with google servers",
    "communicating with google servers",
)

# Google's own retry page: one sentence and a single `Next`. Seen straight
# after accepting the Terms on `Blank caio 1`, 2026-08-17 -- the sign-in had
# succeeded by then, so treating this as fatal threw away a finished 2FA.
#
# Matched on "something went wrong **there**", the retry page's own wording,
# rather than the bare heading it shares with the failed phone lookup and the
# unreachable-servers page.
_RETRY_MARKERS = (
    "sorry, something went wrong there",
    "something went wrong there. please try again",
)

# Ordered: the specific before the general. `_PASSWORD_MARKERS` carries a
# bare "welcome", which appears on several Google screens -- including, it
# turns out, Play Store's own signed-in home ("Welcome to Play... Signed in
# as x@gmail.com"), whose own markers are more specific and so have to be
# checked first. Confirmed live, 2026-08-24 (zaxko530@gmail.com): a sign-in
# that had genuinely succeeded -- password accepted, Terms agreed, Play
# Store home on screen with "for you top charts" right there in the dump --
# was misclassified as SCREEN_PASSWORD anyway because that was checked
# first, then reported RESULT_STUCK on a password field that did not exist.
_ORDERED = (
    (SCREEN_WRONG_PASSWORD, _WRONG_PASSWORD_MARKERS),
    # Both checked before ROBOT_CHECK: solving a grid can land on Google's own
    # "Sorry, something went wrong there" / "Restart" error page while the
    # reCAPTCHA widget's own stale "confirm that you're not a robot" text is
    # still sitting in the same dump above it. Confirmed live 2026-08-25
    # (lucas18anosff@gmail.com, GeeLark/Android 16): the grid was genuinely
    # solved -- "the reCAPTCHA challenge cleared" was logged correctly -- but
    # the very next screen read matched ROBOT_CHECK anyway on that leftover
    # text, so the flow searched for a checkbox on a page that no longer had
    # one and gave up, throwing away a real solve. Both marker sets are
    # narrow, specific sentences a genuine fresh robot-check screen never
    # contains, so this reordering does not risk misreading one as an error.
    (SCREEN_SERVER_ERROR, _SERVER_ERROR_MARKERS),
    (SCREEN_RETRY, _RETRY_MARKERS),
    (SCREEN_ROBOT_CHECK, _ROBOT_CHECK_MARKERS),
    (SCREEN_DEVICE_VERIFICATION, _DEVICE_VERIFICATION_MARKERS),
    (SCREEN_SAVE_PASSWORD, _SAVE_PASSWORD_MARKERS),
    (SCREEN_TOTP, _TOTP_MARKERS),
    (SCREEN_2FA_CHOOSER, _2FA_MARKERS),
    (SCREEN_SERVICES, _SERVICES_MARKERS),
    (SCREEN_PLAY_TIP, _PLAY_TIP_MARKERS),
    (SCREEN_TERMS, _TERMS_MARKERS),
    (SCREEN_EASE_FAILED, _EASE_FAILED_MARKERS),
    (SCREEN_EASE, _EASE_MARKERS),
    (SCREEN_EMAIL, _EMAIL_MARKERS),
    (SCREEN_PLAY_HOME, _PLAY_HOME_MARKERS),
    (SCREEN_PASSWORD, _PASSWORD_MARKERS),
    (SCREEN_PLAY_SIGNIN, _PLAY_SIGNIN_MARKERS),
)

_LOADING_MARKERS = ("just a moment", "loading", "checking info", "please wait",
                    "searching for accounts")

# Phrases that are only ever furniture, never real content, regardless of how
# long the rest of the dump is -- unlike `_LOADING_MARKERS`, not restricted to
# a short dump. Google's own extra risk-check step after the password screen
# repeats its whole "verify that it's you" boilerplate around this phrase, so
# the dump is well past 300 characters; the plain `_LOADING_MARKERS` path
# never caught it and it read as an unnamed screen. Confirmed live,
# 2026-08-24 (brendv748@gmail.com), immediately after the password was
# accepted: "This may take a few moments… To help keep your account safe,
# Google wants to make sure that it's really you trying to sign in
# [address] Loading".
_UNAMBIGUOUS_LOADING_MARKERS = ("this may take a few moments",)

# Words that are never the *content* of a screen, only its furniture. A dump
# containing nothing but these was taken while the real screen was still
# drawing -- `skip next` stopped a run on 2026-08-16 that was otherwise fine.
# The signup flow learned the same lesson on its own screens.
_CHROME_ONLY_WORDS = frozenset({
    "next", "back", "skip", "loading", "ok", "continue", "done", "cancel",
    "google", "sign", "in",
})


def _still_drawing(haystack: str) -> bool:
    if any(marker in haystack for marker in _UNAMBIGUOUS_LOADING_MARKERS):
        return True
    if any(marker in haystack for marker in _LOADING_MARKERS) and len(haystack) <= 300:
        return True
    words = [word for word in re.split(r"[^a-z]+", haystack) if word]
    return bool(words) and set(words) <= _CHROME_ONLY_WORDS


def classify_google_screen(text: str | None, field_hints=()) -> str:
    """Name a screen in the Play Store sign-in chain.

    `field_hints` are the hints of the text fields on screen. They settle the
    one pair these markers cannot: the 2-step-verification **chooser** and the
    **code entry** screen say nearly the same words -- both offer "get a
    verification code from the Google Authenticator app" -- but only the code
    screen has somewhere to type. Read by text alone, `Blank caio 1` bounced
    between the two on 2026-08-17 until the repeat guard stopped it.
    """
    if not text:
        return SCREEN_UNKNOWN
    haystack = text.lower()

    if any("totppin" in hint or "enter code" in hint
           for hint in (field_hints or ())):
        return SCREEN_TOTP

    # Before the markers: the phone's home screen carries none of them, so it
    # would fall through to `unknown` and stop a run whose only problem is that
    # the Play Store did not start. `Blank caio 2`, 2026-08-16, ended on
    # "search gallery play store home telephone messaging music chrome camera".
    if looks_like_launcher(haystack):
        return SCREEN_LAUNCHER

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
RESULT_GOOGLE_UNREACHABLE = "google_unreachable"
RESULT_ROBOT_CHECK = "google_robot_check"
# Google asking for a phone number to verify the device before signing in --
# a different security gate from the reCAPTCHA robot check, equally
# unautomatable (it wants a real phone number that can receive an SMS/call).
RESULT_DEVICE_VERIFICATION = "device_verification"
# The phone never rendered a readable hierarchy. Deliberately not `stuck`: this
# says nothing about the address, so the mailbox stays in the pool and the run
# is worth repeating, which is the opposite of what `stuck` should trigger.
RESULT_NO_DUMP = "no_ui_dump"

# How many dumpless reads to sit through before calling it the phone. Each
# costs about six seconds, against a phone that lives roughly fifteen minutes.
MAX_DUMPLESS_READS = 5

MAX_STEPS = 40
MAX_REPEATS = 4

# Google's sign-in is slow through these proxies -- every request leaves via a
# German mobile exit. `Blank caio 2` sat on `checking info…` for a full minute
# and was given up on at eight waits, which is a flow that quit on a screen that
# was still working. Twenty waits of eight seconds is a bit over two and a half
# minutes, still comfortably inside the phone's ~15-minute life.
MAX_LOADING_WAITS = 20
LOADING_WAIT_SECONDS = 8

# Google refusing to talk is worth sitting out -- it cost a whole launch on
# 2026-08-17 -- but not indefinitely: if it is the proxy rather than a hiccup,
# three tries establish that at the cost of a minute, and the phone's remaining
# life is better spent on a different one.
MAX_SERVER_ERRORS = 3
SERVER_ERROR_WAIT_SECONDS = 20

# A submitted code leaves the field on screen with the code still in it while
# Google checks it -- the dump carries a spinner alongside. Typing again there
# overwrites a submission in flight, which is how `Blank caio 1` lost the one
# run where the code was actually accepted (2026-08-17, 14:01:15). Waiting is
# bounded so a genuinely ignored code still gets retyped rather than hanging.
MAX_CODE_WAITS = 6
CODE_WAIT_SECONDS = 8

# Google's retry page, and the scrolling consent list that follows the Terms.
# Both sit outside the repeat guard -- one because terms -> retry -> terms is a
# cycle it cannot see, the other because tapping down the same page *is* the
# progress -- so each needs a bound of its own.
MAX_RETRY_PAGES = 3
MAX_SERVICES_TAPS = 8

# A dump that comes back empty. Worth several looks -- these phones produce one
# while a screen is mid-transition -- but not forever, since a phone that has
# stopped answering will never answer.
MAX_BLANK_READS = 5

# How many times to open Android's add-account wizard. Two, because the first
# go can land on a stale Play Store home before the wizard draws, and because
# a phone that will not open it at all is not going to on the fifth try.
MAX_ADD_ACCOUNT_STARTS = 2

# How long to let Google sit on a password it is checking. The same shape as the
# code screen's wait, because the failure they guard against is the same:
# retyping into a form that is already submitted, which spends the repeat guard
# without ever giving the check time to land. Eight, because on 2026-08-18 the
# form was still spinning 47 seconds after `NEXT`.
MAX_PASSWORD_WAITS = 8
PASSWORD_WAIT_SECONDS = 10
BLANK_READ_WAIT_SECONDS = 6

# How many times *this run* will try clearing a reCAPTCHA challenge before
# accepting `RESULT_ROBOT_CHECK`. `recaptcha_grid.solve_checkbox` already
# retries the checkbox itself several times internally (its own budget is a
# couple of minutes); this is a second, outer layer for the rarer case where
# Google shows a *second*, independent challenge right after the first one
# cleared. Kept small -- each attempt is expensive in both time and 2captcha
# cost, and a mailbox that fails this many is worth a person's look.
MAX_ROBOT_CHECK_SOLVES = 2

_SKIP = ("Skip", "SKIP", "Not now", "NOT NOW", "Never", "NEVER")
_NEXT = ("Next", "NEXT", "Continue", "CONTINUE")


def _start_play_store(adb_client, target: str, logger=None) -> bool:
    """Bring the Play Store up, and say whether it actually arrived.

    Two ways round, because neither is reliable alone on these phones:
    `monkey` returned an empty string and started nothing on three separate
    runs, and the explicit intent needs an activity name that is not stable
    across Play Store versions -- so the name is resolved from the phone.

    Confirming matters as much as starting. `Blank caio 2` was left on its home
    screen with the flow reading "search gallery play store home telephone" and
    calling it an unnamed screen.
    """
    resolved = adb_client.run_command(
        f"adb -s {target} shell cmd package resolve-activity --brief "
        f"{PLAY_PACKAGE}") or ""
    activity = ""
    for line in resolved.splitlines():
        line = line.strip()
        if "/" in line and line.startswith(PLAY_PACKAGE):
            activity = line
    if activity:
        adb_client.run_command(f"adb -s {target} shell am start -n {activity}")
    else:
        if logger is not None:
            logger.warning("google_signin: could not resolve a Play Store "
                           "activity from %r", resolved.strip()[:160])
        adb_client.run_command(
            f"adb -s {target} shell monkey -p {PLAY_PACKAGE} "
            f"-c android.intent.category.LAUNCHER 1")

    time.sleep(6)
    focus = adb_client.run_command(
        f"adb -s {target} shell dumpsys window | grep mCurrentFocus") or ""
    arrived = PLAY_PACKAGE in focus
    if logger is not None:
        logger.info("google_signin: Play Store %s (focus %s)",
                    "is in front" if arrived else "did NOT come up",
                    focus.strip()[:120] or "<none>")
    return arrived


def _start_add_account(adb_client, target: str, logger=None) -> bool:
    """Open Google's own "add an account" wizard, and say whether it arrived.

    The Play Store's `Sign in` button only exists while the phone has no Google
    account at all. Once one is on there the store opens on its home screen,
    and the second mailbox has to be added through Android's account settings
    instead -- `ADD_ACCOUNT_SETTINGS` with `account_types` pinned to Google, so
    the picker is skipped and the wizard opens straight on the email form.

    This is deliberately not Gmail's own `Add an email address`, which fails at
    the Terms step on these phones every time (SIGNUP_RUN_2026-08-13).
    """
    adb_client.run_command(
        f"adb -s {target} shell am start -a "
        f"android.settings.ADD_ACCOUNT_SETTINGS "
        f"--esa account_types com.google")
    time.sleep(8)
    focus = adb_client.run_command(
        f"adb -s {target} shell dumpsys window | grep mCurrentFocus") or ""
    # Whichever wrapper Android puts in front, the wizard itself is GMS.
    arrived = ("com.google.android.gms" in focus
               or "AddAccountSettings" in focus
               or "accounts" in focus.lower())
    if logger is not None:
        logger.info("google_signin: add-account wizard %s (focus %s)",
                    "is in front" if arrived else "did NOT come up",
                    focus.strip()[:120] or "<none>")
    return arrived


def _wake(adb_client, target: str) -> None:
    """Wake the phone and get past the keyguard.

    KEYCODE_WAKEUP rather than POWER, which would toggle a screen that is
    already on back off.
    """
    adb_client.run_command(f"adb -s {target} shell input keyevent 224")
    adb_client.run_command(f"adb -s {target} shell wm dismiss-keyguard")


def _press_enter(adb_client, target: str) -> None:
    """Submit the focused field with the keyboard's own action key.

    Keyevent 66 is `ENTER`. On Google's sign-in form this is the submit the
    button tap cannot reproduce: the tap is delivered to the right bounds and
    the form simply redraws.
    """
    adb_client.run_command(f"adb -s {target} shell input keyevent 66")


def _submit_after_typing(driver, labels) -> bool:
    """Tap `labels` after typing, without dismissing the keyboard first.

    Same fix as `instagram_login._submit_after_typing` and
    `signup._submit_after_typing`, found the same day (2026-08-22): both of
    those flows dismissed the keyboard (`KEYCODE_BACK`) before their first tap
    attempt, and BACK is not reliably consumed by the IME on some devices --
    it falls through and steps the *flow itself* back a screen, which reads
    as "stuck" or "the form keeps re-rendering" rather than what it is. The
    email and password steps here have exactly the same dismiss-then-tap
    shape, so the same fix applies: try the tap with the keyboard still open
    first, and only dismiss as a fallback if the button truly is not
    reachable that way. The existing `_press_enter` escalation on a *second*
    failed submit is untouched -- it answers a different failure (the tap
    lands and the form redraws anyway), not this one.
    """
    if driver.tap_label(labels):
        return True
    driver.dismiss_keyboard()
    return driver.tap_label(labels)


# How long to leave between looks while waiting for a screen to move. A
# `uiautomator` dump costs about 2-3 seconds on these phones (measured on
# `Blank caio 2`, 2026-08-18), so the real cadence is ~4s and there is nothing
# to gain from a smaller number -- the dump, not the pause, is the floor.
POLL_SECONDS = 1.5


def _differs(a: str, b: str) -> bool:
    """Whether two screen reads are meaningfully different.

    Whitespace and case are noise: the same screen dumps with different spacing
    depending on how far a layout has settled.
    """
    return " ".join((a or "").split()).lower() != " ".join((b or "").split()).lower()


def settle(driver, previous: str, seconds: float, sleep=time.sleep,
           clock=time.monotonic) -> str:
    """Wait for the screen to move on from `previous`, up to `seconds`.

    Replaces a blind `sleep(seconds)` after an action. The ceiling is unchanged
    -- what changes is that a screen which settles in three seconds no longer
    costs ten, and this chain has about a dozen such waits in it.

    **It only returns early on an actual change.** A screen that has not moved
    is waited out in full, so the caller's repeat guard counts exactly what it
    counted before: this makes a working run faster without making a stuck one
    look different.

    Two things never end the wait: an empty read, which is a dump that failed
    rather than a screen that changed; and a change into a spinner, because
    Google's forms redraw in stages and acting on a half-drawn one lands a tap
    on nothing or on the wrong control -- which is what the fixed sleeps were
    there to prevent.

    Bounded twice on purpose. The clock is the real limit in production; the
    look count is what keeps the tests -- which inject a sleep that does not
    sleep -- from spinning on the driver for a wall-clock second.

    Returns the last text actually read, so the caller can use it instead of
    paying for another dump.
    """
    if seconds <= 0:
        return previous
    deadline = clock() + seconds
    # One more look than the budget divides into, so the clock is what
    # actually limits a production wait and this count only ever catches
    # the tests, whose injected sleep does not move a real clock.
    looks = max(2, int(seconds / POLL_SECONDS) + 1)
    last = None
    for _ in range(looks):
        if clock() >= deadline:
            break
        sleep(POLL_SECONDS)
        try:
            current = driver.read_screen() or ""
        except Exception:                                     # noqa: BLE001
            # The driver's own problem, not a screen state. Hand back what we
            # have and let the loop's normal handling see it.
            return last if last is not None else previous
        if not current.strip():
            continue
        last = current
        if not _differs(current, previous):
            continue
        if classify_google_screen(current) == SCREEN_LOADING:
            continue
        return current
    return last if last is not None else previous


def accounts_on_device(adb_client, target: str) -> list[str]:
    """Every Google account already on the phone.

    `dumpsys account` is the only honest answer to "is the mailbox on here" --
    Gmail showing an inbox proves nothing about *which* mailbox.
    """
    out = adb_client.run_command(
        f"adb -s {target} shell dumpsys account") or ""
    return re.findall(r"Account\s*\{name=([^,]+),\s*type=com\.google", out)


def sign_in(driver, adb_client, target: str, address: str, password: str,
            totp_secret: str, logger=None, sleep=time.sleep,
            clock=time.monotonic, recaptcha_solver=None) -> str:
    """Sign `address` into the phone through the Play Store.

    Returns one of the `RESULT_*` constants. Never force-stops anything: this
    may run on a phone with a signup in flight.

    `recaptcha_solver` is a `CaptchaSolver` (`adb_bot.clients.captcha`) used
    to answer an image-grid reCAPTCHA challenge if one appears -- built from
    the configured 2captcha key when not given explicitly. Passed through
    mainly so tests can inject a double; production callers can leave it out.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)("google_signin: " + message, *args)

    if recaptcha_solver is None:
        from adb_bot.clients.captcha import build_solver
        recaptcha_solver = build_solver(logger=logger)

    already = accounts_on_device(adb_client, target)
    if any(address.lower() == name.lower() for name in already):
        log("info", "%s is already on the phone", address)
        return RESULT_ALREADY
    if already:
        log("warning", "the phone already carries %s -- adding a second "
                       "account", already)

    _start_play_store(adb_client, target, logger=logger)
    sleep(10)

    last, repeats, loading_waits = None, 0, 0
    # The failed phone lookup returns to the email form, so email -> lookup ->
    # failure -> email is a *cycle of different screens* that the repeat guard
    # cannot see. Bounded separately.
    lookup_failures = 0
    MAX_LOOKUP_FAILURES = 2
    email_submits = 0
    password_submits = 0
    # Reads that produced no hierarchy at all. Counted across the whole run,
    # not per screen: a phone that cannot be dumped is not going to start on
    # the next screen either.
    dumpless = 0
    robot_check_solves = 0
    totp_submits = 0
    submitted_code, code_waits = None, 0
    restarts = 0
    MAX_APP_RESTARTS = 3
    server_errors = 0
    retry_pages = 0
    services_taps = 0
    blank_reads = 0
    add_account_starts = 0
    password_waits = 0
    email_loading_waits = 0

    # What the last adaptive wait already read. Using it saves one dump per
    # step -- 2-3 seconds each, about twenty times a run.
    pending: str | None = None

    for step in range(MAX_STEPS):
        if pending is not None:
            text, pending = pending, None
        else:
            text = driver.read_screen() or ""

        # An empty read is a dump that failed, not a screen that is unknown.
        # `Blank caio 3` ended a run on one within two steps of starting
        # (2026-08-17), and the same mistake -- reading "nothing" as "not what
        # I expected" -- is the one this codebase keeps warning about.
        if not text.strip():
            blank_reads += 1
            if blank_reads > MAX_BLANK_READS:
                log("warning", "the screen would not dump anything, %d times "
                               "running", blank_reads - 1)
                return RESULT_STUCK
            if blank_reads == 1:
                # `mCurrentFocus=null` and nothing to dump is what a sleeping
                # or locked phone looks like, and it is the state `Blank caio
                # 3` was in when the Play Store would not start. Waking it
                # costs two commands and is worth trying before spending the
                # rest of the budget looking at nothing.
                log("info", "nothing on screen and no focused window; waking "
                            "the phone")
                _wake(adb_client, target)
            log("info", "the screen dumped nothing; looking again (%d/%d)",
                blank_reads, MAX_BLANK_READS)
            sleep(BLANK_READ_WAIT_SECONDS)
            continue
        blank_reads = 0

        hints = driver.input_hints() if hasattr(driver, "input_hints") else ()
        screen = classify_google_screen(text, hints)
        log("info", "step %d: %s", step + 1, screen)

        # Before the repeat guard, and for the same reason as `loading`: a
        # password that is still being checked is not a screen that failed to
        # advance. `Blank caio 2` spent its whole budget here on 2026-08-18 --
        # the password was in the field and Google was drawing its own spinner
        # over the form, and every pass typed it again.
        if screen == SCREEN_PASSWORD and password_submits:
            values = driver.input_values() if hasattr(driver, "input_values") else []
            if password in values:
                password_waits += 1
                if password_waits <= MAX_PASSWORD_WAITS:
                    log("info", "the password is still in the field; giving "
                                "Google a moment (%d/%d)",
                        password_waits, MAX_PASSWORD_WAITS)
                    sleep(PASSWORD_WAIT_SECONDS)
                    continue
                # Long enough that it was not accepted: let the handler below
                # type it again.
                log("info", "the password has sat unanswered; typing it again")
                password_waits = 0
            else:
                # Google cleared the field, so it is asking again rather than
                # still thinking.
                password_waits = 0

        # Before the repeat guard, and for the same reason as `loading`: a code
        # that is still being checked is not a screen that failed to advance.
        if screen == SCREEN_TOTP and submitted_code:
            values = driver.input_values() if hasattr(driver, "input_values") else []
            if submitted_code in values:
                code_waits += 1
                if code_waits <= MAX_CODE_WAITS:
                    log("info", "the submitted code is still in the field; "
                                "giving Google a moment (%d/%d)",
                        code_waits, MAX_CODE_WAITS)
                    sleep(CODE_WAIT_SECONDS)
                    continue
                # Long enough that it was not accepted: let the handler below
                # type a fresh one.
                log("info", "the code has sat unanswered; typing a fresh one")
                submitted_code, code_waits = None, 0
            else:
                # Google cleared it -- that is a rejection, not a wait.
                submitted_code, code_waits = None, 0

        if screen == SCREEN_LOADING:
            loading_waits += 1
            if loading_waits > MAX_LOADING_WAITS:
                log("warning", "still loading after %d waits", loading_waits)
                return RESULT_STUCK
            pending = settle(driver, text, LOADING_WAIT_SECONDS,
                             sleep=sleep, clock=clock)
            continue
        loading_waits = 0

        # Before the repeat guard, same reasoning as the password/code waits
        # above: `_still_drawing`'s loading check only fires for a *bare*
        # "Just a moment" screen (<=300 chars) so it never catches a full
        # email form with a small loading indicator drawn over it -- the form
        # text alone is already past that length. That form then classified
        # as a plain, unfinished `SCREEN_EMAIL` and got re-filled and
        # re-tapped on every pass, hitting `MAX_REPEATS` (4 tries) in well
        # under the 160s the dedicated loading wait allows -- while a real
        # Play Store sign-in can sit here past a minute (confirmed live,
        # 2026-08-23). Caught here, before that guard, whenever the address
        # is already correctly in the field: nothing left to fill, so a
        # repeated read of the same screen is Google still working, not the
        # flow failing to advance.
        if screen == SCREEN_EMAIL and email_submits and any(
                m in text.lower() for m in _LOADING_MARKERS):
            values = driver.input_values() if hasattr(driver, "input_values") else []
            if any(address.lower() == str(v).lower() for v in values):
                email_loading_waits += 1
                if email_loading_waits <= MAX_LOADING_WAITS:
                    log("info", "the email screen has not advanced but the "
                                "address is still correctly in the field and "
                                "the page says it's loading; giving Google a "
                                "moment (%d/%d)",
                        email_loading_waits, MAX_LOADING_WAITS)
                    pending = settle(driver, text, LOADING_WAIT_SECONDS,
                                     sleep=sleep, clock=clock)
                    continue
                log("warning", "email screen still loading after %d waits",
                    email_loading_waits)
                return RESULT_STUCK
        email_loading_waits = 0

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
            if names and add_account_starts < MAX_ADD_ACCOUNT_STARTS:
                # A phone that already carries somebody else's mailbox opens
                # the store on its home screen, so there is no `Sign in` to
                # press. This is the *only* reason a second account cannot be
                # added the same way as the first, and it read as `stuck`.
                add_account_starts += 1
                log("info", "the store is signed in as %s; opening Android's "
                            "add-account wizard for %s (%d/%d)",
                    names, address, add_account_starts,
                    MAX_ADD_ACCOUNT_STARTS)
                _start_add_account(adb_client, target, logger=logger)
                # The wizard re-enters on the email form, which the repeat
                # guard would otherwise count against the screen we came from.
                last, repeats = None, 0
                continue
            log("warning", "Play Store looks signed in but dumpsys says %s",
                names or "nothing")
            return RESULT_STUCK

        if screen == SCREEN_LAUNCHER:
            # Not a verdict about anything -- the Play Store simply is not in
            # front. Starting it again is nearly free.
            restarts += 1
            if restarts > MAX_APP_RESTARTS:
                log("warning", "the Play Store would not stay in front")
                return RESULT_STUCK
            log("info", "on the home screen; starting the Play Store again "
                        "(%d/%d)", restarts, MAX_APP_RESTARTS)
            _start_play_store(adb_client, target, logger=logger)
            sleep(8)
            continue

        if screen == SCREEN_SERVER_ERROR:
            # Nothing on this screen is tappable, so there is no "try again" to
            # press: back out of it and start the Play Store over.
            server_errors += 1
            if server_errors > MAX_SERVER_ERRORS:
                log("warning", "Google was unreachable %d times running",
                    server_errors - 1)
                return RESULT_GOOGLE_UNREACHABLE
            log("info", "Google could not be reached; waiting %ds and starting "
                        "over (%d/%d)", SERVER_ERROR_WAIT_SECONDS,
                server_errors, MAX_SERVER_ERRORS)
            sleep(SERVER_ERROR_WAIT_SECONDS)
            adb_client.shell_back(target)
            sleep(2)
            _start_play_store(adb_client, target, logger=logger)
            sleep(8)
            # The retry re-enters on the same screen it failed from, and the
            # repeat guard would count that as going nowhere.
            last, repeats = None, 0
            continue

        if screen == SCREEN_RETRY:
            # Manual signups hit this exact page and found the account
            # already signed in once Play Store and Google Services were
            # force-closed and reopened -- the page is a Play Store UI
            # glitch, not proof the sign-in failed server-side (confirmed by
            # hand, 2026-08-22; matches the 2026-08-17 note above about a
            # finished 2FA nearly being thrown away here). Check the real
            # source of truth, `dumpsys account`, before spending a retry on
            # the on-screen button, which walks the whole flow from
            # scratch and can lose a sign-in that already succeeded.
            if not retry_pages:
                log("info", "force-closing Play Store and Google Services to "
                            "check whether the sign-in already went through")
                adb_client.run_command(
                    f"adb -s {target} shell am force-stop {PLAY_PACKAGE}")
                adb_client.run_command(
                    f"adb -s {target} shell am force-stop "
                    f"com.google.android.gms")
                sleep(3)
                _start_play_store(adb_client, target, logger=logger)
                sleep(6)
                if address in accounts_on_device(adb_client, target):
                    log("info", "%s is on the device after all -- the retry "
                                "page was a UI glitch, not a real failure",
                        address)
                    return RESULT_SIGNED_IN

            # One button, and taking it is the whole point of the page. The
            # repeat guard cannot see terms -> retry -> terms as going nowhere,
            # so this is bounded on its own.
            retry_pages += 1
            if retry_pages > MAX_RETRY_PAGES:
                log("warning", "Google offered its retry page %d times",
                    retry_pages - 1)
                return RESULT_STUCK
            log("info", "taking Google's retry page (%d/%d)", retry_pages,
                MAX_RETRY_PAGES)
            if not driver.tap_label(_NEXT + ("Try again", "TRY AGAIN",
                                             "Retry", "RETRY",
                                             "Restart", "RESTART")):
                log("warning", "nothing to tap on the retry page")
                return RESULT_STUCK
            sleep(10)
            continue

        if screen == SCREEN_WRONG_PASSWORD:
            return RESULT_WRONG_PASSWORD

        if screen == SCREEN_ROBOT_CHECK:
            # Solved live for the first time 2026-08-24: the checkbox and its
            # image grid are answerable (OCR to find them, 2captcha's
            # `GridTask` to read the pictures) -- not the dead end this
            # branch used to assume. Try clearing it in place before giving
            # up; only report `RESULT_ROBOT_CHECK` once that has genuinely
            # failed.
            if robot_check_solves < MAX_ROBOT_CHECK_SOLVES:
                robot_check_solves += 1
                log("info", "a reCAPTCHA challenge is on screen for %s -- "
                            "attempting to clear it (%d/%d)", address,
                    robot_check_solves, MAX_ROBOT_CHECK_SOLVES)
                if recaptcha_grid.solve_checkbox(driver, recaptcha_solver,
                                                 logger=logger, sleep=sleep,
                                                 clock=clock):
                    log("info", "the reCAPTCHA challenge cleared; continuing "
                                "the sign-in")
                    last, repeats = None, 0
                    continue
                log("warning", "could not clear the reCAPTCHA challenge "
                               "(%d/%d)", robot_check_solves,
                    MAX_ROBOT_CHECK_SOLVES)
            log("warning", "Google put a robot check on %s -- that mailbox "
                           "cannot be signed in from here; use another",
                address)
            return RESULT_ROBOT_CHECK

        if screen == SCREEN_DEVICE_VERIFICATION:
            # No captcha to solve here -- Google wants an actual phone number
            # to send an SMS/call to. Nothing to try in place, unlike the
            # robot check; this mailbox needs a person with a number, or
            # another mailbox.
            log("warning", "Google wants to verify %s's device with a phone "
                           "number -- that mailbox cannot be signed in from "
                           "here; use another", address)
            return RESULT_DEVICE_VERIFICATION

        if screen == SCREEN_UNKNOWN:
            # Ask the phone before calling this a failure. `Blank caio 2` ended
            # a 750-second sign-in on "signed in as cicireynaamelia@gmail.com"
            # (2026-08-18) -- Google's own confirmation, reported as
            # `unknown_screen`, which spent a launch and read as the mailbox
            # being unusable. The screen at the end of this chain is the one
            # part of it we cannot enumerate; `dumpsys` is the same honest
            # answer relied on everywhere else here.
            names = accounts_on_device(adb_client, target)
            if any(address.lower() == name.lower() for name in names):
                log("info", "%s is on the phone, whatever this screen is: %s",
                    address, (text or "")[:120])
                return RESULT_SIGNED_IN
            log("warning", "unnamed screen: %s", (text or "")[:300])
            return RESULT_UNKNOWN_SCREEN

        if screen == SCREEN_PLAY_SIGNIN:
            driver.tap_label(("Sign in", "SIGN IN"))
            sleep(8)

        elif screen == SCREEN_EASE:
            # A lookup by phone number. These phones cannot receive SMS.
            if not driver.tap_label(_SKIP):
                driver.tap_label(_NEXT)
            pending = settle(driver, text, 6, sleep=sleep, clock=clock)

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
                # A screen read by OCR has no input fields *by construction* --
                # OCR returns text, not a hierarchy -- so `fill` cannot
                # possibly succeed on one, and reporting that as "the address
                # would not stay in the field" blames the mailbox for a failed
                # `uiautomator dump`. Five Geelark phones were written off that
                # way on 2026-08-21 with the email box plainly visible in the
                # OCR text. Give the dump another go instead: it is a transient
                # on a phone still settling, not a verdict on anything.
                if getattr(driver, "_source", "") != "ui-dump":
                    dumpless += 1
                    if dumpless > MAX_DUMPLESS_READS:
                        log("warning", "the screen would not produce a UI dump "
                                       "after %d tries; this is the phone, not "
                                       "the mailbox", dumpless)
                        return RESULT_NO_DUMP
                    log("info", "no UI dump on the email screen (%d/%d) -- "
                                "waiting and reading again",
                        dumpless, MAX_DUMPLESS_READS)
                    sleep(6)
                    pending = None
                    continue
                log("warning", "the address would not stay in the field")
                return RESULT_STUCK
            email_submits += 1
            if email_submits == 1:
                _submit_after_typing(driver, _NEXT)
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
            pending = settle(driver, text, 9, sleep=sleep, clock=clock)

        elif screen == SCREEN_PASSWORD:
            # Same reasoning as the email screen, including the fallback.
            if not driver.fill(("password",), password, "google password"):
                log("warning", "the password would not stay in the field")
                return RESULT_STUCK
            password_submits += 1
            if password_submits == 1:
                _submit_after_typing(driver, _NEXT)
            else:
                _press_enter(adb_client, target)
            pending = settle(driver, text, 10, sleep=sleep, clock=clock)

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
            pending = settle(driver, text, 6, sleep=sleep, clock=clock)

        elif screen == SCREEN_TOTP:
            # Generated and typed inside one window, from this process.
            code, left = totp.fresh_code(totp_secret)
            log("info", "code with %ds left", left)
            if not driver.fill(("code", "totppin"), code, "2fa code"):
                log("warning", "the code did not land in the field")
                return RESULT_STUCK
            totp_submits += 1
            if totp_submits == 1:
                driver.dismiss_keyboard()
                driver.tap_label(_NEXT)
            else:
                # The same thing the email and password forms do: the code reads
                # back out of the field, `NEXT` is tapped at the bounds the dump
                # reports, and Google redraws the screen (`Blank caio 1`, four
                # codes running, 2026-08-17). Submitting with the IME action is
                # what actually moves these screens. A fresh code is generated
                # every pass, so nothing here is retrying a stale one.
                log("info", "tapping NEXT did not move the code screen; "
                            "submitting with the keyboard's own action")
                _press_enter(adb_client, target)
            submitted_code = code
            pending = settle(driver, text, 10, sleep=sleep, clock=clock)

        elif screen == SCREEN_SERVICES:
            # `Accept` first: on the last page both buttons may be present, and
            # tapping `More` there would scroll past the one that finishes.
            services_taps += 1
            if services_taps > MAX_SERVICES_TAPS:
                log("warning", "the Google services page would not end after "
                               "%d taps", services_taps - 1)
                return RESULT_STUCK
            if not driver.tap_label(("Accept", "ACCEPT", "I agree", "AGREE",
                                     "Agree", "Turn on", "More", "MORE",
                                     "Next", "NEXT")):
                log("warning", "nothing to tap on the Google services page")
                return RESULT_STUCK
            # Scrolling the same page is progress, not a screen that failed to
            # advance, so this is bounded by its own counter instead.
            last, repeats = None, 0
            pending = settle(driver, text, 8, sleep=sleep, clock=clock)

        elif screen == SCREEN_PLAY_TIP:
            # Nothing to do with the account -- just get it out of the way.
            if not driver.tap_label(("OK", "Ok", "GOT IT", "Got it")):
                log("warning", "nothing to tap on the Play Store install tip")
                return RESULT_STUCK
            last, repeats = None, 0
            pending = settle(driver, text, 6, sleep=sleep, clock=clock)

        elif screen == SCREEN_TERMS:
            driver.tap_label(("I agree", "I AGREE", "Accept", "ACCEPT"))
            pending = settle(driver, text, 10, sleep=sleep, clock=clock)

        elif screen == SCREEN_SAVE_PASSWORD:
            # "NOT NOW" the first time, "NEVER" after -- both are in `_SKIP`.
            if not driver.tap_label(_SKIP):
                driver.dismiss_keyboard()
            pending = settle(driver, text, 4, sleep=sleep, clock=clock)

        sleep(2)

    return RESULT_STUCK


# Results worth a fresh Play Store/Google Play Services rather than accepted
# as final. `RESULT_ROBOT_CHECK` and `RESULT_WRONG_PASSWORD` are deliberately
# absent -- both are about the *account* (a captcha tied to the address, a
# genuinely wrong credential), and closing the app again does not make
# Google re-verify an account any faster or a wrong password become right;
# it would just spend another full walk of the chain finding the same wall.
_RETRYABLE_RESULTS = (RESULT_STUCK, RESULT_UNKNOWN_SCREEN,
                      RESULT_GOOGLE_UNREACHABLE, RESULT_NO_DUMP)

DEFAULT_SIGNIN_RETRIES = 5


def sign_in_with_retries(driver, adb_client, target: str, address: str,
                         password: str, totp_secret: str, logger=None,
                         max_attempts: int = DEFAULT_SIGNIN_RETRIES,
                         sleep=time.sleep, clock=time.monotonic,
                         recaptcha_solver=None) -> str:
    """`sign_in`, force-stopping Play Store and Google Play Services and
    starting over on a retryable result, up to `max_attempts` times total.

    Built for the 2-step-verification chooser specifically (`oukroaicha
    @gmail.com`, 2026-08-23): tapping "Get a verification code from the
    Google Authenticator app" highlighted the row blue and went nowhere --
    an app-state glitch, not a wrong answer, and closing Play Store/GMS and
    walking the chain again is the same fix already proven for the
    equivalent install-side stalls in `play_install.install_with_retries`.
    """
    verdict = RESULT_STUCK
    for attempt in range(1, max_attempts + 1):
        verdict = sign_in(driver, adb_client, target, address, password,
                          totp_secret, logger=logger, sleep=sleep, clock=clock,
                          recaptcha_solver=recaptcha_solver)
        if verdict in (RESULT_SIGNED_IN, RESULT_ALREADY):
            return verdict
        if verdict not in _RETRYABLE_RESULTS:
            return verdict
        if logger is not None:
            logger.info("google_signin: attempt %d/%d for %s ended %s -- "
                        "closing Play Store/GMS and trying again",
                        attempt, max_attempts, address, verdict)
        if attempt < max_attempts:
            adb_client.run_command(f"adb -s {target} shell am force-stop "
                                   f"{PLAY_PACKAGE}")
            adb_client.run_command(f"adb -s {target} shell am force-stop "
                                   f"com.google.android.gms")
            sleep(6)
    return verdict
