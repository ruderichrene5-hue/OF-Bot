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

**The image captcha.** "Confirm you're human" is usually the *first* screen a
flagged account shows, so it gates everything else. `clients/captcha.py` sends
it to 2captcha and types the answer back. When an answer is rejected -- which
shows up as the captcha screen simply coming round again -- the run reports it
back to the service (refunding that solve) before trying once more. With no
captcha key configured the challenge ends the run as `needs_human`, which is
the state the profile was already in.

The image itself does not always render: `Laila 3` held this screen for 90
seconds with the image node present, correctly sized and pure white. There is
nothing to re-read in that case, so the run taps the screen's own `Get a new
code` link for a fresh image, and gives up as `needs_human` rather than paying
for a solve of a blank rectangle.

**The device half is a seam.** Everything below drives a `ChallengeDriver` --
read the screen, type in a field, tap the button, upload a photo. The
orchestration in this module is pure logic and is unit-tested with a fake
driver; the real one is `flows/verification_driver.AdbChallengeDriver`, and
`automation/verification_probe.py` is how it gets pointed at a live profile.

**What has actually been seen.** The phone, code, image-captcha, banned and
signed-out screens have been read off real flagged phones and are pinned as
fixtures in the tests. The photo and method-chooser markers below are still
general knowledge of Instagram's wording rather than this fleet's screens.

The chain **has** been solved end to end -- `Laila 4`, 2026-08-12, phone ->
code -> un-suspended. See TODO_2026-08-12.md for that run's numbers.
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
CHALLENGE_CONSENT = "consent"               # Meta consent / onboarding gate
CHALLENGE_VERIFY_INTRO = "verify_intro"     # "confirm you're human" -- press Continue
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
# TODO_2026-08-12 §3: **confirmed** against real dumps -- the phone, code,
# image-captcha and signed-out lists, plus the ban path. **Still guesses** --
# `_PHOTO_MARKERS` and `_CHOOSE_METHOD_MARKERS`, which no real screen has yet
# exercised. A phrase that never appears is dead weight; a real screen that
# classifies as the *wrong* challenge is worse than one that classifies as
# none, and the captcha proved it: its real wording ("enter the code from the
# image") matched the SMS code markers, so the loop would have waited for a
# text nobody asked for.
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
    # `Jil 3`, 2026-08-12 -- an in-app error dialog rather than the signed-out
    # welcome screen, and the reason it is here at all: it matched no marker,
    # so it would have classified as `none` and been reported as SOLVED,
    # untagging a logged-out account back into the posting loop. The
    # positive-health check caught it and handed over the text; these markers
    # are that text. Same result as the welcome screen -- it needs credentials,
    # not verification -- which is also why it is terminal for the runner.
    "you've been logged out",
    "youve been logged out",
    "the account owner may have changed the password",
)

_IMAGE_CAPTCHA_MARKERS = (
    # Confirmed off a real screen -- `Laila 3`, 2026-08-12. Instagram's wording
    # here is nothing like the general knowledge below it, and the difference
    # was not cosmetic: this screen says **"enter the code from the image"**,
    # which contains `_CODE_STRONG_MARKERS`' "enter the code", so with none of
    # these matching it classified as the *SMS code* screen. The loop would
    # then have sat waiting for a text nobody had asked for, on a screen with
    # no phone number anywhere in the chain.
    # NOT "confirm you're human" on its own -- see `_VERIFY_INTRO_MARKERS`.
    # `Katherine 8` (2026-08-12) showed a screen headed exactly that with no
    # image and no answer box, only a Continue button, and this list matched
    # it: the run cropped the whole screenshot, paid 2captcha to read it (it
    # said "jkhgkjughu"), and then failed with nowhere to type the answer.
    # Every marker here now names the *code-entry* captcha specifically.
    "code from the image",
    "can't read this text",
    "hear this code",
    # General knowledge, still unconfirmed on this fleet. Kept because
    # Instagram words this screen differently across surfaces and a phrase that
    # never appears costs nothing, while a missing one is a hole the loop walks
    # straight past.
    "type the characters",
    "enter the characters",
    "characters you see",
    "letters and numbers you see",
    "type the letters",
    "enter the text you see",
    "solve the puzzle",
)

# --- what a *working* Instagram looks like ------------------------------------
# The counterpart to every marker list above, and the one that decides whether a
# run reports success. Everything above answers "which challenge is this?"; a
# screen matching none of them was, until now, taken as "no challenge, so we are
# through" -- and `run` returned SOLVED.
#
# The recordings say that is three different screens, not one. Of the runs saved
# under ~/.adb_bot/verification, screens carrying no challenge marker included:
#
#   * a healthy feed -- genuinely fine;
#   * the **Android launcher** ("search gallery play store home telephone
#     messaging music chrome camera") -- Instagram was not even running, and
#     `Jil 2` sat there for two minutes;
#   * Meta's **ads-consent gate** -- a real blocker, whose only button is
#     `Get started`, seen on `Jil 20`;
#   * an **empty read** -- the dump returned nothing at all.
#
# Reported as SOLVED and wired to a runner that untags on success, each of those
# hands a profile that nobody fixed back to the posting loop. So success now
# needs *positive* evidence, and anything unrecognised is a person's problem
# rather than a silent pass. The markers are taken verbatim from real dumps.
_APP_HEALTHY_MARKERS = (
    # Instagram's bottom navigation bar, in `content-desc`. Present on the feed,
    # reels, search and profile, so it covers wherever the app happens to be.
    "search and explore",
    "home reels message",
    # The feed's story tray, on every healthy feed dump we have.
    "add to story",
    "reels tray container",
    # The screen Instagram shows once a challenge is cleared -- confirmed twice
    # (`Laila 4` and `Laila 3`, 2026-08-12). This is what success actually looks
    # like at the end of a chain, and it is not a feed.
    "you're back on instagram",
    "no longer suspended",
)

# Meta's consent / onboarding gates. Not verification challenges and not
# healthy screens: they block the app until answered.
#
# These were deliberately left to a person until 2026-08-12, when they turned
# out to be ~20% of the flagged blanks and the owner decided: **approve
# anything that costs nothing.** `interruptions` already does exactly that and
# is what clears them -- including picking `Use free of charge with ads` on the
# subscription screen, so approving never buys anything.
#
# Kept as a list here as well as in `interruptions` because this module has to
# *recognise* the screen to route it; that one knows how to *tap* it.
# The screen that introduces a challenge rather than being one: "Confirm you're
# human to use your account, <handle>" over a Continue button, and a promise
# that it "takes about 30 seconds". Nothing to answer -- press Continue and the
# real step is behind it. Read off `Katherine 8`, 2026-08-12.
_VERIFY_INTRO_MARKERS = (
    "confirm you're human to use your account",
    "confirm youre human to use your account",
    "takes about 30 seconds",
)

_CONSENT_GATE_MARKERS = (
    "choose if we process your data for ads",
    "consent to us processing your personal data",
    "process your personal data",
    "subscribe or continue using our products",
    "free of charge with ads",
    "cookies on our products",
    "consent to meta processing",
    "set up on new device",
)


def screen_is_healthy(text: str | None) -> bool:
    """True when the screen positively shows Instagram working normally.

    Deliberately a whitelist. A blacklist of "screens that are not the feed"
    cannot be written, because the whole problem is the screens nobody has seen
    yet -- and every one of those should stop the run, not pass it.
    """
    if not text:
        # An unreadable screen is not a clear screen. It was reported as one:
        # `Jil 20`'s first look returned an empty string and classified as
        # "nothing wrong".
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _APP_HEALTHY_MARKERS)


def looks_like_consent_gate(text: str | None) -> bool:
    """True for Meta's ads-consent interstitial (see `_CONSENT_GATE_MARKERS`)."""
    if not text:
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _CONSENT_GATE_MARKERS)


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
    # Ahead of the captcha: this screen says "confirm you're human" too, and
    # the difference is that it has nothing on it to answer.
    (CHALLENGE_VERIFY_INTRO, _VERIFY_INTRO_MARKERS),
    (CHALLENGE_IMAGE_CAPTCHA, _IMAGE_CAPTCHA_MARKERS),
    (CHALLENGE_PHOTO, _PHOTO_MARKERS),
    (CHALLENGE_CODE, _CODE_STRONG_MARKERS),
    (CHALLENGE_PHONE, _PHONE_STRONG_MARKERS),
    (CHALLENGE_CHOOSE_METHOD, _CHOOSE_METHOD_MARKERS),
    (CHALLENGE_CODE, _CODE_WEAK_MARKERS),
    (CHALLENGE_PHONE, _PHONE_WEAK_MARKERS),
    (CHALLENGE_SIGNED_OUT, _SIGNED_OUT_WEAK_MARKERS),
    # Last on purpose. A consent gate is only ever a consent gate when nothing
    # that actually asks something of the account matched first -- these words
    # are broad, and shadowing a phone or code screen with one would be far
    # worse than the reverse.
    (CHALLENGE_CONSENT, _CONSENT_GATE_MARKERS),
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
        if not any(marker in haystack for marker in markers):
            continue
        # A screen that positively shows a working Instagram is not a consent
        # gate, whatever words are on it. The consent markers are broad enough
        # to appear on an ordinary feed -- `Blank (23)` (2026-08-12) came out
        # of its consent chain onto a feed whose dump still carried "free of
        # charge with ads" -- and reading that as a gate would tap at a healthy
        # account until the run gave up on it. Only consent is qualified this
        # way: every other marker set names something the account is being
        # *asked*, which a feed cannot be showing.
        if kind == CHALLENGE_CONSENT and screen_is_healthy(haystack):
            return CHALLENGE_NONE
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

    def can_request_new_number(self) -> bool:
        """Whether *this* screen offers the change-number link.

        `request_new_number` presses Back when it finds no link, which is a fine
        last resort when a number we rented has just timed out -- we know where
        we are. It is not fine as a way to *ask*: on a code screen for somebody
        else's number, Back leaves the chain rather than restarting it.

        Optional. A driver without it is treated as unable to say, and the loop
        takes the cautious branch.
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

# How many times one run may take over a code screen that is waiting on a number
# it does not own (`_handle_code`). Once: if pressing the change-number link
# lands us straight back on somebody else's code screen, the link did not do
# what it says, and pressing it again is how a loop is built. The rented-number
# retries that follow are bounded separately by MAX_NUMBER_ATTEMPTS.
MAX_NUMBER_TAKEOVERS = 1

# How many times one run will ask Instagram for a different captcha image. The
# blank-image case is a real one (`Laila 3`, 2026-08-12: correctly sized node,
# pure white, for 90 seconds), and asking costs nothing but a tap -- but a
# phone that renders no images will render none of these either, and each round
# also spends one of `MAX_REPEATS` on the same screen.
MAX_CAPTCHA_IMAGES = 2

# How many times one run will look again at a captcha screen that produced no
# UI dump, and how long it waits between looks. A phone that is still thinking
# about the answer just submitted is the common case (`Default profile name
# (44)`, 2026-08-13), and it resolves in under a minute or not at all: two
# waits cover it without letting a phone that never settles hold the pass.
MAX_CAPTCHA_BLIND_READS = 2
CAPTCHA_SETTLE_SECONDS = 20.0

