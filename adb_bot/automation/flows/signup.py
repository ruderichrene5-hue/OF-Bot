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
SCREEN_CODE_OPTIONS = "code_options"    # bottom sheet the code screen can fall into
SCREEN_CALL_CONFIRM = "call_confirm"    # "Confirm ... automatically with a phone call"
SCREEN_SEND_SMS = "send_sms"            # "Send SMS to confirm your account" (outbound!)
SCREEN_METHOD_CHOOSER = "method_chooser"  # "Change mobile number / Confirm by email"
SCREEN_PASSWORD = "password"            # "Create a password"
SCREEN_BIRTHDAY = "birthday"            # "What's your date of birth?"
SCREEN_DATE_PICKER = "date_picker"      # the Android spinner dialog
SCREEN_NAME = "name"                    # "What's your name?"
SCREEN_USERNAME = "username"            # "Create a username"
SCREEN_TERMS = "terms"                  # "Agree to Instagram's terms" -- creates it
SCREEN_PERMISSIONS = "permissions"      # "Allow Instagram to access your device?"
SCREEN_PHOTO_PROMPT = "photo_prompt"    # "Add a profile photo"
SCREEN_FOLLOW = "follow"                # "Follow 5 or more people"
SCREEN_COOKIES = "cookies"              # Meta's cookie consent, after the account exists
SCREEN_ADD_EMAIL = "add_email"          # post-creation "Add an email address"
SCREEN_ADD_PHONE = "add_phone"          # post-creation "Add a mobile number"
SCREEN_PERMISSION_DIALOG = "permission_dialog"   # Android's own allow/deny
SCREEN_INTERSTITIAL = "interstitial"    # feed personalisation, nav tips
SCREEN_SAVE_PASSWORD = "save_password"  # Android autofill, not Instagram
SCREEN_LAUNCHER = "launcher"            # the phone's home screen -- start the app
SCREEN_NOT_INSTAGRAM = "not_instagram"  # some other app is in front -- same fix
SCREEN_LOADING = "loading"              # mid-render -- wait, do not act
SCREEN_DONE = "done"                    # a working account
SCREEN_BANNED = "banned"                # created and immediately disabled
SCREEN_CHECKPOINT = "checkpoint"        # created, then held for verification
SCREEN_UNKNOWN = "unknown"              # stop -- never act on this

# "Confirm you're human to use your account, <username>" -- what Instagram put
# in front of `@ida.sommer43` seconds after the `I agree` tap on 2026-08-18.
# The account exists at this point: Instagram names it, which it cannot do
# before creating it. Reported as `unknown_screen` this reads like a failure
# and the credentials look worthless, when in fact the only work left is the
# verification flow this repo already has.
_CHECKPOINT_MARKERS = (
    "confirm you're human to use your account",
    "confirm you are human to use your account",
)

_ENTRY_MARKERS = (
    "join instagram",
    "share what you're into with the people who get you",
    # A freshly installed Instagram opens on its **login** form instead, with
    # signup offered as `Create new account` at the bottom. Seen on the
    # `gmail test` phone, 2026-08-13 -- same destination, different door.
    "create new account",
)

# "Get started" alone is too broad to name the entry screen -- it appears on
# onboarding screens all over the app -- so it is only a marker next to the
# title above.
_PHONE_MARKERS = (
    "what's your mobile number",
    "whats your mobile number",
    "enter the mobile number on which you can be contacted",
    # Instagram now words it "where you can be contacted" here too, which used
    # to belong to the post-creation prompt alone. See `_ADD_PHONE_MARKERS`.
    "enter the mobile number where you can be contacted",
)

# Instagram's offer to verify by ringing the number instead of texting it.
# Seen first on 2026-08-21, on the first UK number ever used here -- twenty-four
# German numbers never produced it, so it appears to follow the country.
#
# It has to be answered, not skipped: it wants the `manage phone calls`
# permission so it can ring the handset and hang up automatically, and a rented
# SMS number cannot take a call. The screen offers `Confirm with a code`, which
# is the SMS route we already know how to drive, so that is the button -- NOT
# `Next`, which is the one that asks for the permission.
_CALL_CONFIRM_MARKERS = (
    "confirm your account automatically with a phone call",
    "we'll call your mobile number and end the call automatically",
)

# Instagram asking the PHONE to send an SMS out, rather than sending one in:
# it opens the handset's own messaging app with a prefilled code addressed to
# Instagram. Seen 2026-08-21 immediately after declining the phone call.
#
# It cannot work here whichever way you look at it. The message would be sent
# by the cloud phone's own SIM, which is not the number Instagram is trying to
# confirm; and these phones have no usable SIM to send it with anyway. The
# screen's `Try another way` is the route onward.
_SEND_SMS_MARKERS = (
    "send sms to confirm your account",
    "send a prefilled code from",
    "tap to open your default sms app",
)

# What `Try another way` actually leads to, seen 2026-08-21 on a UK number:
# `Dismiss / Change mobile number / Confirm by email / Close`.
#
# Read that list carefully, because it is the whole finding. There is **no
# option to receive an SMS code**. Instagram offered this number a phone call,
# an outbound SMS, a different number, or email -- and never once offered to
# text it. German numbers were offered the code route and simply never
# received; British ones are refused it outright. Both are the same wall from
# opposite sides: a rented virtual number cannot finish this signup.
_METHOD_CHOOSER_MARKERS = (
    "change mobile number",
    "confirm by email",
)

# Surfaces belonging to some *other* app, seen when Instagram has left the
# foreground. Only the Play Store so far, which is what these phones fall back
# to; add others as they turn up rather than guessing at them.
#
# Each marker has to be unmistakably not-Instagram. A loose one here is worse
# than a missing one: it would restart Instagram in the middle of a signup that
# was going fine, and a restarted signup begins again at "Join Instagram" and
# throws away everything it had.
_OTHER_APP_MARKERS = (
    "sign in to find the latest android apps, games, movies, music",
    "google play store",
)


