"""Work a flagged account through Instagram's verification chain.

Instagram does not present one verification screen, it presents a *chain* of
them, and the order is not fixed: an account may be asked for a phone number,
then a code, then a photo; another gets the photo first; another gets a "how do
you want to get the code?" chooser in front of everything. Writing this as a
fixed sequence of steps would work for exactly one of those accounts.

So this module is a loop, not a script:

    read the screen -> name what is on it -> do the one thing that screen needs
                    -> read the screen again

Each pass is independent. Nothing assumes what came before, which is what makes
the order irrelevant, and it also means a screen appearing twice (Instagram
re-asking for a code after a resend) is handled by the same code that handled it
the first time.

**Where the SMS number comes from.** `clients/sms/` rents it, and the lease is
held across screens: the number is typed on one screen and the code arrives for
the code screen after it. A code that never arrives inside 45 seconds refunds the
number, counts a failure toward the provider circuit breaker, and the loop asks
for another number -- from the other provider once the breaker has tripped.

**The image captcha.** "Type the characters you see" is usually the *first*
screen a flagged account shows, so it gates everything else. `clients/captcha.py`
sends it to 2captcha and types the answer back. When an answer is rejected --
which shows up as the captcha screen simply coming round again -- the run
reports it back to the service (refunding that solve) before trying once more.
With no captcha key configured the challenge ends the run as `needs_human`,
which is the state the profile was already in.

**The device half is a seam.** Everything below drives a `ChallengeDriver` --
read the screen, type in a field, tap the button, upload a photo. The
orchestration in this module is pure logic and is unit-tested with a fake
driver; the real one is `flows/verification_driver.AdbChallengeDriver`, and
`automation/verification_probe.py` is how it gets pointed at a live profile.

**What has actually been seen.** The phone, code, banned and signed-out screens
have been read off real flagged phones and are pinned as fixtures in the tests.
The photo, image-captcha and method-chooser markers below are still general
knowledge of Instagram's wording rather than this fleet's screens. Nothing has
yet *solved* a challenge end to end -- see TODO_2026-08-12.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from adb_bot.automation import ban_detection

# --- what is on screen --------------------------------------------------------
CHALLENGE_NONE = "none"                     # no challenge visible -- we are through
CHALLENGE_CHOOSE_METHOD = "choose_method"   # "how do you want to get the code?"
CHALLENGE_PHONE = "phone"                   # asks for a phone number
CHALLENGE_CODE = "code"                     # asks for the code it just sent
CHALLENGE_PHOTO = "photo"                   # asks for a photo to prove a human
CHALLENGE_IMAGE_CAPTCHA = "image_captcha"   # asks for letters/digits in an image
CHALLENGE_BANNED = "banned"                 # not a challenge; the account is gone
CHALLENGE_SIGNED_OUT = "signed_out"         # not a challenge; nobody is logged in

# --- how the run ended --------------------------------------------------------
RESULT_SOLVED = "solved"            # the chain is cleared
RESULT_NEEDS_HUMAN = "needs_human"  # a step we cannot do (captcha, or numbers exhausted)
RESULT_BANNED = "banned"            # account disabled -- stop, flag it
RESULT_STUCK = "stuck"              # the same screen kept coming back
RESULT_FAILED = "failed"            # a step the driver could not perform
RESULT_SIGNED_OUT = "signed_out"    # nobody is logged in; there is nothing to verify

# --- screen markers -----------------------------------------------------------
# Matched against *visible* screen text only (element `text` / `content-desc`,
# or OCR) -- never the raw XML of a UI dump. Matching the raw dump is how 17 of
# 29 profiles were once wrongly flagged as needing verification: Instagram ships
# resource ids containing words like "confirm" and "verification" on ordinary
# screens, so any marker below would match a perfectly healthy feed.
#
# TODO_2026-08-12 §3: **confirmed** against real dumps -- the phone, code and
# signed-out lists, plus the ban path. **Still guesses** -- `_PHOTO_MARKERS`,
# `_IMAGE_CAPTCHA_MARKERS` and `_CHOOSE_METHOD_MARKERS`, which no real screen
# has yet exercised. The captcha one matters most: it is reportedly the first
# screen a flagged account shows, so a hole there gates everything behind it.
# A phrase that never appears is dead weight; a real screen that classifies as
# CHALLENGE_NONE is a hole the loop walks straight past.
# `verification_probe.py --sweep` collects the dumps; add a fixture per
# confirmed screen to tests/test_verification_flow.py (see `RealScreenTest`).
_CHOOSE_METHOD_MARKERS = (
    "how do you want to get",
    "choose how to",
    "where should we send",
    "get a code",
    "send code to",
    "choose a way to confirm",
)

# The phone and code screens cannot be told apart by category order, because
# each one contains the other's words: the phone screen says "enter your mobile
# number to get a confirmation code", and the code screen says "enter the code
# we sent to your phone number +1...". Getting this backwards is expensive in
# both directions -- reading the code screen as a phone screen throws away a
# number that is seconds from receiving, and reading the phone screen as a code
# screen waits 45 seconds for an SMS nobody ever asked for.
#
# So each has *strong* markers, which name the action being asked for and only
# ever appear on that screen, and *weak* ones, which are just the topic and can
# appear on either. Strong markers decide; weak markers only break a tie when no
# strong marker matched at all.
_PHONE_STRONG_MARKERS = (
    "enter your mobile number",
    "enter your phone number",
    "enter a mobile number",
    "enter a phone number",
    "add your phone number",
    "add phone number",
    "add a phone number",
    "confirm your phone number",
)

_CODE_STRONG_MARKERS = (
    "enter the code",
    "enter confirmation code",
    "enter the confirmation code",
    "enter the 6-digit code",
    "enter 6-digit code",
    "enter the security code",
    "enter the verification code",
    "we sent a code",
    "we sent you a code",
    "didn't get the code",
    "resend code",
)

_PHONE_WEAK_MARKERS = (
    "mobile number",
    "phone number",
)

_CODE_WEAK_MARKERS = (
    "confirmation code",
    "security code",
    "verification code",
)

_PHOTO_MARKERS = (
    "take a photo of yourself",
    "video selfie",
    "upload a photo",
    "upload a picture",
    "photo of yourself",
    "confirm your identity with a photo",
    "we need a photo",
    "take a selfie",
)

# Instagram's signed-out welcome screen. Confirmed from a real dump of `Jil 2`
# on 2026-08-11 -- the only marker list here that is not a guess.
#
# This matters far more than it looks. A signed-out phone shows no verification
# screen at all, so without these the loop reads it as "nothing left to answer"
# and reports SOLVED. Wired to a runner that clears the MultiLogin `Issue` tag
# on success, that would quietly hand every logged-out profile back to the
# posting loop as if a person had fixed it. The `Issue` tag is applied by hand
# and covers several different problems -- the remarks on the tagged profiles
# include "log in again", "disable" and "no ig account" as well as "human
# verification" -- so this flow WILL meet signed-out accounts routinely.
#
# Split strong/weak for the same reason the phone and code screens are: the
# strong ones were read off a real signed-out phone and cannot appear anywhere
# else, while the weak ones are plausible login-surface wording that a genuine
# challenge screen might also carry ("Forgot password?" sits under plenty of
# forms). Strong decides immediately; weak only decides when no challenge
# marker matched at all, so a real challenge is never thrown away over a
# stray password link. Bare "log in" is in neither: it appears on ordinary
# screens too.
_SIGNED_OUT_STRONG_MARKERS = (
    "join instagram",
    "i already have a profile",
    "create new account",
    "sign up with email",
)

_SIGNED_OUT_WEAK_MARKERS = (
    "log in with facebook",
    "log into another account",
    "forgot password",
)

_IMAGE_CAPTCHA_MARKERS = (
    "type the characters",
    "enter the characters",
    "characters you see",
    "letters and numbers you see",
    "type the letters",
    "enter the text you see",
    "solve the puzzle",
)

# Most specific first. The captcha and photo screens are unambiguous, so they
# lead; then the two strong sets; then the chooser, whose wording ("get a code")
# is broad enough to match a code screen if it were checked earlier; then the
# weak topic words as a last resort.
#
# Code beats phone within each tier: when a screen really is ambiguous, waiting
# on the number already typed costs 45 seconds, while renting another one costs
# money and abandons a number that may be about to receive.
#
# The strong signed-out markers lead: they name a surface that cannot also be a
# challenge, and matching them early is what stops a number being rented for a
# phone nobody is logged into. The weak ones trail everything.
_ORDERED_MARKERS = (
    (CHALLENGE_SIGNED_OUT, _SIGNED_OUT_STRONG_MARKERS),
    (CHALLENGE_IMAGE_CAPTCHA, _IMAGE_CAPTCHA_MARKERS),
    (CHALLENGE_PHOTO, _PHOTO_MARKERS),
    (CHALLENGE_CODE, _CODE_STRONG_MARKERS),
    (CHALLENGE_PHONE, _PHONE_STRONG_MARKERS),
    (CHALLENGE_CHOOSE_METHOD, _CHOOSE_METHOD_MARKERS),
    (CHALLENGE_CODE, _CODE_WEAK_MARKERS),
    (CHALLENGE_PHONE, _PHONE_WEAK_MARKERS),
    (CHALLENGE_SIGNED_OUT, _SIGNED_OUT_WEAK_MARKERS),
)


def classify_challenge(text: str | None) -> str:
    """Name the verification screen in `text`, or `CHALLENGE_NONE`.

    `text` must be lowercased *visible* screen text -- see the note on the
    markers above. Pure text in, label out, so the whole ordering question is
    unit-testable without a phone.
    """
    if not text:
        return CHALLENGE_NONE
    haystack = text.lower()

    # A disabled account can show a "confirm it's you" screen it will never let
    # anyone past, so the ban check wins over every challenge marker.
    if ban_detection.classify_block_text(haystack) == ban_detection.KIND_BANNED:
        return CHALLENGE_BANNED

    for kind, markers in _ORDERED_MARKERS:
        if any(marker in haystack for marker in markers):
            return kind
    return CHALLENGE_NONE


# --- the device seam ----------------------------------------------------------
@runtime_checkable
class ChallengeDriver(Protocol):
    """Everything the loop needs a phone to do.

    Implemented for real by `flows/verification_driver.AdbChallengeDriver`, and
    as a fake in the tests. The driver exists but **its selectors have never met
    a real challenge screen** -- confirming them is TODO_2026-08-12.md section 3.
    The house rule from `interruptions.py` applies throughout: tap buttons only
    on an EXACT label match taken from a UI dump, never from OCR.

    Every method returns a bool for "did that work", never raises for an
    ordinary failure, so the loop can decide what a failed step means.
    """

    def read_screen(self) -> str:
        """Lowercased visible text of the current screen ('' if unreadable)."""

    def refresh_feed(self) -> bool:
        """Pull the feed down, to make Instagram serve a challenge it withheld.

        Optional: `_confirm_clear` calls it through `getattr` so a caller can
        supply a driver without one. False means it was not done (a dry run, or
        the screen size could not be read), and the caller must not then treat
        the next read as a post-refresh answer.
        """

    def choose_sms_method(self) -> bool:
        """On the chooser, pick the SMS/text-message option."""

    def enter_phone(self, number: str) -> bool:
        """Type `number` into the phone field and submit it."""

    def enter_code(self, code: str) -> bool:
        """Type `code` into the confirmation field and submit it."""

    def request_new_number(self) -> bool:
        """Get back from the code screen to the phone screen to try again.

        Instagram spells this differently per surface ("Change number", "I didn't
        get the code" then "Change number", or simply Back), which is why it is
        one call here rather than the loop guessing at buttons.
        """

    def upload_photo(self) -> bool:
        """Satisfy the photo challenge by uploading a picture from the device.

        Open question before this can be written: *which* picture. Nothing in
        this repo currently owns a photo of a person, the model's media folder
        is the obvious source but a reel frame may not pass, and the wrong face
        on the wrong account is worse than failing the step
        (TODO_2026-08-12 §5.1). Returning
        False here is a legitimate outcome -- it leaves the profile to a human.
        """

    def capture_captcha_image(self) -> str | None:
        """Screenshot + crop the captcha image; return a local PNG path."""

    def enter_captcha(self, text: str) -> bool:
        """Type the solved characters and submit."""


@dataclass
class VerificationResult:
    """What happened, in enough detail for the Airtable note and the log."""

    status: str
    detail: str = ""
    steps: list = field(default_factory=list)      # challenges handled, in order
    numbers_used: int = 0                          # numbers rented this run
    code_received: bool = False

    @property
    def ok(self) -> bool:
        return self.status == RESULT_SOLVED


# How many screens one run will work through before giving up. The longest real
# chain seen is chooser -> phone -> code -> photo; the rest of the budget is for
# retried numbers and screens that repeat.
MAX_STEPS = 14

# How many numbers one account gets before the run hands back to a human. Each
# one costs 45 seconds and real money, and an account that has burned three
# numbers is usually being refused for a reason a fourth will not fix.
MAX_NUMBER_ATTEMPTS = 3

# The same screen this many times in a row, with the step reporting success each
# time, means the step is not actually advancing anything.
MAX_REPEATS = 3

# How long a clear screen is doubted before it is believed, and how often it is
# re-read in that window.
#
# Instagram opens on the feed and drops the challenge in afterwards -- often
# several seconds afterwards. A single read taken in that gap says "none", and
# `run` reads "none" as *solved*, so the run would report success on an account
# it never looked at. That is the same shape as the two false-clear bugs this
# fleet has already paid for: a feed check that counted any Instagram activity
# as a healthy feed, and `Blank (24)`, whose "we disabled your account" screen
# only appeared on the second look a few seconds later.
CLEAR_SCREEN_PATIENCE_SECONDS = 15.0
CLEAR_SCREEN_POLL_SECONDS = 3.0


def run_verification(driver: ChallengeDriver, router, solver=None, logger=None,
                     max_steps: int = MAX_STEPS,
                     max_number_attempts: int = MAX_NUMBER_ATTEMPTS,
                     service: str = "instagram", country: str | None = None,
                     sleep=None, clock=None) -> VerificationResult:
    """Drive one account through whatever verification screens it shows.

    `router` is an `SmsRouter`; `solver` a `CaptchaSolver` (defaults to the
    unconfigured one). Returns rather than raises for every outcome the caller
    can act on -- the caller's job is to write the result to Airtable, and an
    exception there would just lose it.

    TODO_2026-08-12 §4.1: no *loop* calls this yet -- only `verification_probe.py --apply`,
    by hand. It needs a runner that picks flagged profiles, takes the profile
    lock, launches, runs this, and writes the result back; `recovery_runner.py`
    is the closest existing shape. On `solved` the MLX `Issue` tag comes off
    (issue_tags.py); on `banned` the ban state goes on (incidents.py); on
    `signed_out` the tag must stay exactly where it is.
    """
    if solver is None:
        from adb_bot.clients.captcha import build_solver
        solver = build_solver(logger=logger)
    if country is None:
        # Resolved here rather than as a default argument so the SMS layer stays
        # the single owner of which country the fleet rents from.
        from adb_bot.clients.sms.base import DEFAULT_COUNTRY
        country = DEFAULT_COUNTRY

    session = _Session(driver, router, solver, logger, max_number_attempts,
                       service, country, sleep=sleep, clock=clock)
    try:
        return session.run(max_steps)
    finally:
        # A number rented but never confirmed must go back, whatever ended the
        # run -- including an exception on the phone half.
        session.release_lease()


class _Session:
    """The loop's mutable state. Split out so `run_verification` stays readable."""

    def __init__(self, driver, router, solver, logger, max_number_attempts,
                 service, country, sleep=None, clock=None) -> None:
        self.driver = driver
        self.router = router
        self.solver = solver
        self.logger = logger
        self.max_number_attempts = max_number_attempts
        self.service = service
        self.country = country
        # Injected so the waiting in `_confirm_clear` is testable. A test that
        # really slept through it would add half a minute per case, which is
        # how waits end up untested.
        self.sleep = sleep or time.sleep
        self.sleep_clock = clock or time.monotonic

        self.lease = None            # the number currently typed into Instagram
        self.numbers_used = 0
        self.code_received = False
        self.steps: list = []
        self._last_challenge = None
        self._repeats = 0
        self._captcha_answered = False

    # --- main loop ------------------------------------------------------------
    def run(self, max_steps: int) -> VerificationResult:
        for _ in range(max_steps):
            challenge = classify_challenge(self.driver.read_screen())

            if challenge == CHALLENGE_NONE:
                # Never believed on the first read -- see `_confirm_clear`. If a
                # challenge is merely late, this is where it is caught; if the
                # screen is genuinely clear, this returns none and we are done.
                challenge = self._confirm_clear()
            if challenge == CHALLENGE_NONE:
                # Nothing left to answer. If we were mid-chain this is success;
                # if we never saw a challenge at all, it is also success -- the
                # profile was flagged but the screen is clear now.
                return self._result(RESULT_SOLVED,
                                    "no verification screen remaining")
            if challenge == CHALLENGE_BANNED:
                return self._result(RESULT_BANNED,
                                    "account is disabled, not verifiable")
            if challenge == CHALLENGE_SIGNED_OUT:
                # Nobody is logged in, so there is no challenge to answer and
                # nothing this flow can do. Reported separately from
                # `needs_human` on purpose: the fix is credentials, not a
                # captcha, and a caller must never read this as success.
                return self._result(
                    RESULT_SIGNED_OUT,
                    "nobody is logged into Instagram on this phone -- it needs "
                    "an account signed in, not verification")

            if not self._note_progress(challenge):
                return self._result(
                    RESULT_STUCK,
                    f"the {challenge} screen kept coming back unchanged")

            outcome = self._handle(challenge)
            if outcome is not None:
                return outcome

        return self._result(RESULT_STUCK,
                            f"gave up after {max_steps} screens")

    def _confirm_clear(self) -> str:
        """Re-read a screen that looked clear. Returns what it settled on.

        A clear screen is the one classification we must not take at face
        value, because it is the one that ends the run as *success*. Instagram
        opens on the feed and the challenge arrives after it, so a read taken
        in that gap is indistinguishable from a healthy account -- and the
        account we were sent to look at is, by definition, one somebody flagged.

        Two rounds of doubt, in increasing order of intrusiveness:

        1. Wait, re-reading, for `CLEAR_SCREEN_PATIENCE_SECONDS`. Costs nothing
           but time and catches a challenge that is merely slow.
        2. Pull the feed down and look again. Instagram serves the challenge on
           a refresh when it did not serve it on the open.

        The refresh is only tried when nothing has been answered yet. Mid-chain
        a clear screen means the step we just completed worked, and a swipe on
        a challenge screen we have not recognised could dismiss or scroll it --
        buying nothing, since patience alone already covers a slow redraw.
        """
        deadline = self.sleep_clock() + CLEAR_SCREEN_PATIENCE_SECONDS
        while self.sleep_clock() < deadline:
            self.sleep(CLEAR_SCREEN_POLL_SECONDS)
            challenge = classify_challenge(self.driver.read_screen())
            if challenge != CHALLENGE_NONE:
                self._log("info", "a %s screen appeared after the first read looked "
                                  "clear -- this is why a clear screen is not believed "
                                  "immediately", challenge)
                return challenge

        if self.steps:
            return CHALLENGE_NONE

        # `refresh_feed` is optional on the driver: a caller may supply a
        # simpler one, and a missing refresh must not turn a working run into
        # an AttributeError.
        refresh = getattr(self.driver, "refresh_feed", None)
        if not callable(refresh) or not refresh():
            return CHALLENGE_NONE

        self._log("info", "screen still looked clear after %.0fs; pulled the feed "
                          "down and looking again", CLEAR_SCREEN_PATIENCE_SECONDS)
        deadline = self.sleep_clock() + CLEAR_SCREEN_PATIENCE_SECONDS
        while self.sleep_clock() < deadline:
            challenge = classify_challenge(self.driver.read_screen())
            if challenge != CHALLENGE_NONE:
                self._log("info", "a %s screen appeared after the refresh", challenge)
                return challenge
            self.sleep(CLEAR_SCREEN_POLL_SECONDS)
        return CHALLENGE_NONE

    def _handle(self, challenge: str):
        """Do the one thing this screen needs. Return a result to stop, or None."""
        if challenge == CHALLENGE_CHOOSE_METHOD:
            if not self.driver.choose_sms_method():
                return self._result(RESULT_FAILED,
                                    "could not choose the SMS option")
            return None

        if challenge == CHALLENGE_PHONE:
            return self._handle_phone()

        if challenge == CHALLENGE_CODE:
            return self._handle_code()

        if challenge == CHALLENGE_PHOTO:
            if not self.driver.upload_photo():
                return self._result(RESULT_NEEDS_HUMAN,
                                    "the photo challenge could not be completed")
            return None

        if challenge == CHALLENGE_IMAGE_CAPTCHA:
            return self._handle_image_captcha()

        return self._result(RESULT_FAILED, f"unhandled challenge {challenge!r}")

    # --- individual screens ---------------------------------------------------
    def _handle_phone(self):
        if self.numbers_used >= self.max_number_attempts:
            return self._result(
                RESULT_NEEDS_HUMAN,
                f"{self.numbers_used} numbers were refused or never received")

        # Any number already typed is spent -- Instagram is asking again.
        self.release_lease()

        try:
            self.lease = self.router.lease(service=self.service,
                                           country=self.country)
        except Exception as exc:
            # Includes an empty provider wallet and "every provider refused".
            # Both need a person, and neither is the account's fault.
            return self._result(RESULT_NEEDS_HUMAN,
                                f"could not rent a number: {exc}")

        self.numbers_used += 1
        self._check_country_picker()
        if not self.driver.enter_phone(self.lease.typed_number):
            return self._result(RESULT_FAILED, "could not enter the phone number")
        return None

    def _check_country_picker(self) -> None:
        """Warn when the on-screen country does not match the rented number.

        Instagram's phone box holds only the national part; the country picker
        beside it supplies the prefix. A real challenge screen on this fleet
        (`Blank (10)`, 2026-08-11) was set to `DE +49` while the router rents US
        numbers by default -- and nothing anywhere would have said so. The
        submitted number is simply wrong, no code ever arrives, the lease times
        out, and the breaker counts it as the provider's fault. Ten of those in
        a row switch providers over a problem no provider has.

        This only reports. Changing the picker is a device action that needs a
        real screen to design against, and renting to match is a decision about
        which country these accounts should use at all.
        """
        read = getattr(self.driver, "read_country_code", None)
        if not callable(read) or self.lease is None:
            return
        try:
            on_screen = read()
        except Exception:
            return
        expected = getattr(self.lease.order, "country_code", None)
        if not on_screen or not expected:
            return
        if str(on_screen).lstrip("+") != str(expected).lstrip("+"):
            self._log("warning",
                      "verification: the phone screen's country picker is set to "
                      "+%s but the rented number is +%s. The number submitted "
                      "will not be the one that was rented, so no code can "
                      "arrive -- this is not a provider problem.",
                      on_screen, expected)

    def _handle_code(self):
        if self.lease is None:
            # The code screen without a number we rented: Instagram is asking
            # about a number already on the account, and we cannot read its SMS.
            return self._result(
                RESULT_NEEDS_HUMAN,
                "a code was requested for a number the bot does not control")

        code = self.lease.wait_for_code()
        if code:
            self.code_received = True
            if not self.driver.enter_code(code):
                return self._result(RESULT_FAILED, "could not enter the code")
            self.release_lease()          # finishes the order: its code was used
            return None

        # No code inside the budget. The lease already refunded the number and
        # counted the failure against the provider; get back to the phone screen
        # so the next pass rents a fresh one -- from the fallback provider if
        # that failure was the one that tripped the breaker.
        self.lease = None
        if self.numbers_used >= self.max_number_attempts:
            return self._result(
                RESULT_NEEDS_HUMAN,
                f"no code arrived for {self.numbers_used} numbers")
        if not self.driver.request_new_number():
            return self._result(RESULT_FAILED,
                                "could not get back to the phone number screen")
        # Asking again is progress, even though the screen name repeats.
        self._repeats = 0
        return None

    def _handle_image_captcha(self):
        # Seeing this screen again after we typed an answer means the answer was
        # wrong. Tell the service before asking it for another one: that refunds
        # the bad solve and is the only feedback keeping its accuracy honest.
        if self._captcha_answered:
            self._report_bad_captcha()

        image = self.driver.capture_captcha_image()
        if not image:
            return self._result(RESULT_NEEDS_HUMAN,
                                "the captcha image could not be captured")

        solved = self.solver.solve_text(image)
        if not solved:
            return self._result(
                RESULT_NEEDS_HUMAN,
                "the image captcha could not be solved (no solver configured, "
                "or the service could not read it)")

        self._captcha_answered = True
        if not self.driver.enter_captcha(solved):
            return self._result(RESULT_FAILED, "could not enter the captcha text")
        return None

    def _report_bad_captcha(self) -> None:
        report = getattr(self.solver, "report_incorrect", None)
        if not report:
            return
        try:
            if report():
                self._log("info", "verification: reported the last captcha "
                                  "answer as incorrect")
        except Exception as exc:
            self._log("warning", "verification: reporting a bad captcha raised (%s)",
                      exc)

    # --- bookkeeping ----------------------------------------------------------
    def _note_progress(self, challenge: str) -> bool:
        """Track repeats. False means this screen is not advancing."""
        if challenge == self._last_challenge:
            self._repeats += 1
        else:
            self._last_challenge = challenge
            self._repeats = 0
        self.steps.append(challenge)
        return self._repeats < MAX_REPEATS

    def release_lease(self) -> None:
        """Settle any held number: finished if its code was used, refunded if not."""
        if self.lease is None:
            return
        lease, self.lease = self.lease, None
        try:
            lease.release()
        except Exception as exc:
            self._log("warning", "verification: releasing %s raised (%s)",
                      lease.order, exc)

    def _result(self, status: str, detail: str) -> VerificationResult:
        level = "info" if status == RESULT_SOLVED else "warning"
        self._log(level, "verification: %s (%s)", status, detail)
        return VerificationResult(
            status=status, detail=detail, steps=list(self.steps),
            numbers_used=self.numbers_used, code_received=self.code_received)

    def _log(self, level: str, message: str, *args) -> None:
        if self.logger is None:
            return
        handler = getattr(self.logger, level, None)
        if handler:
            handler(message, *args)