# Consecutive "code not sent" refusals before the run gives up on this account.
# Instagram's own wording offers both readings -- "try again later **or** use a
# different mobile number" -- so one more number is worth trying. Two identical
# refusals back to back is Instagram declining to send at all, and a third
# number buys nothing: on `Jil 10` (2026-08-12) numbers 2 and 3 were refused
# within 27 seconds of each other, the second rented purely to learn that.
MAX_PHONE_REFUSALS = 2

# How many times one run will re-enter a consent chain. `interruptions` already
# walks a whole chain per call with its own stuck-detection, so this bounds
# *re-entry*: a gate still on screen after being answered twice is not being
# answered, and a person should look.
#
# Two rather than three so this fires before `MAX_REPEATS` does. Both stop the
# run safely, but "a consent screen was still there after 2 attempts" tells a
# VA which screen to go and clear; "the consent screen kept coming back
# unchanged" makes them go and find out.
MAX_CONSENT_ROUNDS = 2

# How many times a run will start Instagram again when the phone is sitting on
# the Android home screen. `Jil 6` (2026-08-13) spent a whole launch, a
# concurrency slot and a hand-back on this: Instagram was simply not running,
# and the pass reported "no verification challenge, but not a working
# Instagram either" -- true, useless, and a person had to look at a phone whose
# only problem was that nobody had opened the app. Two is enough for an app
# that was killed; a phone that will not run Instagram at all is a real
# problem and should still reach a person.
MAX_APP_RESTARTS = 2