def looks_like_another_app(text: str | None) -> bool:
    """True when the screen belongs to an app that is not Instagram."""
    if not text:
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _OTHER_APP_MARKERS)


# Instagram naming the account back to us, on the screens it shows once one
# exists. This is ground truth for the handle: the flow's own idea of it is a
# guess about what the username box ended up holding, and the two have already
# diverged in production.
_HANDLE_PHRASES = (
    "to use your account, ",
    "confirm you're human to use your account, ",
)

# Instagram handles: letters, digits, dots, underscores, up to 30.
_HANDLE_RE = re.compile(r"([a-z0-9._]{1,30})")


def handle_from_text(text: str | None) -> str:
    """The account handle Instagram itself names, or "" if it names none.

    Deliberately narrow. A wrong answer here renames a real account in our own
    records, which is worse than no answer: an empty result leaves the flow's
    own guess in place, while a wrong one overwrites a handle that was right.
    """
    if not text:
        return ""
    haystack = text.lower()
    for phrase in _HANDLE_PHRASES:
        index = haystack.find(phrase)
        if index < 0:
            continue
        match = _HANDLE_RE.match(haystack[index + len(phrase):].lstrip())
        if not match:
            continue
        handle = match.group(1).strip(".")
        # A bare word that is really the start of a sentence is not a handle.
        if len(handle) >= 3 and not handle.isdigit():
            return handle
    return ""

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

# A bottom sheet the code screen can fall into instead of advancing --
# confirmed live 2026-08-26 (jgjfjfcjjvjfjcncncg@gmail.com, Blank 1): typing
# the code and submitting it with the keyboard's own action (the code
# screen's usual recovery when auto-submit doesn't fire) landed here instead
# of on a real result. Its own buttons are the safe way out (`Dismiss` gets
# back to the code screen without restarting the app), but with no marker of
# its own it fell to `SCREEN_UNKNOWN` and burned both `restart_app`
# attempts, landing on the identical sheet each time.
_CODE_OPTIONS_MARKERS = ("resend confirmation code",)

_PASSWORD_MARKERS = (
    "create a password",
    "create a password with at least six",
)

