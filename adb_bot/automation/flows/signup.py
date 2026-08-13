"""Create an Instagram account: name the screen, do the one thing it needs, look again.

Same shape as `verification.py` -- a loop over classified screens -- because
signup is the same problem with a different marker set. Every marker below was
read off a real dump during the 2026-08-13 run that produced `@cici.aurainta`;
see `SIGNUP_RUN_2026-08-13.md`. Nothing here was written from general knowledge,
which is deliberate: on that run every list guessed in advance was wrong, and
every list taken from a dump worked first time.

**Signup uses SMS, not email.** The mobile-number screen is the one Instagram
offers first, and reading a mailbox from this fleet is unsolved -- IMAP wants an
app-specific password, Android refuses to add the mailbox after Google's Terms
screen, and Chrome on these profiles cannot reach mail.google.com at all.

**The run has to finish in one go.** Restarting Instagram returns to "Join
Instagram" and throws away an already-verified number, so there is no resuming a
half-made account, and the phone itself only lives ~15 minutes.

Three screens arrive pre-filled with something wrong and none of them errors if
accepted:

* the **username** carries Instagram's own suggestion;
* the **"add an email address"** screen carries the *phone's* Google account;
* the **date-of-birth picker opens on today**, so accepting it claims the
  account holder was born this year.

So every field is cleared, typed, and read back before anything is submitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from adb_bot.automation import ban_detection
from adb_bot.automation.flows.verification import looks_like_launcher, screen_is_healthy

# --- what a screen can be -----------------------------------------------------
SCREEN_ENTRY = "entry"                  # "Join Instagram" -- the start
SCREEN_PHONE = "phone"                  # "What's your mobile number?"
SCREEN_EMAIL = "email"                  # "What's your email address?" (we avoid it)
SCREEN_CODE = "code"                    # "Enter the confirmation code"
SCREEN_PASSWORD = "password"            # "Create a password"
SCREEN_BIRTHDAY = "birthday"            # "What's your date of birth?"
SCREEN_DATE_PICKER = "date_picker"      # the Android spinner dialog
SCREEN_NAME = "name"                    # "What's your name?"
SCREEN_USERNAME = "username"            # "Create a username"
SCREEN_TERMS = "terms"                  # "Agree to Instagram's terms" -- creates it
SCREEN_PERMISSIONS = "permissions"      # "Allow Instagram to access your device?"
SCREEN_PHOTO_PROMPT = "photo_prompt"    # "Add a profile photo"
SCREEN_FOLLOW = "follow"                # "Follow 5 or more people"
SCREEN_ADD_EMAIL = "add_email"          # post-creation "Add an email address"
SCREEN_INTERSTITIAL = "interstitial"    # feed personalisation, nav tips
SCREEN_SAVE_PASSWORD = "save_password"  # Android autofill, not Instagram
SCREEN_LAUNCHER = "launcher"            # the phone's home screen -- start the app
SCREEN_LOADING = "loading"              # mid-render -- wait, do not act
SCREEN_DONE = "done"                    # a working account
SCREEN_BANNED = "banned"                # created and immediately disabled
SCREEN_UNKNOWN = "unknown"              # stop -- never act on this

_ENTRY_MARKERS = (
    "join instagram",
    "share what you're into with the people who get you",
)

# "Get started" alone is too broad to name the entry screen -- it appears on
# onboarding screens all over the app -- so it is only a marker next to the
# title above.
_PHONE_MARKERS = (
    "what's your mobile number",
    "whats your mobile number",
    "enter the mobile number on which you can be contacted",
)

_EMAIL_MARKERS = (
    "what's your email address",
    "whats your email address",
    "enter the email address at which you can be contacted",
)

# Strong enough to beat the phone markers, which is why CODE is ordered first:
# the code screen names the number it just texted, so it contains a phone
# number and often the word "mobile" too.
_CODE_MARKERS = (
    "enter the confirmation code",
    "6-digit code",
    "six-digit code",
    "i didn't receive the code",
)

_PASSWORD_MARKERS = (
    "create a password",
    "create a password with at least six",
)

_BIRTHDAY_MARKERS = (
    "what's your date of birth",
    "whats your date of birth",
    "use your own date of birth",
    "why do i need to provide my date of birth",
)

# The Android date-picker dialog. It has no Instagram wording at all -- just the
# spinners and the two buttons -- so it is named by those.
_DATE_PICKER_MARKERS = (
    "set date",
)

_NAME_MARKERS = (
    "what's your name",
    "whats your name",
)

_USERNAME_MARKERS = (
    "create a username",
    "add a username or use our suggestion",
)

_TERMS_MARKERS = (
    "agree to instagram's terms and policies",
    "by tapping i agree, you agree to create an account",
)

_PERMISSIONS_MARKERS = (
    "allow instagram to access your device",
)

_PHOTO_PROMPT_MARKERS = (
    "add a profile photo",
    "profile photo that shows your vibe",
)

_FOLLOW_MARKERS = (
    "follow 5 or more people",
    "following isn't required",
)

_ADD_EMAIL_MARKERS = (
    "add an email address",
    "enter the email where you can be contacted",
)

_INTERSTITIAL_MARKERS = (
    "see more of what you love in your feed",
    "swipe to easily access reels and messages",
    "we've simplified our navigation",
)

# Android's password manager, not Instagram. It overlays the signup and its
# label changes between runs -- "NOT NOW" the first time, "NEVER" afterwards --
# so it is dismissed with Back rather than by matching a label.
_SAVE_PASSWORD_MARKERS = (
    "save password to google password manager",
    "passwords are saved to google password manager",
)

# What a finished account looks like. `screen_is_healthy` covers the feed (its
# markers are the story tray and the bottom nav), but a brand-new account often
# lands on its own *profile*, which has none of those -- read off
# `@cici.aurainta` minutes after it was created. Whitelist, for the same reason
# verification's is: the danger is always the screen nobody has seen.
_ACCOUNT_EXISTS_MARKERS = (
    "edit profile",
    "share profile",
    "add your bio",
    "your profile.",
)

_ORDERED_MARKERS = (
    # Before anything else: a screen that says the account is gone is not a
    # step in the chain.
    (SCREEN_SAVE_PASSWORD, _SAVE_PASSWORD_MARKERS),
    (SCREEN_DATE_PICKER, _DATE_PICKER_MARKERS),
    (SCREEN_CODE, _CODE_MARKERS),
    (SCREEN_TERMS, _TERMS_MARKERS),
    (SCREEN_PASSWORD, _PASSWORD_MARKERS),
    (SCREEN_BIRTHDAY, _BIRTHDAY_MARKERS),
    (SCREEN_USERNAME, _USERNAME_MARKERS),
    (SCREEN_NAME, _NAME_MARKERS),
    # "add an email address" before the signup email screen: the post-creation
    # prompt also contains the word pair, and mistaking it for the signup
    # screen would type an address into a live account's settings.
    (SCREEN_ADD_EMAIL, _ADD_EMAIL_MARKERS),
    (SCREEN_EMAIL, _EMAIL_MARKERS),
    (SCREEN_PHONE, _PHONE_MARKERS),
    (SCREEN_PERMISSIONS, _PERMISSIONS_MARKERS),
    (SCREEN_PHOTO_PROMPT, _PHOTO_PROMPT_MARKERS),
    (SCREEN_FOLLOW, _FOLLOW_MARKERS),
    (SCREEN_INTERSTITIAL, _INTERSTITIAL_MARKERS),
    (SCREEN_ENTRY, _ENTRY_MARKERS),
)


# What a half-drawn screen says. Either it names its own waiting, or the only
# thing that has rendered so far is furniture -- a lone `next` was one of the
# two unnamed Instagram screens in the recordings.
_LOADING_MARKERS = ("loading", "checking info", "just a moment",
                    "this will take just a moment")

# Words that are never the *content* of a screen, only its chrome. A screen
# made of nothing but these has not finished drawing.
_CHROME_ONLY_WORDS = frozenset({
    "next", "back", "loading", "ok", "continue", "skip", "done", "cancel",
})


def submit_in_flight(text: str | None) -> bool:
    """Whether the screen's own submit button is busy.

    Instagram does not disable the button while it works -- it **renames it to
    `Loading`**. So a screen mid-submit still classifies as the screen it was,
    and a flow that re-taps `Next` finds no such button and concludes nothing
    is happening. That is exactly what cost the first real run its account:
    `Blank (1)`, 2026-08-13, gave up after four tries at a password screen
    whose clickable labels were `['••••••••••••', 'Password,', 'Learn more',
    'Loading', 'I already have an account', 'Back']` -- the password had gone
    through on the very first tap.

    Waiting is always right here: the button will either finish or the screen
    will change, and both are handled by looking again.
    """
    if not text:
        return False
    return bool(re.search(r"\bloading\b", text.lower()))


def _looks_like_loading(haystack: str) -> bool:
    """Whether this is a screen mid-render rather than one we cannot name.

    Deliberately narrow. "Short" alone is not enough -- an unrecognised screen
    can be short too, and calling it loading would wait out the run instead of
    stopping it with the text a person needs.
    """
    if any(marker in haystack for marker in _LOADING_MARKERS) and len(haystack) <= 200:
        return True
    words = [word for word in re.split(r"[^a-z]+", haystack) if word]
    return bool(words) and set(words) <= _CHROME_ONLY_WORDS


def classify_signup_screen(text: str | None) -> str:
    """Name the signup screen in `text`.

    Pure text in, label out, so the ordering is unit-testable without a phone.
    Anything unrecognised is `SCREEN_UNKNOWN` and **not** something benign: the
    run stops there rather than guessing, because a screen read as the wrong
    thing makes the loop confidently do the wrong thing.
    """
    if not text:
        return SCREEN_UNKNOWN
    haystack = text.lower()

    if ban_detection.classify_block_text(haystack) == ban_detection.KIND_BANNED:
        return SCREEN_BANNED

    # Before the markers: a home screen carries none of them, so it would fall
    # through to `unknown` and stop a run whose only problem is that Instagram
    # is not in front. Verification learned this the expensive way on `Jil 6`.
    if looks_like_launcher(haystack):
        return SCREEN_LAUNCHER

    for kind, markers in _ORDERED_MARKERS:
        if any(marker in haystack for marker in markers):
            return kind

    # Only once nothing asked us for anything: a working feed or profile means
    # the account exists. Checked last so a challenge can never be read as
    # success -- the same ordering rule verification uses.
    if screen_is_healthy(haystack) or any(marker in haystack
                                          for marker in _ACCOUNT_EXISTS_MARKERS):
        return SCREEN_DONE

    # A screen caught mid-render is not an unrecognised screen. Replaying the
    # 2026-08-13 recordings through this function found exactly two Instagram
    # screens it could not name -- `checking info…` and a bare `next` -- and
    # both were a dump taken while the real screen was still drawing. Stopping
    # a run on those would abandon accounts for no reason. Deliberately last,
    # so a screen with any real content on it can never be dismissed as
    # "still loading".
    if _looks_like_loading(haystack):
        return SCREEN_LOADING
    return SCREEN_UNKNOWN


# --- what the flow needs from a phone -----------------------------------------
@runtime_checkable
class SignupDriver(Protocol):
    """The device seam. `run_signup` never touches adb itself."""

    def read_screen(self) -> str: ...

    def tap_label(self, labels) -> bool:
        """Tap the clickable node whose label EQUALS one of `labels`.

        Exact, and it must tap the clickable *ancestor* when the text sits on a
        child -- several controls in this chain are `clickable=false` with a
        clickable parent.
        """

    def fill(self, hints, value: str, what: str,
             submits_itself: bool = False) -> bool:
        """Clear the field `hints` names, type `value`, and prove it landed.

        Both halves matter. A partial clear silently prepends whatever survived
        (a 14-backspace clear against a 21-character address produced
        `i1aikjg<ours>@gmail.com`, and Instagram mailed a code to it), and a
        fresh empty field can swallow or duplicate the first character.
        """

    def dismiss_keyboard(self) -> None:
        """Hide the IME. The floating keyboard covers the submit button and
        receives the tap, while the dump still reports the button as visible."""

    def set_date(self, day: int, month: str, year: int) -> bool:
        """Drive the Android date-picker spinner and confirm it."""


@dataclass
class Identity:
    """Everything one new account needs to be created with."""

    full_name: str
    username: str
    password: str
    birth_day: int
    birth_month: str
    birth_year: int

    def summary(self) -> str:
        return (f"{self.full_name} / @{self.username} / "
                f"{self.birth_day} {self.birth_month} {self.birth_year}")


RESULT_CREATED = "created"
RESULT_UNKNOWN_SCREEN = "unknown_screen"
RESULT_NO_NUMBER = "no_number"
RESULT_BANNED = "banned"
RESULT_STUCK = "stuck"
RESULT_PHONE_LOST = "phone_lost"
RESULT_ERROR = "error"


@dataclass
class SignupResult:
    status: str
    detail: str = ""
    username: str = ""
    steps: list = field(default_factory=list)
    numbers_used: int = 0
    phone_number: str = ""

    @property
    def ok(self) -> bool:
        return self.status == RESULT_CREATED


# The real chain is 13 screens; the rest of the budget covers a retried number
# and screens that repeat while something loads.
MAX_STEPS = 30

# Each number costs real money and about 150 seconds of waiting. The German
# pool delivers roughly one time in two or three, so three is a real budget
# rather than a generous one -- and a fourth rarely fixes what three could not.
MAX_NUMBER_ATTEMPTS = 3

# The same screen this many times running, with its handler claiming success,
# means the handler is not advancing anything.
MAX_REPEATS = 4

# How long to wait for an SMS before writing the number off.
CODE_WAIT_SECONDS = 150

# How many times a run will wait for a screen that is still drawing before
# calling it stuck. Bounded so a genuinely blank screen -- one appeared after a
# code was accepted and never rendered anything -- ends the run instead of
# spinning until the phone dies.
MAX_LOADING_WAITS = 8
LOADING_WAIT_SECONDS = 6

# Same reasoning as verification's: an app that was backgrounded is worth
# starting again, a phone that will not run Instagram at all is not.
MAX_APP_RESTARTS = 2

# Reads that come back empty before the phone is written off. A cloud phone
# that dies mid-run -- they last about fifteen minutes -- returns nothing at
# all, and calling that an unrecognised screen sends somebody looking for a
# marker list that does not exist. One empty read can be a screen mid-redraw;
# three in a row is the phone.
MAX_EMPTY_READS = 3

# Screens that are simply dismissed, and the exact labels that dismiss them.
# `Skip` on the permissions screen is what declines contacts sync -- which is
# the thing that would link these accounts to one another.
_SKIP_LABELS = ("Skip", "SKIP", "Not now", "NOT NOW", "Got it", "GOT IT",
                "Dismiss", "Cancel")

_SUBMIT_LABELS = ("Next", "NEXT", "Continue", "Done")


def run_signup(driver: SignupDriver, router, identity: Identity, logger=None,
               sleep=None) -> SignupResult:
    """Walk one account from "Join Instagram" to a working profile.

    `router` is an `SmsRouter`; numbers are leased lazily -- only once the
    screen that needs one is actually up, because a number starts ageing the
    moment it is bought.
    """
    import time as _time
    sleep = sleep or _time.sleep

    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)("signup: " + message, *args)

    steps: list = []
    lease = None
    numbers_used = 0
    last_screen = None
    repeats = 0
    loading_waits = 0
    app_restarts = 0
    empty_reads = 0
    done_flags = set()

    def release(count_failure: bool):
        nonlocal lease
        if lease is not None:
            try:
                lease.release(count_failure=count_failure)
            except Exception as exc:      # releasing must never lose the run
                log("warning", "releasing the number failed (%s)", exc)
            lease = None

    def finish(status, detail=""):
        release(status != RESULT_CREATED)
        return SignupResult(status=status, detail=detail,
                            username=identity.username, steps=steps,
                            numbers_used=numbers_used,
                            phone_number=(lease.e164 if lease else ""))

    try:
        for step in range(MAX_STEPS):
            text = driver.read_screen()

            # Nothing at all came back. That is the phone, not the screen.
            if not (text or "").strip():
                empty_reads += 1
                if empty_reads >= MAX_EMPTY_READS:
                    return finish(
                        RESULT_PHONE_LOST,
                        f"the phone stopped answering after {len(steps)} screen(s)"
                        f" -- these cloud phones last about fifteen minutes")
                log("warning", "the screen read as empty (%d/%d)",
                    empty_reads, MAX_EMPTY_READS)
                sleep(LOADING_WAIT_SECONDS)
                continue
            empty_reads = 0

            screen = classify_signup_screen(text)
            log("info", "step %d: %s", step + 1, screen)
            steps.append(screen)

            if screen == SCREEN_LAUNCHER:
                # Not a signup screen and not a verdict: Instagram is simply
                # not in front. Starting it again is nearly free.
                starter = getattr(driver, "restart_app", None)
                if app_restarts < MAX_APP_RESTARTS and callable(starter) and starter():
                    app_restarts += 1
                    log("info", "on the home screen; starting Instagram again "
                                "(%d/%d)", app_restarts, MAX_APP_RESTARTS)
                    steps.pop()
                    sleep(6)
                    continue
                return finish(RESULT_STUCK,
                              "Instagram is not running and would not start")

            # A screen whose submit button says `Loading` has already taken the
            # tap. Acting again finds no button, and four rounds of that is a
            # run that throws away a verified number.
            if screen not in (SCREEN_DONE, SCREEN_BANNED) and submit_in_flight(text):
                loading_waits += 1
                if loading_waits > MAX_LOADING_WAITS:
                    return finish(RESULT_STUCK,
                                  f"the {screen} screen was still working after "
                                  f"{MAX_LOADING_WAITS} waits")
                log("info", "the %s screen is still working; waiting (%d/%d)",
                    screen, loading_waits, MAX_LOADING_WAITS)
                steps.pop()
                sleep(LOADING_WAIT_SECONDS)
                continue

            if screen == SCREEN_LOADING:
                # Waiting is not a step: it must neither consume the step
                # budget's meaning nor trip the repeat guard.
                loading_waits += 1
                if loading_waits > MAX_LOADING_WAITS:
                    return finish(RESULT_STUCK,
                                  "the screen never finished drawing")
                log("info", "still drawing; waiting (%d/%d)",
                    loading_waits, MAX_LOADING_WAITS)
                steps.pop()
                sleep(LOADING_WAIT_SECONDS)
                continue
            loading_waits = 0

            if screen == last_screen:
                repeats += 1
                if repeats >= MAX_REPEATS:
                    return finish(RESULT_STUCK,
                                  f"{screen} did not advance in {MAX_REPEATS} tries")
            else:
                repeats = 0
            last_screen = screen

            if screen == SCREEN_DONE:
                log("info", "the account exists: @%s", identity.username)
                release(False)
                return SignupResult(status=RESULT_CREATED, username=identity.username,
                                    steps=steps, numbers_used=numbers_used,
                                    phone_number="")

            if screen == SCREEN_BANNED:
                return finish(RESULT_BANNED, "disabled on creation")

            if screen == SCREEN_UNKNOWN:
                # The whole safety property of this flow. Today's run proved a
                # misread screen produces a confident wrong action, so an
                # unnamed screen ends the run and keeps its text for a person.
                return finish(RESULT_UNKNOWN_SCREEN, (text or "")[:400])

            if screen == SCREEN_ENTRY:
                driver.tap_label(("Get started",))

            elif screen == SCREEN_EMAIL:
                # Signup defaults to phone; if we somehow landed here, go back
                # to the number, which is the path this flow can actually
                # complete.
                driver.tap_label(("Sign up with mobile number",))

            elif screen == SCREEN_PHONE:
                if lease is None:
                    if numbers_used >= MAX_NUMBER_ATTEMPTS:
                        return finish(RESULT_NO_NUMBER,
                                      f"{numbers_used} numbers, none delivered")
                    lease = router.lease()
                    numbers_used += 1
                    log("info", "number %d: %s", numbers_used, lease.e164)
                # The national part only: the picker is already on DE +49.
                if not driver.fill(("mobile", "phone", "number"),
                                   lease.typed_number, "mobile number"):
                    release(False)
                    continue
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(6)

            elif screen == SCREEN_CODE:
                if lease is None:
                    log("warning", "code screen with no number; going back")
                    driver.tap_label(("Back",))
                    sleep(4)
                    continue
                code = lease.wait_for_code(timeout=CODE_WAIT_SECONDS)
                if not code:
                    log("warning", "no code for %s; swapping the number", lease.e164)
                    release(True)
                    driver.tap_label(("Back",))
                    sleep(4)
                    continue
                log("info", "code arrived")
                driver.fill(("code",), code, "confirmation code",
                            submits_itself=True)
                release(False)
                # The field auto-submits on the sixth digit, so a submit tap is
                # a bonus rather than a requirement.
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(10)

            elif screen == SCREEN_PASSWORD:
                if "password" not in done_flags:
                    driver.fill(("password",), identity.password, "password")
                    done_flags.add("password")
                    driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(6)

            elif screen == SCREEN_BIRTHDAY:
                if "birthday" not in done_flags:
                    # Opening the picker is what the field tap does; the picker
                    # itself is the next screen.
                    if not driver.tap_label(_SUBMIT_LABELS):
                        driver.tap_label(("Date of birth",))
                    done_flags.add("birthday")
                else:
                    driver.tap_label(_SUBMIT_LABELS)
                sleep(5)

            elif screen == SCREEN_DATE_PICKER:
                driver.set_date(identity.birth_day, identity.birth_month,
                                identity.birth_year)
                sleep(5)

            elif screen == SCREEN_NAME:
                driver.fill(("name",), identity.full_name, "full name")
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(6)

            elif screen == SCREEN_USERNAME:
                # Arrives holding Instagram's own suggestion, which is why this
                # is a fill and not a "tap Next".
                driver.fill(("username",), identity.username, "username")
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(8)

            elif screen == SCREEN_TERMS:
                log("info", "agreeing -- this is the tap that creates the account")
                driver.tap_label(("I agree",))
                sleep(12)

            elif screen == SCREEN_SAVE_PASSWORD:
                # Back, not a label: the button is "NOT NOW" the first time and
                # "NEVER" afterwards.
                driver.dismiss_keyboard()
                sleep(3)

            elif screen in (SCREEN_PERMISSIONS, SCREEN_PHOTO_PROMPT, SCREEN_FOLLOW,
                            SCREEN_ADD_EMAIL, SCREEN_INTERSTITIAL):
                # All declined. Contacts sync links these accounts together;
                # the photo and the first post are the human hand-off; and the
                # email prompt arrives holding the phone's own Google account.
                if not driver.tap_label(_SKIP_LABELS):
                    log("warning", "nothing to skip on %s", screen)
                    driver.dismiss_keyboard()
                sleep(5)

            sleep(2)

        return finish(RESULT_STUCK, f"still going after {MAX_STEPS} screens")
    except Exception as exc:
        log("warning", "run failed (%s)", exc)
        release(True)
        return SignupResult(status=RESULT_ERROR, detail=str(exc)[:200],
                            username=identity.username, steps=steps,
                            numbers_used=numbers_used)