# The Android launcher, read off `Jil 6`: "search gallery gallery play store
# play store home telephone telephone messaging messaging music music chrome
# chrome camera camera". `play store` is the reliable half -- no Instagram
# screen mentions it -- and the second half keeps a single stray word from
# convicting a screen that really is Instagram's.
_LAUNCHER_STRONG_MARKERS = ("play store", "google play store")
_LAUNCHER_SUPPORTING_MARKERS = ("telephone", "messaging", "gallery", "camera",
                                "chrome", "music")


def looks_like_launcher(text: str | None) -> bool:
    """True when the phone is showing its home screen rather than Instagram.

    Not a challenge and not a verdict about the account: the app is not
    running. Reported separately because the fix is to start it, and because
    reading a launcher as "no challenge found" is one of the four ways the old
    `solved` default was wrong.
    """
    if not text:
        return False
    haystack = text.lower()
    if not any(marker in haystack for marker in _LAUNCHER_STRONG_MARKERS):
        return False
    return sum(marker in haystack
               for marker in _LAUNCHER_SUPPORTING_MARKERS) >= 2

# Instagram's inline error on the phone screen when it will not text the number
# that was just submitted. Read off `Jil 10`, 2026-08-12.
_PHONE_REFUSED_MARKERS = (
    "code not sent",
    "use a different mobile number",
)