_BIRTHDAY_MARKERS = (
    "what's your date of birth",
    "whats your date of birth",
    "use your own date of birth",
    "why do i need to provide my date of birth",
    # The US build says "birthday" where the German one says "date of birth".
    # Same screen, same field, different noun -- and on 2026-08-21 it stopped
    # the first run ever to get a code delivered, one screen past the wall
    # everything else had been stuck behind.
    "what's your birthday",
    "whats your birthday",
    "use your own birthday",
    "why do i need to provide my birthday",
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

# Meta's cookie consent, which arrives *after* the account has been created --
# it greets the new username by name. Read off `Blank caio 2` on 2026-08-18,
# where it was the last screen of a successful signup and, being unnamed, turned
# `@alina.sommer74` into an `unknown_screen` failure.
_COOKIES_MARKERS = (
    "allow the use of cookies",
    "we use cookies",
    "allow all cookies",
    # Meta's ads-consent screen, which is a separate gate from the cookie one
    # and carries only `Get started`. Seen on an MLX twin on 2026-08-21, where
    # it stopped a login cold: the screen IS Instagram, so `open_instagram`
    # read "not in front" and gave up after five relaunches against an app
    # that was already there.
    "choose if we process your data for ads",
    "whether you consent to us processing your personal data",
)

_ADD_EMAIL_MARKERS = (
    "add an email address",
    "enter the email where you can be contacted",
)

# Post-creation, Instagram also asks for a phone number. Not the signup phone
# screen -- that one asks "What's your mobile number?" -- and not something to
# answer: the account is already made.
_ADD_PHONE_MARKERS = (
    "add a mobile number",
    # "enter the mobile number where you can be contacted" USED to be here and
    # must not come back: Instagram now says exactly that on the *signup*
    # mobile-number screen as well, and this screen is classified first -- so
    # the marker turned every signup into a post-creation prompt the flow then
    # tried to skip. There is no Skip on it, so two Geelark runs looped thirty
    # screens and gave up with the account never started (2026-08-21).
    #
    # "add a mobile number" is the heading only the post-creation prompt has.
    # If this ever needs a second marker, use something the signup screen
    # cannot carry -- it still offers "Sign up with email" and "I already have
    # an account", neither of which can appear once an account exists.
)

# Android's own runtime permission dialogs, raised by the screen below. Contacts
# especially: syncing them is what ties these accounts to each other.
# Note the wording: Android's dialogs name the *thing* ("your contacts"), while
# Instagram's own screen asks about "your device". Including the latter here
# made the flow deny a screen that has nothing to deny.
_PERMISSION_DIALOG_MARKERS = (
    "allow instagram to access your contacts",
    "allow instagram to send you notifications",
)

_INTERSTITIAL_MARKERS = (
    "only get message notifications",
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
    # The interest-picker screen ("Pick what you want to see more of", real
    # account suggestions like "bkblend_official"), observed 2026-08-22 on
    # `nora.sommer62`: the run had already tapped "I agree" -- the account
    # was created -- walked every post-creation prompt correctly (permissions,
    # photo, follow, add-email), and only fell through to `unknown_screen`
    # because this one, final onboarding step had no marker. The account and
    # its credentials were real and already saved; only the ledger status was
    # wrong.
    "pick what you want to see more of",
)
# NOT "your profile.": half the signup says "no one will see this on your
# profile", and so does the post-creation "Add a mobile number" prompt. Reading
# that as a finished account is a false success -- the worst kind of bug this
# flow can have, because it stops the run believing it won.

_ORDERED_MARKERS = (
    # First: the checkpoint names the account, so it must not be read as any of
    # the screens whose words it happens to share ("account", "continue").
    (SCREEN_CHECKPOINT, _CHECKPOINT_MARKERS),
    # Before anything else: a screen that says the account is gone is not a
    # step in the chain.
    (SCREEN_SAVE_PASSWORD, _SAVE_PASSWORD_MARKERS),
    (SCREEN_DATE_PICKER, _DATE_PICKER_MARKERS),
    (SCREEN_CODE_OPTIONS, _CODE_OPTIONS_MARKERS),
    (SCREEN_CODE, _CODE_MARKERS),
    (SCREEN_TERMS, _TERMS_MARKERS),
    (SCREEN_PASSWORD, _PASSWORD_MARKERS),
    (SCREEN_BIRTHDAY, _BIRTHDAY_MARKERS),
    (SCREEN_USERNAME, _USERNAME_MARKERS),
    (SCREEN_NAME, _NAME_MARKERS),
    # "add an email address" before the signup email screen: the post-creation
    # prompt also contains the word pair, and mistaking it for the signup
    # screen would type an address into a live account's settings.
    (SCREEN_PERMISSION_DIALOG, _PERMISSION_DIALOG_MARKERS),
    (SCREEN_ADD_EMAIL, _ADD_EMAIL_MARKERS),
    (SCREEN_ADD_PHONE, _ADD_PHONE_MARKERS),
    # Before SCREEN_PHONE: this screen repeats the number and can carry the
    # same wording, and mistaking it for the number form retypes a number
    # Instagram has already accepted.
    (SCREEN_CALL_CONFIRM, _CALL_CONFIRM_MARKERS),
    (SCREEN_SEND_SMS, _SEND_SMS_MARKERS),
    (SCREEN_METHOD_CHOOSER, _METHOD_CHOOSER_MARKERS),
    (SCREEN_EMAIL, _EMAIL_MARKERS),
    (SCREEN_PHONE, _PHONE_MARKERS),
    (SCREEN_PERMISSIONS, _PERMISSIONS_MARKERS),
    (SCREEN_COOKIES, _COOKIES_MARKERS),
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

    # Instagram can disappear *mid-signup*, not only before it starts. On
    # 2026-08-21 a run tapped the number field, found the field list empty ten
    # seconds later, and read the Play Store on the next dump -- Instagram had
    # gone and the store was simply what lay behind it. Classified as unknown,
    # that ends the run; classified here, it is the same cheap fix as the home
    # screen, which is to start Instagram again.
    if looks_like_another_app(haystack):
        return SCREEN_NOT_INSTAGRAM

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

    def press_enter(self) -> None:
        """Submit the focused field with the keyboard's own action key --
        safe where a button tap or `dismiss_keyboard()`'s BACK is not: it
        only ever acts on a focused text field, never falls through as
        navigation."""

    def set_date(self, day: int, month: str, year: int) -> bool:
        """Drive the Android date-picker spinner and confirm it."""


@dataclass
class Identity:
    """Everything one new account needs to be created with.

    `email` decides which of the two chains runs. With it set, signup takes the
    `Sign up with email address` escape hatch off the mobile-number screen and
    the confirmation code is read out of the Gmail app on the phone; without it,
    a number is rented. `email_password` is not used during signup -- it is
    written down with the rest so the mailbox that owns the account is
    recoverable, which is the hole the SMS-only accounts are stuck in.
    """

    full_name: str
    username: str
    password: str
    birth_day: int
    birth_month: str
    birth_year: int
    email: str = ""
    email_password: str = ""

    def summary(self) -> str:
        via = f" via {self.email}" if self.email else " via SMS"
        return (f"{self.full_name} / @{self.username} / "
                f"{self.birth_day} {self.birth_month} {self.birth_year}{via}")


RESULT_CREATED = "created"
# Created, then held behind "confirm you're human" before it could be used. The
# account and its credentials are real; what is left is verification, not
# signup. Kept separate from `created` so nothing downstream treats it as a
# phone ready to post, and separate from the failures so nobody throws the
# credentials away.
RESULT_CREATED_UNVERIFIED = "created_unverified"
RESULT_UNKNOWN_SCREEN = "unknown_screen"
RESULT_NO_NUMBER = "no_number"
# Instagram would not text this number at all -- it offered a call, an outbound
# SMS, a different number or email, and never the code route. Deliberately not
# `no_number`, which means the opposite: Instagram *did* send a code and the
# number never received it. One says the pool is burned, the other says the
# number type is refused, and they need different answers.
RESULT_NUMBER_REFUSED = "number_refused"
RESULT_BANNED = "banned"
RESULT_STUCK = "stuck"
RESULT_PHONE_LOST = "phone_lost"
RESULT_ERROR = "error"

# The phone already had somebody's account on it. Not a failure of the signup --
# nothing was attempted -- and emphatically not a success.
RESULT_OCCUPIED = "occupied"

# The mailbox could not be read: not signed in on the phone, not syncing, the
# wrong inbox on screen, or no mail inside the window. Separate from
# `no_number` because the fix is a phone setting rather than a provider.
RESULT_MAILBOX = "mailbox"


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

# Each number costs real money and a `CODE_WAIT_SECONDS` wait. The US pool
# delivered 11 of 15 on 2026-08-21 -- about 73% -- so three attempts carry a
# run past a bad draw without spending the whole phone on numbers.
#
# An undelivered number is refunded, so the cost of an extra attempt is the
# phone's life rather than money; that is what bounds this, and it is why the
# wait was cut to 90s.
MAX_NUMBER_ATTEMPTS = 3

# The country Instagram's own picker opens on, which follows the phone's proxy
# and locale -- German, for this fleet. It decides how a number is typed, not
# where numbers are bought: a number from this country goes in as its national
# part, and any other has to be typed in full with its `+` code.
#
# Buying German is a separate decision, and on 2026-08-21 the evidence went
# against it: fifteen German numbers across both providers delivered nothing,
# every one of them from the same +49 1590 56xx block, and SMSPool prices
# Germany at $0.60 against $0.30 for the UK.
PICKER_COUNTRY = "DE"

# The same screen this many times running, with its handler claiming success,
# means the handler is not advancing anything.
MAX_REPEATS = 4

# How long to wait for an SMS before writing the number off.
#
# Ninety, not a hundred and fifty. Every code that has ever arrived here
# arrived fast: measured across all of 2026-08-21's runs, the delivery
# latencies were 0s, 0s, 3s and 77s, and **nothing has ever landed between 77s
# and the old 150s ceiling**. So the last minute of each wait was spent on an
# outcome never once observed, at 73 seconds a number out of a phone that lives
# about fifteen minutes -- often the difference between getting a third number
# tried and running out of phone first.
#
# Raise it again only against new evidence of a slow delivery, not on the
# general feeling that longer is safer: longer is only safer if something
# actually arrives late, and across 40 numbers that waited the full 150s, not
# one ever did.
#
# **Delivery here is bimodal, and that shapes how this can bite.** The two slow
# codes -- 68s and 77s, on different phones in different runs -- arrived within
# one second of each other. They were not drifting; the provider was holding a
# backlog and flushed it at 16:12:24, while every other code that day came in
# 0s or 3s. So a number is either answered at once or stuck behind a stall that
# clears for everyone together.
#
# The failure that implies is not gradual. A stall lasting a little longer than
# that one costs not one number but every number waiting in the window, across
# every phone running -- so it will present as a fleet-wide SMS outage rather
# than as variance. Several runs reporting `no_number` in the same minute is
# that signature, and it means "the provider stalled", not "the pool is
# burned".
CODE_WAIT_SECONDS = 90

# How long to wait for the mail. Longer than the SMS budget because nothing is
# ageing while we wait -- no number is rented -- but still bounded by the phone,
# which only lives about fifteen minutes.
MAIL_WAIT_SECONDS = 210

# How many times a run will wait for a screen that is still drawing before
# calling it stuck. Bounded so a genuinely blank screen -- one appeared after a
# code was accepted and never rendered anything -- ends the run instead of
# spinning until the phone dies.
#
# Fifteen, not eight. Instagram turns the `Next` button into a spinner while it
# submits, and everything here leaves via a German mobile exit: the same email
# screen answered in about thirty seconds on one run and was still spinning
# after ninety on the next (`Blank caio 2`, 2026-08-17). Eight waits is under a
# minute, which called a working screen stuck.
MAX_LOADING_WAITS = 15
LOADING_WAIT_SECONDS = 6

# Same reasoning as verification's: an app that was backgrounded is worth
# starting again, a phone that will not run Instagram at all is not.
MAX_APP_RESTARTS = 2

# How many times the code screen's own options sheet gets dismissed before
# giving up -- its `Dismiss` button returns to the code screen without an
# app restart, so this is bounded separately from MAX_APP_RESTARTS.
MAX_CODE_OPTIONS_DISMISSALS = 3

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

# The way off the phone-call offer and back onto SMS. Several spellings,
# because this screen has only been seen once and Instagram varies its wording
# between builds and locales.
_CONFIRM_WITH_CODE_LABELS = (
    "Confirm with a code", "CONFIRM WITH A CODE", "Confirm with code",
    "Send a code", "Use a code", "Confirm another way",
)

# Instagram saying the handle is spoken for. Both wordings appear together on
# the same screen: a human sentence and an accessibility label.
_USERNAME_TAKEN_MARKERS = (
    "is not available",
    "input username is invalid",
    "username isn't available",
    "that username is taken",
)

# Instagram saying the box is fine as it stands. Checked only after the
# rejection markers, which must win: "input username is invalid" is not
# matched by "input username is valid", but the reverse order would still be
# asking for trouble the day Instagram rewords one of them.
#
# This is what breaks the retype loop. Instagram decorates the value it renders
# -- a handle typed as `emma8613` came back as `emma8_613` -- so a fill that
# insists the field echo back exactly what was typed can never succeed, and the
# flow retyped a username Instagram had already accepted until its budget ran
# out, with `Next` sitting there enabled the whole time.
_USERNAME_VALID_MARKERS = (
    "input username is valid",
    "username is available",
)

# Instagram refusing a number as malformed, before it ever tries to text it.
# This is about the number, not the format we typed it in: `+12274442163` was
# rejected this way while `+19382778361` went through in exactly the same
# shape minutes earlier. Retrying the same number cannot help -- the flow has
# to swap it, which is what it already does when no code arrives.
_PHONE_INVALID_MARKERS = (
    "mobile number is invalid",
    "your mobile number may be incorrect",
    "phone number is invalid",
)

# How many rejected handles to work through before giving up. Each costs about
# 28 seconds of a phone that lives roughly fifteen minutes.
MAX_USERNAME_REJECTIONS = 4


def next_username(rejected: str, attempt: int) -> str:
    """A different handle after Instagram refuses one.

    Deliberately not Instagram's own suggestion, which sits in the field's hint
    and is tempting to reuse: those are minted from the real name and collide
    with the pattern every other account here already uses. A short numeric
    tail keeps the handle recognisably ours and is what the identity generator
    would have produced anyway.

    A `.` sits between the stem and the tail on purpose. Without it (a bare
    `stem + tail`, e.g. `maja6658`) Instagram silently reformats the box --
    observed 2026-08-22 as `maja6658` coming back `maja6_658`, deterministically,
    every single retype -- and the mismatch-detection this file already has
    for Instagram's *own* suggestions correctly saw the box no longer held
    what was typed and retyped the same rejected shape forever. Two of five
    accounts in one batch died that way. A separator already in the box
    leaves Instagram nothing to insert.

    Bounded to Instagram's 30-character limit by trimming the stem, never the
    tail -- a truncated tail is how two accounts end up asking for the same
    handle again.
    """
    import random as _random

    tail = str(_random.randint(10, 9999))
    stem = "".join(ch for ch in rejected if ch.isalnum() or ch in "._")
    stem = stem.rstrip("0123456789").rstrip("._") or "user"
    budget = 30 - len(tail) - 1  # 1 for the separator
    return f"{stem[:budget]}.{tail}"


# The way off any verification method this fleet cannot perform.
_ANOTHER_WAY_LABELS = (
    "Try another way", "TRY ANOTHER WAY", "Try Another Way",
    "Another way", "Choose another way", "Use another method",
)


def _submit_after_typing(driver, labels) -> bool:
    """Tap `labels` after typing, without dismissing the keyboard first.

    Mirrors `instagram_login._submit_after_typing`, found the same day
    (2026-08-22) chasing the mirror-image bug: the username screen never
    truly submits on some devices, and looks instead like Instagram
    re-suggesting a name forever (`hanna3` -> `hanna6337` -> `hanna63372026`,
    two of three accounts in one batch). The likely mechanism is the same one
    proved live against a real Geelark phone that day -- `dismiss_keyboard()`
    sends BACK, and BACK is not reliably consumed by the IME on that device;
    it falls through and steps the signup flow itself back a screen, so
    re-entering "create a username" hands back a fresh suggestion rather than
    confirming what was typed. Because the box then holds Instagram's
    suggestion instead of ours, the existing "box holds Instagram's
    suggestion again" branch fires and retypes from scratch every time --
    never reaching the `submit_with_keyboard()` fallback already written for
    a *different* failure shape (button enabled, tap lands, screen just does
    not move). This tries the tap with the keyboard still open first, which
    cannot suffer that failure at all; only if the button truly is not
    reachable does it fall back to the old dismiss-first sequence.
    """
    if driver.tap_label(labels):
        return True
    driver.dismiss_keyboard()
    return driver.tap_label(labels)


def run_signup(driver: SignupDriver, router, identity: Identity, logger=None,
               sleep=None, mailbox=None, country: str | None = None) -> SignupResult:
    """Walk one account from "Join Instagram" to a working profile.

    Two chains, chosen by `identity.email`:

    * **no email** -- the mobile-number screen Instagram offers first. `router`
      is an `SmsRouter` and numbers are leased lazily, only once the screen that
      needs one is up, because a number starts ageing the moment it is bought.
    * **an email** -- the `Sign up with email address` escape hatch on that same
      screen, with `mailbox` (a `gmail_code.PhoneMailbox`) reading the code out
      of the Gmail app on the phone. This is the chain that leaves the account
      recoverable: the SMS one rents a number, releases it, and leaves nobody
      able to get back in -- which is where roughly sixteen fleet profiles are
      already stuck.
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
    # Handles Instagram has refused. Counted so a run cannot spend its whole
    # phone cycling through names.
    rejections = 0
    # Whether our handle has been put in the box at all. The screen arrives
    # holding Instagram's own suggestion, which is also "valid" -- accepting
    # that without typing first would take a handle minted from the real name,
    # which is the pattern every other account here already uses.
    username_filled = False
    # How many times an accepted username has been submitted. The second
    # submit takes a different route rather than repeating one that did not
    # work -- repeating an identical action is what the repeat guard exists to
    # catch, and it caught this.
    username_submits = 0
    empty_reads = 0
    code_submitted = False
    code_options_dismissed = 0
    done_flags = set()
    # Whether this run has actually put anything into the signup: a number, a
    # code, a password, a name. Until it has, a finished-looking screen is
    # somebody else's account, not ours -- see the `SCREEN_DONE` branch.
    progressed = False

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

            if screen in (SCREEN_LAUNCHER, SCREEN_NOT_INSTAGRAM):
                # Not a signup screen and not a verdict: Instagram is simply
                # not in front. Starting it again is nearly free.
                starter = getattr(driver, "restart_app", None)
                if app_restarts < MAX_APP_RESTARTS and callable(starter) and starter():
                    # Give the number back first. A restart puts Instagram at
                    # "Join Instagram" and the flow walks from the top, leasing
                    # a fresh number -- so any number already rented is now
                    # unreachable, and holding it means paying for one nobody
                    # will ever type. Not counted as a failure: the number was
                    # fine, the app went away.
                    release(False)
                    app_restarts += 1
                    log("info", "%s; starting Instagram again (%d/%d)",
                        "on the home screen" if screen == SCREEN_LAUNCHER
                        else "another app is in front",
                        app_restarts, MAX_APP_RESTARTS)
                    steps.pop()
                    sleep(6)
                    continue
                # Released here, not left to `finish`, which counts every
                # non-created outcome against the provider. The number was
                # delivered to us perfectly well; Instagram died on the phone
                # seventeen seconds later. Charging that to the provider walks
                # a healthy one toward its breaker -- and we have already
                # watched that breaker misfire, cooling every provider down and
                # then renting anyway.
                release(False)
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
                # A working feed on a phone this run has not typed anything
                # into is **not** the account it was about to create -- it is
                # the account that was already there. `Default profile name
                # (42)`, offered by the runner as an empty staging phone on
                # 2026-08-16, was running a logged-in account with a populated
                # feed; without this guard the very first look would have
                # reported `CREATED @<invented-username>` and written
                # credentials for an account nobody ever made.
                if not progressed:
                    return finish(
                        RESULT_OCCUPIED,
                        "the phone already has an account logged in -- nothing "
                        "was created, and nothing on it was touched")
                log("info", "the account exists: @%s", identity.username)
                release(False)
                return SignupResult(status=RESULT_CREATED, username=identity.username,
                                    steps=steps, numbers_used=numbers_used,
                                    phone_number="")

            if screen == SCREEN_CHECKPOINT:
                # Guarded exactly like SCREEN_DONE: on a phone this run has not
                # typed into, a checkpoint belongs to whatever account was
                # already there, and claiming it as ours would write
                # credentials for an account nobody made.
                if not progressed:
                    return finish(
                        RESULT_OCCUPIED,
                        "a checkpoint for an account already on this phone -- "
                        "nothing was created, and nothing on it was touched")
                # Instagram names the account on this screen, and it is the
                # only source that cannot be wrong. Everything upstream is a
                # guess about what the username box ended up holding: the flow
                # may have accepted Instagram's own suggestion, or the field
                # may render a value it never received. On 2026-08-21
                # @nora450960 was written down as @nora.brandt, which puts a
                # real password under a handle that does not exist -- the one
                # unrecoverable way to lose an account.
                named = handle_from_text(text)
                if named and named != identity.username:
                    log("info", "instagram calls this account @%s, not @%s; "
                                "believing instagram", named,
                        identity.username)
                    identity.username = named
                log("info", "the account exists but is held for verification: "
                            "@%s", identity.username)
                release(False)
                return SignupResult(status=RESULT_CREATED_UNVERIFIED,
                                    username=identity.username,
                                    detail=(text or "")[:200],
                                    steps=steps, numbers_used=numbers_used,
                                    phone_number="")

            if screen == SCREEN_BANNED:
                return finish(RESULT_BANNED, "disabled on creation")

            if screen == SCREEN_UNKNOWN:
                # The whole safety property of this flow. Today's run proved a
                # misread screen produces a confident wrong action, so an
                # unnamed screen is never tapped on or typed into.
                #
                # Restarting Instagram is different from that: it never reads
                # or acts on the unknown screen's own content, so it keeps
                # the safety property intact while still attempting the
                # recovery that usually works. Most unnamed screens here are
                # exactly the same app-state glitch `restart_app` already
                # fixes for SCREEN_LAUNCHER/SCREEN_NOT_INSTAGRAM -- "just
                # retry" (explicit instruction, 2026-08-25), not a screen
                # this flow needs to understand. Bounded by the same
                # `app_restarts` budget as those, and the unnamed text is
                # still kept for a person if the restart does not help
                # either.
                starter = getattr(driver, "restart_app", None)
                if app_restarts < MAX_APP_RESTARTS and callable(starter) and starter():
                    app_restarts += 1
                    log("warning", "unnamed screen (%s); restarting Instagram "
                                   "and trying again (%d/%d): %s",
                        screen, app_restarts, MAX_APP_RESTARTS, (text or "")[:200])
                    steps.pop()
                    sleep(6)
                    continue
                return finish(RESULT_UNKNOWN_SCREEN, (text or "")[:400])

            if screen == SCREEN_ENTRY:
                # Two entry screens exist: "Join Instagram" with `Get started`,
                # and the login form with `Create new account`.
                driver.tap_label(("Get started", "Create new account"))

            elif screen == SCREEN_EMAIL:
                if not identity.email:
                    # No mailbox to receive a code, so this screen is a dead
                    # end: go back to the number, which is the path this run
                    # can actually complete.
                    driver.tap_label(("Sign up with mobile number",))
                    sleep(5)
                    continue

                if "email" not in done_flags:
                    # The field arrives holding the phone's **own** Google
                    # account, and nothing errors if that is accepted -- the
                    # account is simply created on somebody else's mailbox. A
                    # half-clear is worse still: fourteen backspaces against a
                    # twenty-one character address left `i1aikjg` in place, ours
                    # was appended, and Instagram mailed a code to the result
                    # without complaint. `fill` clears by measured length and
                    # reads back, and its answer is checked here.
                    if not driver.fill(("email",), identity.email,
                                       "email address"):
                        return finish(
                            RESULT_STUCK,
                            f"could not get {identity.email} into the email "
                            f"field -- refusing to submit a half-typed address")
                    done_flags.add("email")
                    progressed = True
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(8)

            elif screen == SCREEN_PHONE:
                if identity.email:
                    # Signup is phone-first; email is one tap away on this same
                    # screen. Taking it before a number is leased is the whole
                    # point -- an SMS run spends money here.
                    log("info", "taking the email escape hatch")
                    if not driver.tap_label(("Sign up with email address",
                                             "Sign up with email")):
                        return finish(
                            RESULT_STUCK,
                            "no 'Sign up with email address' on the mobile "
                            "number screen, and this run has no number to fall "
                            "back on")
                    sleep(6)
                    continue

                if lease is not None and any(marker in text
                                             for marker in
                                             _PHONE_INVALID_MARKERS):
                    # Instagram will not even try this number. Retyping it just
                    # earns the same rejection -- four times, then a spent
                    # phone. Swap it, and do not count it against the provider:
                    # the number was delivered to us fine, Instagram declined
                    # to use it.
                    log("info", "instagram calls %s invalid; swapping it",
                        lease.e164)
                    release(False)
                if lease is None:
                    if numbers_used >= MAX_NUMBER_ATTEMPTS:
                        return finish(RESULT_NO_NUMBER,
                                      f"{numbers_used} numbers, none delivered")
                    lease = (router.lease(country=country) if country
                             else router.lease())
                    numbers_used += 1
                    log("info", "number %d: %s (%s)", numbers_used, lease.e164,
                        country or PICKER_COUNTRY)
                # The national part only while the number matches the picker,
                # which these phones open on because their proxy and locale are
                # German. A number from anywhere else must go in whole, with
                # its `+` country code, or the picker silently prefixes +49 to
                # a British national number and Instagram texts a number that
                # does not exist.
                typed = (lease.typed_number
                         if (country or PICKER_COUNTRY) == PICKER_COUNTRY
                         else lease.e164)
                if not driver.fill(("mobile", "phone", "number"),
                                   typed, "mobile number"):
                    release(False)
                    continue
                progressed = True
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(6)

            elif screen == SCREEN_CALL_CONFIRM:
                # Take the code, not the call. `Next` here grants Instagram the
                # `manage phone calls` permission so it can ring the handset --
                # which a rented SMS number can never answer, and which would
                # spend the number for nothing.
                if not driver.tap_label(_CONFIRM_WITH_CODE_LABELS):
                    return finish(RESULT_STUCK,
                                  "no way from the phone-call offer back to a "
                                  "code: " + ", ".join(
                                      str(x) for x in
                                      (driver.clickable_labels() or ())[:8]))
                log("info", "declined the phone call; asking for a code")
                progressed = True
                sleep(6)

            elif screen == SCREEN_SEND_SMS:
                # `Open SMS app` would send from the cloud phone's own SIM,
                # which is not the number being confirmed -- and these phones
                # have no usable SIM to send with. `Try another way` is the
                # only move.
                if not driver.tap_label(_ANOTHER_WAY_LABELS):
                    return finish(RESULT_STUCK,
                                  "no way off the outbound-SMS screen: "
                                  + ", ".join(str(x) for x in
                                              (driver.clickable_labels()
                                               or ())[:8]))
                log("info", "declined sending an SMS; asking for another way")
                progressed = True
                sleep(6)

            elif screen == SCREEN_METHOD_CHOOSER:
                labels = [str(x) for x in (driver.clickable_labels() or ())]
                if mailbox is not None and driver.tap_label(
                        ("Confirm by email", "CONFIRM BY EMAIL")):
                    log("info", "no SMS option offered; confirming by email")
                    progressed = True
                    sleep(6)
                    continue
                # Nothing here can be done with a rented number. Another
                # number from the same pool would be refused the same way, so
                # spending two more to be told twice more is waste: stop and
                # say which methods were actually offered.
                release(False)
                return finish(
                    RESULT_NUMBER_REFUSED,
                    "Instagram offered no SMS-code option for this number; "
                    "it offered: " + ", ".join(labels[:8]))

            elif screen == SCREEN_CODE_OPTIONS:
                code_options_dismissed += 1
                if code_options_dismissed > MAX_CODE_OPTIONS_DISMISSALS:
                    return finish(
                        RESULT_STUCK,
                        "the code screen's options sheet would not clear "
                        "after %d tries" % (code_options_dismissed - 1))
                log("info", "the code screen fell into its options sheet "
                            "instead of advancing; dismissing it (%d/%d)",
                    code_options_dismissed, MAX_CODE_OPTIONS_DISMISSALS)
                if not driver.tap_label(("Dismiss", "DISMISS", "Close",
                                         "CLOSE")):
                    log("warning", "nothing to tap on the code options sheet")
                    return finish(RESULT_STUCK,
                                 "no button on the code options sheet")
                # The code was already typed and submitted before this sheet
                # appeared -- give the real code screen behind it a genuine
                # fresh look, not the stale "already submitted" branch.
                code_submitted = False
                last_screen, repeats = None, 0
                sleep(4)
                continue

            elif screen == SCREEN_CODE:
                if code_submitted:
                    # The code was typed and it usually submits itself on the
                    # sixth digit. Tapping a button here is how the run walks
                    # backwards -- with no keyboard up, `dismiss_keyboard()`'s
                    # BACK is not reliably consumed by the IME and falls
                    # through as real navigation, taking a finished code
                    # screen back to "What's your mobile number?" (the same
                    # mechanism as `_submit_after_typing`'s docstring).
                    #
                    # But auto-submit does not always fire: confirmed live
                    # 2026-08-25 (briangonzalezyi121@gmail.com), the same six
                    # digits sat filled through four full waits with nothing
                    # ever pressed, and the run gave up as stuck on a code
                    # that had already arrived correctly. ENTER (keyevent 66)
                    # is the safe middle ground `google_signin._press_enter`
                    # already proved for exactly this shape of problem: it
                    # submits the focused field's own IME action rather than
                    # sending BACK, so it cannot fall through as navigation.
                    #
                    # Keyed off `repeats`, not a separate counter: the generic
                    # repeat-guard above this dispatch (`MAX_REPEATS = 4`)
                    # already ends the run once this same screen has come back
                    # unchanged that many times, well before `loading_waits`
                    # (reset to 0 every iteration this branch is reached from)
                    # could ever count that high on its own -- the ENTER
                    # attempt has to fit inside that same, much smaller
                    # budget, one try, on the next-to-last chance.
                    loading_waits += 1
                    if loading_waits > MAX_LOADING_WAITS:
                        return finish(RESULT_STUCK,
                                      "the code screen did not advance after the "
                                      "code was entered")
                    if repeats == MAX_REPEATS - 2:
                        log("info", "code entered but the screen has not moved "
                                    "on; submitting with the keyboard's own "
                                    "action")
                        driver.press_enter()
                    else:
                        log("info", "code entered; waiting for the screen to "
                                    "move on (%d/%d)", repeats, MAX_REPEATS)
                    steps.pop()
                    sleep(LOADING_WAIT_SECONDS)
                    continue
                if identity.email:
                    # The code is in the mailbox on this phone. Reading it means
                    # leaving Instagram in the background -- which the signup
                    # survives, as long as nothing force-stops it.
                    if mailbox is None:
                        return finish(
                            RESULT_ERROR,
                            f"a code was sent to {identity.email} and this run "
                            f"has no way to read that mailbox")
                    try:
                        code = mailbox.wait_for_code(timeout=MAIL_WAIT_SECONDS)
                    except Exception as exc:
                        # `MailboxNotReady` / `WrongMailbox` both mean a person
                        # has to fix the phone, and both are worth saying in
                        # full rather than retrying blindly against a mailbox
                        # that will never answer.
                        return finish(RESULT_MAILBOX, str(exc)[:300])
                    if not code:
                        return finish(
                            RESULT_MAILBOX,
                            f"no Instagram code reached {identity.email} in "
                            f"{MAIL_WAIT_SECONDS}s")
                elif lease is None:
                    log("warning", "code screen with no number; going back")
                    driver.tap_label(("Back",))
                    sleep(4)
                    continue
                else:
                    code = lease.wait_for_code(timeout=CODE_WAIT_SECONDS)
                    if not code:
                        log("warning", "no code for %s; swapping the number",
                            lease.e164)
                        release(True)
                        driver.tap_label(("Back",))
                        sleep(4)
                        continue
                log("info", "code arrived")
                driver.fill(("code",), code, "confirmation code",
                            submits_itself=True)
                code_submitted = True
                progressed = True
                release(False)
                # Nothing else is pressed here. The field acts on its sixth
                # digit, and the next look will show whatever it advanced to.
                sleep(10)

            elif screen == SCREEN_PASSWORD:
                if "password" not in done_flags:
                    driver.fill(("password",), identity.password, "password")
                    done_flags.add("password")
                    progressed = True
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
                progressed = True
                driver.fill(("name",), identity.full_name, "full name")
                driver.dismiss_keyboard()
                driver.tap_label(_SUBMIT_LABELS)
                sleep(6)

            elif screen == SCREEN_USERNAME:
                # Arrives holding Instagram's own suggestion, which is why this
                # is a fill and not a "tap Next".
                progressed = True
                # Our handle must still be in the box. Instagram bounces back
                # to the name screen after a submit and returns the username
                # screen holding **its own suggestion** again -- so
                # `input username is valid` is true, just not about us. Without
                # this check the flow submits a field it never retyped, four
                # times, and calls itself stuck; three phones went that way.
                # Equality against the field, never a search of the screen
                # text. Instagram mutates a submitted handle by appending
                # digits -- `sara65` comes back as `sara652203` -- so a
                # substring test reports our handle present while the box holds
                # something else, and the accept path then submits Instagram's
                # value while logging ours. One run went seven times round a
                # name/username loop that way before giving up at thirty
                # screens. Short handles make it near-certain.
                holds = getattr(driver, "field_holds", None)
                if callable(holds):
                    ours_on_screen = holds(("username",), identity.username)
                else:
                    ours_on_screen = identity.username.lower() in text
                if (username_filled and ours_on_screen
                        and not any(marker in text
                                    for marker in _USERNAME_TAKEN_MARKERS)
                        and any(marker in text
                                for marker in _USERNAME_VALID_MARKERS)):
                    # Instagram has already accepted what is in the box, so
                    # there is nothing left to type -- only a button to press.
                    # Retyping here is the loop that spent a run's whole
                    # budget while the screen said the handle was valid.
                    username_submits += 1
                    if username_submits == 1:
                        log("info", "instagram accepts %s; submitting",
                            identity.username)
                        _submit_after_typing(driver, _SUBMIT_LABELS)
                    else:
                        # The tap landed and the screen did not move: five
                        # identical reads, `Next` enabled, `input username is
                        # valid` on screen. Same shape as Google's email form,
                        # and the same answer -- submit the field with the
                        # IME's own action, which is the one thing a tap on the
                        # button cannot do.
                        log("info", "tapping Next did not move the username "
                                    "screen; submitting with the keyboard")
                        submitter = getattr(driver, "submit_with_keyboard",
                                            None)
                        if callable(submitter):
                            submitter()
                        else:
                            driver.tap_label(_SUBMIT_LABELS)
                    sleep(8)
                    continue
                if any(marker in text for marker in _USERNAME_TAKEN_MARKERS):
                    # Instagram has rejected this handle, and it will reject it
                    # every time: retyping the same one is what the repeat
                    # guard sees, so the run died "username did not advance in
                    # 4 tries" on a screen that was telling us plainly what was
                    # wrong. Change the handle instead.
                    rejected = identity.username
                    identity.username = next_username(rejected, rejections)
                    rejections += 1
                    log("info", "username %s is taken; trying %s",
                        rejected, identity.username)
                    if rejections > MAX_USERNAME_REJECTIONS:
                        return finish(RESULT_STUCK,
                                      f"{rejections} usernames rejected in a "
                                      f"row, last {rejected}")
                if username_filled and not ours_on_screen:
                    # Retyping after Instagram put its suggestion back. The
                    # submit counter resets with it: the next submit is a first
                    # attempt at this value, and going straight to the keyboard
                    # route would skip the tap that works everywhere else.
                    log("info", "the box holds instagram's suggestion again; "
                                "retyping %s", identity.username)
                    username_submits = 0
                driver.fill(("username",), identity.username, "username")
                username_filled = True
                _submit_after_typing(driver, _SUBMIT_LABELS)
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

            elif screen == SCREEN_PERMISSION_DIALOG:
                # Android's own dialog. Always deny: contacts sync is what ties
                # these accounts to one another, and notifications buy nothing.
                if not driver.tap_label(("DON'T ALLOW", "Don't allow", "Deny")):
                    log("warning", "no deny button on the permission dialog")
                    driver.tap_label(_SKIP_LABELS)
                sleep(4)

            elif screen == SCREEN_PERMISSIONS:
                # Two variants: one offers `Skip`, the other only `Next`, which
                # leads to Android's dialogs above (where the answer is no).
                if not driver.tap_label(_SKIP_LABELS):
                    driver.tap_label(_SUBMIT_LABELS)
                sleep(5)

            elif screen == SCREEN_COOKIES:
                # Answered rather than skipped: there is no "not now" on it, and
                # it stands between a created account and the app. `Allow all
                # cookies` is the ordinary choice and the one that clears in a
                # single tap; declining is accepted too, and is the fallback
                # only because a screen that will not answer is worse than
                # either answer.
                if not driver.tap_label(("Allow all cookies", "ALLOW ALL COOKIES",
                                         "Allow", "Decline optional cookies",
                                         # Meta's ads-consent gate carries only
                                         # this, and leads on to the choices.
                                         "Get started", "GET STARTED")):
                    log("warning", "nothing to answer on the cookie screen")
                sleep(5)

            elif screen in (SCREEN_PHOTO_PROMPT, SCREEN_FOLLOW,
                            SCREEN_ADD_EMAIL, SCREEN_ADD_PHONE,
                            SCREEN_INTERSTITIAL):
                # All declined. The photo and the first post are the human
                # hand-off, and the email and phone prompts arrive pre-filled
                # with whatever the device knows.
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