def phone_number_refused(text: str | None) -> bool:
    """True when the phone screen is showing Instagram's send-refused error."""
    if not text:
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _PHONE_REFUSED_MARKERS)

# The longest one account may take before the run gives up on it. MAX_STEPS
# bounds how many *screens* are worked, but not how long each takes: a chain
# that spends 45s waiting for each of three numbers, 20s on captcha images and
# 15s doubting a clear screen adds up, and an unattended pass needs to be
# predictable enough to put on a timer. Reaching this is reported as `stuck`,
# which changes nothing about the profile -- the same as running out of steps.
MAX_RUN_SECONDS = 900.0

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
                     sleep=None, clock=None,
                     max_seconds: float = MAX_RUN_SECONDS) -> VerificationResult:
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
                       service, country, sleep=sleep, clock=clock,
                       max_seconds=max_seconds)
    try:
        return session.run(max_steps)
    finally:
        # A number rented but never confirmed must go back, whatever ended the
        # run -- including an exception on the phone half.
        session.release_lease()


class _Session:
    """The loop's mutable state. Split out so `run_verification` stays readable."""

    def __init__(self, driver, router, solver, logger, max_number_attempts,
                 service, country, sleep=None, clock=None,
                 max_seconds: float = MAX_RUN_SECONDS) -> None:
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
        self.max_seconds = max_seconds
        self._started_at = self.sleep_clock()

        self.lease = None            # the number currently typed into Instagram
        self.numbers_used = 0
        self.code_received = False
        self.steps: list = []
        self._last_challenge = None
        self._repeats = 0
        self._captcha_answered = False
        self._captcha_images = 0
        self._refusals = 0
        self._consent_rounds = 0
        self._number_takeovers = 0   # see `_handle_code`
        self._captcha_blind_reads = 0  # see `_handle_image_captcha`
        self._app_restarts = 0       # see `looks_like_launcher`

    # --- main loop ------------------------------------------------------------
    def run(self, max_steps: int) -> VerificationResult:
        for _ in range(max_steps):
            # Checked before the screen is read, not after the work is done, so
            # the run cannot start a step it has no time to finish -- renting a
            # number and then abandoning it is the one thing this must not do.
            # `release_lease` in the caller settles anything already held.
            elapsed = self.sleep_clock() - self._started_at
            if self.max_seconds and elapsed >= self.max_seconds:
                return self._result(
                    RESULT_STUCK,
                    f"gave up after {elapsed / 60:.0f} minutes on this account")

            text = self.driver.read_screen()

            # Instagram not being on screen is not an answer about the account.
            # Starting it again is nearly free and recovers the launch; only a
            # phone that will not show Instagram after that is a person's
            # problem. Checked before classification because a launcher carries
            # no challenge marker and would otherwise be weighed as "clear".
            if looks_like_launcher(text):
                if self._app_restarts < MAX_APP_RESTARTS and self._restart_app():
                    self._app_restarts += 1
                    self._log("info",
                              "verification: the phone is on its home screen, not "
                              "Instagram -- starting it again (%d/%d)",
                              self._app_restarts, MAX_APP_RESTARTS)
                    self._repeats = 0
                    continue
                return self._result(
                    RESULT_NEEDS_HUMAN,
                    "Instagram is not running on this phone and would not start, "
                    "so nothing here says anything about the account")

            challenge = classify_challenge(text)

            if challenge == CHALLENGE_NONE:
                # Never believed on the first read -- see `_confirm_clear`. If a
                # challenge is merely late, this is where it is caught; if the
                # screen is genuinely clear, this returns none and we are done.
                challenge, text = self._confirm_clear(text)
            if challenge == CHALLENGE_NONE:
                return self._clear_screen_result(text)
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

            # A captcha we answered is only "the last one" until something else
            # comes up. Without this, a chain that returns to a captcha later
            # (Instagram does re-ask) would report the *earlier*, correct answer
            # to 2captcha as wrong -- refunding a solve we should have paid for
            # and feeding the service bad accuracy data.
            if challenge != CHALLENGE_IMAGE_CAPTCHA:
                self._captcha_answered = False

            if not self._note_progress(challenge):
                return self._result(
                    RESULT_STUCK,
                    f"the {challenge} screen kept coming back unchanged")

            outcome = self._handle(challenge, text)
            if outcome is not None:
                return outcome

        return self._result(RESULT_STUCK,
                            f"gave up after {max_steps} screens")

    def _clear_screen_result(self, text):
        """Decide what a screen carrying no challenge marker actually means.

        The one place a run is allowed to report success, and the reason it is
        not simply `return SOLVED`: "no challenge marker" and "the account is
        fine" are different claims, and the recordings show the gap between
        them is three real screens -- the Android launcher, Meta's ads-consent
        gate, and an empty read. See `_APP_HEALTHY_MARKERS`.

        Erring towards `needs_human` is deliberate and cheap: the profile keeps
        the `Issue` tag it already had and somebody glances at it. Erring the
        other way hands a profile nobody fixed back to the posting loop, which
        is how a phone spends launches for days achieving nothing.
        """
        if screen_is_healthy(text):
            self._dismiss_confirmation()
            # Two very different runs end here, and until now both reported the
            # same sentence. `Jasmin 5` was tagged `Issue` at 04:40 on
            # 2026-08-13 for `Retries Exhausted` -- a posting failure -- and
            # this pass launched it, found an ordinary working Instagram and
            # called it solved, exactly as it reads a captcha that was actually
            # answered. The tally then counts a launch that cleared nothing as
            # a verification success, which is the number decisions get made
            # on. `steps` already knows the difference.
            worked = [s for s in self.steps if s != CHALLENGE_NONE]
            if worked:
                # dict.fromkeys: in order, without repeating a screen that came
                # round twice.
                detail = "cleared " + ", ".join(dict.fromkeys(worked))
            else:
                detail = ("Instagram was already working -- there was no "
                          "challenge on this phone to clear")
            return self._result(RESULT_SOLVED, detail)

        if looks_like_consent_gate(text):
            return self._result(
                RESULT_NEEDS_HUMAN,
                "Instagram is showing Meta's ads-consent gate, which blocks the "
                "app until somebody answers it. What an account consents to is "
                "not the bot's decision, so this needs a person")

        # Everything else: name it as unrecognised and hand over the text, which
        # is the only way the marker lists ever grow. A screen that lands here
        # twice is a marker list waiting to be written.
        snippet = " ".join(str(text or "").split())[:200] or "(the screen read as empty)"
        self._log("warning",
                  "verification: no challenge marker, but the screen does not look "
                  "like a working Instagram either. Not reporting this as solved. "
                  "Screen was: %r", snippet)
        return self._result(
            RESULT_NEEDS_HUMAN,
            f"the screen shows no verification challenge, but does not look like "
            f"a working Instagram either -- so this cannot be called solved. "
            f"Screen text: {snippet}")

    def _dismiss_confirmation(self) -> None:
        """Best-effort tap on the `Done` of the un-suspension screen.

        Both accounts solved on 2026-08-12 were left sitting on *"You're back on
        Instagram"* with its button untapped. Harmless as far as anyone can
        tell, but leaving a phone parked mid-screen is untidy and nobody has
        checked whether Instagram wants the acknowledgement. Optional on the
        driver, and a failure is not a failure of the run: the chain is already
        cleared by the time this is reached.
        """
        dismiss = getattr(self.driver, "dismiss_confirmation", None)
        if not callable(dismiss):
            return
        try:
            if dismiss():
                self._log("info", "verification: acknowledged the confirmation screen")
        except Exception as exc:
            self._log("warning", "verification: dismissing the confirmation raised (%s)",
                      exc)

    def _confirm_clear(self, text) -> tuple:
        """Re-read a screen that looked clear. Returns `(challenge, text)`.

        The text comes back with the verdict because the caller has to judge it
        further -- "no challenge marker" is not "the account is fine", and
        `_clear_screen_result` needs the *last* text read, not the first.

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
            text = self.driver.read_screen()
            challenge = classify_challenge(text)
            if challenge != CHALLENGE_NONE:
                self._log("info", "a %s screen appeared after the first read looked "
                                  "clear -- this is why a clear screen is not believed "
                                  "immediately", challenge)
                return (challenge, text)

        if self.steps:
            return (CHALLENGE_NONE, text)

        # `refresh_feed` is optional on the driver: a caller may supply a
        # simpler one, and a missing refresh must not turn a working run into
        # an AttributeError.
        refresh = getattr(self.driver, "refresh_feed", None)
        if not callable(refresh) or not refresh():
            return (CHALLENGE_NONE, text)

        self._log("info", "screen still looked clear after %.0fs; pulled the feed "
                          "down and looking again", CLEAR_SCREEN_PATIENCE_SECONDS)
        deadline = self.sleep_clock() + CLEAR_SCREEN_PATIENCE_SECONDS
        while self.sleep_clock() < deadline:
            text = self.driver.read_screen()
            challenge = classify_challenge(text)
            if challenge != CHALLENGE_NONE:
                self._log("info", "a %s screen appeared after the refresh", challenge)
                return (challenge, text)
            self.sleep(CLEAR_SCREEN_POLL_SECONDS)
        return (CHALLENGE_NONE, text)

    def _handle(self, challenge: str, text=None):
        """Do the one thing this screen needs. Return a result to stop, or None."""
        if challenge == CHALLENGE_CHOOSE_METHOD:
            if not self.driver.choose_sms_method():
                return self._result(RESULT_FAILED,
                                    "could not choose the SMS option")
            return None

        if challenge == CHALLENGE_PHONE:
            return self._handle_phone(text)

        if challenge == CHALLENGE_CODE:
            return self._handle_code()

        if challenge == CHALLENGE_PHOTO:
            if not self.driver.upload_photo():
                return self._result(RESULT_NEEDS_HUMAN,
                                    "the photo challenge could not be completed")
            return None

        if challenge == CHALLENGE_VERIFY_INTRO:
            return self._handle_verify_intro()

        if challenge == CHALLENGE_CONSENT:
            return self._handle_consent()

        if challenge == CHALLENGE_IMAGE_CAPTCHA:
            return self._handle_image_captcha()

        return self._result(RESULT_FAILED, f"unhandled challenge {challenge!r}")

    def _handle_verify_intro(self):
        """Press Continue on the screen that only introduces a challenge.

        There is nothing to answer here, so the one thing that must not happen
        is treating it as the step it introduces -- which is what cost a
        2captcha solve and a failed run on `Katherine 8`. Whatever is behind it
        comes back round the loop and is classified on its own terms.
        """
        advance = getattr(self.driver, "advance_intro", None)
        if not callable(advance) or not advance():
            return self._result(
                RESULT_NEEDS_HUMAN,
                "could not get past the screen introducing the challenge")
        return None

    def _handle_consent(self):
        """Tap through Meta's consent / onboarding chain.

        Free by definition -- `interruptions` picks the no-cost option on the
        one screen that has a paid one. What it must never do is loop: a gate
        still on screen after being answered is not being answered.
        """
        if self._consent_rounds >= MAX_CONSENT_ROUNDS:
            return self._result(
                RESULT_NEEDS_HUMAN,
                f"a consent screen was still there after {self._consent_rounds} "
                f"attempts to answer it")
        self._consent_rounds += 1

        clear = getattr(self.driver, "clear_blocking_prompts", None)
        if not callable(clear) or not clear():
            return self._result(
                RESULT_NEEDS_HUMAN,
                "Instagram is showing a consent screen this run could not get "
                "past")
        self._log("info", "verification: tapped through the consent screens "
                          "(round %d)", self._consent_rounds)
        return None

    # --- individual screens ---------------------------------------------------
    def _handle_phone(self, text=None):
        # Instagram declining to send is not the provider's fault, and the
        # breaker must not learn it as one. Checked before the lease is
        # released, because releasing is what would count it.
        refused = phone_number_refused(text) and self.lease is not None
        if refused:
            self._refusals += 1
            self._log("warning",
                      "verification: Instagram refused to text %s (\"code not "
                      "sent\"). That is not the provider's fault, so it is not "
                      "counted against it. Refusal %d of %d.",
                      getattr(self.lease.order, "phone", "the number"),
                      self._refusals, MAX_PHONE_REFUSALS)
            self.release_lease(count_failure=False)
            if self._refusals >= MAX_PHONE_REFUSALS:
                # Renting a third is buying the same answer again. "Try again
                # later" is about this account, not about the numbers.
                return self._result(
                    RESULT_NEEDS_HUMAN,
                    f"Instagram refused to send a code to {self._refusals} "
                    f"different numbers -- it is not accepting new numbers for "
                    f"this account right now")
        else:
            self._refusals = 0

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
            #
            # That is not the end of it, because the screen itself offers a way
            # out. `Blank (13)` and `Blank (15)` both opened here on 2026-08-13
            # -- "enter the 6-digit confirmation code we sent via sms to
            # +49..." over an `Update mobile number` link -- and both were
            # settled as terminal, tagged `unable to verify`, and benched for
            # seven days without that link ever being pressed. Blank (15) had
            # burned three of our own numbers the day before, so the number it
            # was waiting on was very likely one of ours that we had already
            # refunded: unreadable now, but nothing about the *account* was
            # wrong.
            #
            # So ask for the phone screen instead, once, and let the loop rent
            # a number we can actually read. Only when the link is really on
            # screen: `request_new_number` falls back to pressing Back, which
            # from here leaves the chain rather than restarting it.
            if self._number_takeovers < MAX_NUMBER_TAKEOVERS \
                    and self.numbers_used < self.max_number_attempts \
                    and self._change_number_offered():
                self._number_takeovers += 1
                self._log("info",
                          "verification: the code screen wants a number we do "
                          "not control, but offers to change it -- asking for "
                          "the phone screen so we can use one we can read")
                if self.driver.request_new_number():
                    # Same reasoning as the timed-out-number path below: the
                    # screen name will repeat, and asking again is progress.
                    self._repeats = 0
                    return None
                self._log("warning",
                          "verification: could not reach the phone screen from "
                          "a code screen for somebody else's number")
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
        # A captcha screen nothing can be typed into is not worth buying an
        # answer for. `Default profile name (44)` on 2026-08-13: the answer
        # `312129` went in, `Next` was pressed, and 43 seconds later the phone
        # -- still working, the intro screen had said it "takes about 30
        # seconds" -- produced no UI dump at all. OCR read the picture, which
        # still showed the captcha screen with `312129` sitting in the field,
        # so the run called the answer wrong, reported it back to 2captcha,
        # bought a second solve off the whole screenshot, and then failed with
        # `no input field on screen` -- because there was no dump to find one
        # in. Three wasted actions, and the first answer may well have been
        # right.
        #
        # So: no dump, no purchase. Wait for the phone to finish and look
        # again; only what a dump shows is worth acting on.
        if not self._screen_is_actionable():
            if self._captcha_blind_reads < MAX_CAPTCHA_BLIND_READS:
                self._captcha_blind_reads += 1
                self._log("info",
                          "verification: the captcha screen produced no UI dump "
                          "(%d of %d) -- nothing on it can be typed into, so "
                          "waiting %.0fs for the phone to settle rather than "
                          "buying an answer",
                          self._captcha_blind_reads, MAX_CAPTCHA_BLIND_READS,
                          CAPTCHA_SETTLE_SECONDS)
                self.sleep(CAPTCHA_SETTLE_SECONDS)
                # The screen name will repeat; waiting for it to settle is
                # progress, in the same sense as asking for a new number.
                self._repeats = 0
                return None
            return self._result(
                RESULT_NEEDS_HUMAN,
                "the captcha screen never produced a UI dump, so an answer "
                "could not be typed into it")

        # Seeing this screen again after we typed an answer means the answer was
        # wrong. Tell the service before asking it for another one: that refunds
        # the bad solve and is the only feedback keeping its accuracy honest.
        if self._captcha_answered:
            self._report_bad_captcha()

        image = self.driver.capture_captcha_image()
        if not image:
            # Usually the image did not render (see `image_is_blank`). The
            # screen offers its own way out -- `Get a new code` -- and asking
            # for a fresh image is the only recovery that exists: there is
            # nothing to re-read, so waiting achieves nothing.
            if self._captcha_images < MAX_CAPTCHA_IMAGES:
                self._captcha_images += 1
                refresh = getattr(self.driver, "request_new_captcha", None)
                if callable(refresh) and refresh():
                    self._log("info", "verification: asked for a new captcha image "
                                      "(%d of %d)",
                              self._captcha_images, MAX_CAPTCHA_IMAGES)
                    # Return to the loop rather than re-reading here, so the
                    # replacement goes through `classify_challenge` like any
                    # other screen -- it may not be a captcha at all.
                    return None
            return self._result(
                RESULT_NEEDS_HUMAN,
                f"the captcha image could not be read after "
                f"{self._captcha_images} attempt(s) -- it is most likely not "
                f"rendering on this phone")

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

    def _screen_is_actionable(self) -> bool:
        """Whether the screen just read carries elements, not just words.

        A driver that cannot say counts as actionable: every fake driver in the
        tests answers screens straight from a script, and the cautious reading
        would stop all of them from ever solving anything.
        """
        source = getattr(self.driver, "screen_source", None)
        if not callable(source):
            return True
        try:
            return str(source()) == "ui-dump"
        except Exception as exc:
            self._log("warning",
                      "verification: could not tell how the screen was read (%s)",
                      exc)
            return True

    def _restart_app(self) -> bool:
        """Ask the driver to start Instagram again.

        Optional on the driver, the same way `can_request_new_number` is: a
        driver that cannot do it answers no, and the run falls through to the
        hand-back it would have made anyway.
        """
        starter = getattr(self.driver, "restart_app", None)
        if not callable(starter):
            return False
        try:
            return bool(starter())
        except Exception as exc:
            self._log("warning", "verification: restarting Instagram raised (%s)", exc)
            return False

    def _change_number_offered(self) -> bool:
        """Whether the screen we are on offers to change the number.

        A driver that cannot answer counts as "no": the only caller uses this
        to decide whether to press something on a screen it did not expect, and
        the cautious branch there hands the profile back unchanged, which is
        where it already was.
        """
        ask = getattr(self.driver, "can_request_new_number", None)
        if not callable(ask):
            return False
        try:
            return bool(ask())
        except Exception as exc:
            self._log("warning",
                      "verification: could not tell whether the code screen "
                      "offers a new number (%s)", exc)
            return False

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

    def release_lease(self, count_failure: bool = True) -> None:
        """Settle any held number: finished if its code was used, refunded if not.

        `count_failure=False` refunds it without blaming the provider -- see
        `NumberLease.release`.
        """
        if self.lease is None:
            return
        lease, self.lease = self.lease, None
        try:
            lease.release(count_failure=count_failure)
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
