"""Read Instagram's confirmation code out of the Gmail app on the phone itself.

The email half of signup needs one thing SMS gets for free: somewhere to read
the code. Three ways were tried on 2026-08-13 and only this one works from this
fleet:

* **IMAP with the mailbox password** -- refused. Google answers
  `Application-specific password required` on a mailbox with 2-Step
  Verification, and `Invalid credentials` on one without (re-checked
  2026-08-16 against five of the base's own addresses, both kinds; every one
  refused). Account passwords do not open IMAP any more.
* **Chrome on the profile** -- cannot reach `mail.google.com` at all
  (`ERR_SSL_PROTOCOL_ERROR`).
* **The Gmail app on the phone** -- works, and is what this module drives.

The mailbox has to be signed in on the profile first (see
`signup_mailbox.py`); this module assumes that and only reads.

Four things this has to get right, each of which produced a *plausible wrong
answer* on the runs that found them:

* **Whose inbox is this?** The phones ship with a resident Google account, and
  the one on the `gmail test` profile held an Instagram code of its own from
  5 August. A regex over "whatever inbox is open" returns somebody else's six
  digits and the flow types them in with total confidence. So the address is
  confirmed before a code is accepted.
* **A fresh account syncs nothing.** An account just added to the phone arrives
  with sync off: the inbox is empty and Gmail says so in a *tip*, not an error.
  "No mail" and "not syncing yet" have to be different answers.
* **Switch apps with `am start`, never `force-stop`.** A backgrounded signup
  survives; a killed one returns to "Join Instagram" and throws away whatever
  was already verified.
* **`monkey` starts nothing on these phones** -- confirmed three separate runs.
  Always the explicit intent.
"""

from __future__ import annotations

import re
import time

from adb_bot.automation.flows import play_install

GMAIL_PACKAGE = "com.google.android.gm"
GMAIL_ACTIVITY = f"{GMAIL_PACKAGE}/.ConversationListActivityGmail"
INSTAGRAM_PACKAGE = "com.instagram.android"
INSTAGRAM_ACTIVITY = f"{INSTAGRAM_PACKAGE}/.activity.MainTabActivity"

# Instagram's own wording in the mail. Kept broad on the sender and narrow on
# the shape of the code, because the subject line varies by locale while the
# six digits do not.
_INSTAGRAM_HINTS = (
    "instagram",
    "is your instagram code",
    "verify your account",
    "confirm your email",
)

# A standalone six-digit run. `\b` on both sides so a longer number -- an order
# id, a year range, a phone number -- cannot supply a "code".
_CODE_RE = re.compile(r"\b(\d{6})\b")

# What Gmail says when an account is on the phone but not syncing. This is a
# tip, not an error, and it renders where a person expects to see mail.
_NOT_SYNCING_MARKERS = (
    "sync is off",
    "gmail sync is off",
    "turn on sync",
    "sync gmail",
)

_EMPTY_MARKERS = (
    "no new mail",
    "nothing in your inbox",
    "no mail here",
    "you're all caught up",
)


class MailboxNotReady(Exception):
    """The phone has the account but is not showing its mail."""


class WrongMailbox(Exception):
    """The inbox on screen belongs to somebody other than the account we mean."""


def inbox_shows_address(text: str | None, address: str) -> bool:
    """Whether the screen on show is `address`'s inbox.

    Gmail writes the signed-in address into the avatar's content description
    (`Signed in as <name> <address>`) and into the account switcher. Matching
    the local part alone would accept a different `@` domain, so the whole
    address is required.
    """
    if not (text and address):
        return False
    return address.lower() in text.lower()


def sync_is_off(text: str | None) -> bool:
    """Whether Gmail is telling us the account is not syncing.

    Separate from "no mail" on purpose: one is fixed by waiting, the other by
    turning a switch on, and a flow that confuses them waits forever.
    """
    if not text:
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _NOT_SYNCING_MARKERS)


def looks_empty(text: str | None) -> bool:
    if not text:
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _EMPTY_MARKERS)


def find_code(text: str | None, address: str = "") -> str:
    """The Instagram confirmation code in `text`, or "".

    Requires Instagram to be named somewhere on the screen. A six-digit number
    on its own is not evidence -- an inbox is full of numbers, and the whole
    failure this guards against is confidently typing the wrong one.
    """
    if not text:
        return ""
    haystack = text.lower()
    if not any(hint in haystack for hint in _INSTAGRAM_HINTS):
        return ""

    # Prefer a code that sits in the same sentence as Instagram's name: an
    # inbox list shows several messages at once, and the newest is not reliably
    # the first thing in a flattened dump.
    for line in re.split(r"[\n.;]", text):
        low = line.lower()
        if "instagram" in low:
            match = _CODE_RE.search(line)
            if match:
                return match.group(1)

    match = _CODE_RE.search(text)
    return match.group(1) if match else ""


# A Gmail installed one minute ago opens on a welcome tour, not on an inbox.
# `Blank caio 2` landed on "new in gmail -- all the features you love with a
# fresh new look -- got it" (2026-08-17), which the ownership check read as
# somebody else's mailbox, correctly refusing to trust it.
_ONBOARDING_MARKERS = (
    "new in gmail",
    "welcome to gmail",
    "all the features you love",
    "take me to gmail",
    "meet the new gmail",
)

_TOUR_BUTTONS = ("Got it", "GOT IT", "Take me to Gmail", "TAKE ME TO GMAIL",
                 "Next", "NEXT", "OK", "Continue", "CONTINUE", "Done")

# Enough for a multi-page tour, few enough that a screen which simply will not
# move on ends the run instead of eating the phone.
MAX_TOUR_TAPS = 6

# Backing out of a compose window should take one press. More than a few means
# Gmail is not going to show a mailbox on this phone.
MAX_COMPOSE_ESCAPES = 3


def is_onboarding(text: str) -> bool:
    """Is this Gmail's welcome tour rather than a mailbox?"""
    haystack = (text or "").lower()
    return any(marker in haystack for marker in _ONBOARDING_MARKERS)


_COMPONENT_RE = re.compile(rf"{re.escape(GMAIL_PACKAGE)}/[\w.$]+")

# Words that mark the activity you actually want to land on. `dumpsys package`
# lists dozens of components; starting each of them in turn would spend more of
# the phone's life than the whole signup.
#
# "mail" is deliberately not among them: every component of Gmail contains it,
# including `.ComposeActivityGmailExternal`, which is what this actually opened
# on 2026-08-17 -- and a compose window shows the address in its `From` field,
# so it passed for the right inbox and was read for 210 seconds.
_LIKELY_LAUNCHERS = ("conversationlist", "conversation", "main")

# Components that are Gmail but are not a mailbox. Never started.
_NOT_A_MAILBOX = ("compose", "widget", "settings", "provider", "service",
                  "receiver", "share", "search", "account")


def _components(text: str, limit: int = 3) -> list:
    """Gmail components named anywhere in `text`, likeliest first."""
    seen, out = set(), []
    for match in _COMPONENT_RE.findall(text or ""):
        name = match.lower()
        if match in seen or any(word in name for word in _NOT_A_MAILBOX):
            continue
        seen.add(match)
        out.append(match)
    out.sort(key=lambda name: any(word in name.lower()
                                  for word in _LIKELY_LAUNCHERS),
             reverse=True)
    return out[:limit]


# What a compose window says. It carries the address in its `From` field, so
# nothing that merely looks for the address can tell it from an inbox.
_COMPOSE_MARKERS = ("compose email", "attach files", "add cc/bcc",
                    "to add cc")


def looks_like_compose(text: str) -> bool:
    """Is this Gmail's compose window rather than a list of mail?"""
    haystack = (text or "").lower()
    return sum(marker in haystack for marker in _COMPOSE_MARKERS) >= 2


class PhoneMailbox:
    """Reads one address's Instagram code off one phone.

    Deliberately not a general mail client: it switches to Gmail, reads, and
    switches back, and every answer it can give is one the signup flow knows
    what to do with.
    """

    def __init__(self, target: str, adb_client, address: str, logger=None,
                 driver=None) -> None:
        self.target = target
        self.adb_client = adb_client
        self.address = address
        self.logger = logger
        self.driver = driver
        self.e164 = ""              # so an SMS lease and this look alike

    def _log(self, level, message, *args):
        if self.logger is not None:
            getattr(self.logger, level)("mailbox: " + message, *args)

    def _shell(self, command: str) -> str:
        return self.adb_client.run_command(
            f"adb -s {self.target} shell {command}") or ""

    def installed(self) -> bool:
        """Is Gmail on this phone at all?

        Exact name: `pm list packages com.google.android.gm` also matches
        **com.google.android.gms**, Play Services, which every phone has.
        """
        out = self._shell(f"pm list packages {GMAIL_PACKAGE}")
        return GMAIL_PACKAGE in play_install.packages_named(out)

    def in_front(self) -> bool:
        """Is Gmail the window actually being drawn?

        The check that matters, and the one whose absence cost a whole launch
        on 2026-08-17. Gmail was not installed, `am start` failed with "Activity
        class ... does not exist", nothing said so, and the flow spent 210
        seconds reading **Instagram's** screen instead. Instagram's own
        confirmation page says "we sent to <address>", so even the "is this our
        inbox?" check passed on it.
        """
        # `dumpsys window`, not `dumpsys window windows` -- the latter answers
        # nothing on these phones, which reads as "not in front" forever.
        focus = self._shell("dumpsys window | grep mCurrentFocus")
        return GMAIL_PACKAGE in (focus or "")

    def open_gmail(self) -> bool:
        """Bring Gmail up, and say whether it arrived.

        `am start`, never `monkey`, never `force-stop`. The hard-coded activity
        is tried first and the package manager asked only if that fails --
        Gmail's launch activity has been renamed before.
        """
        self._log("info", "switching to Gmail")
        self._shell(f"am start -n {GMAIL_ACTIVITY}")
        time.sleep(6)
        if self.in_front():
            return True

        # The hard-coded activity is gone on these phones -- `am start` answers
        # "Activity class ... does not exist" -- so ask the package manager
        # what Gmail's launcher actually is.
        # Both the resolver and the package dump name the launcher activity,
        # in different formats and neither reliably, so the component is picked
        # out of whatever text comes back rather than by line position.
        # `monkey` is deliberately not a fallback: it starts nothing on these
        # phones, confirmed four separate times.
        for query in (f"cmd package resolve-activity --brief {GMAIL_PACKAGE}",
                      f"dumpsys package {GMAIL_PACKAGE}"):
            for component in _components(self._shell(query) or ""):
                self._log("info", "starting Gmail as %s", component)
                self._shell(f"am start -n {component}")
                time.sleep(6)
                if self.in_front():
                    return True
        return False

    def back_to_instagram(self) -> None:
        self._log("info", "switching back to Instagram")
        self._shell(f"am start -n {INSTAGRAM_ACTIVITY}")

    def _read(self) -> str:
        if self.driver is None:
            return ""
        return self.driver.read_screen() or ""

    def wait_for_code(self, timeout: int = 180, poll_seconds: int = 15) -> str:
        """The code, or "" if none arrived inside `timeout`.

        Signature matches the SMS lease's so `run_signup` can hold either.
        Always returns Instagram to the foreground, including on the way out of
        a failure -- leaving Gmail in front would make the next screen read
        report an unknown screen and end a run that was fine.
        """
        deadline = time.monotonic() + timeout
        if not self.installed():
            raise MailboxNotReady(
                f"Gmail is not installed on this phone, so {self.address} "
                f"cannot be read here")
        arrived = self.open_gmail()
        time.sleep(8)
        if not arrived and not self.in_front():
            raise MailboxNotReady(
                "Gmail would not come to the front; refusing to read the "
                "screen, which is still Instagram's")

        try:
            checked_owner, tours, escapes = False, 0, 0
            while time.monotonic() < deadline:
                # Re-checked every pass, not once: Instagram's confirmation
                # page names the address too, so a read taken while it is in
                # front looks exactly like the right inbox.
                if not self.in_front():
                    self._log("info", "Gmail slipped out of the front; "
                                      "bringing it back")
                    self.open_gmail()
                    time.sleep(6)
                    continue

                text = self._read()

                # A compose window is Gmail, is in front, and carries the
                # address in its `From` field -- everything the ownership check
                # looks for, and no mail on it at all. Back out of it.
                if looks_like_compose(text):
                    escapes += 1
                    if escapes > MAX_COMPOSE_ESCAPES:
                        raise MailboxNotReady(
                            "Gmail keeps opening its compose window instead of "
                            "a mailbox")
                    self._log("info", "backing out of Gmail's compose window "
                                      "(%d/%d)", escapes, MAX_COMPOSE_ESCAPES)
                    self.adb_client.shell_back(self.target)
                    time.sleep(4)
                    continue

                # A freshly installed Gmail opens on its own welcome tour, not
                # on an inbox. Click through it before judging whose mail this
                # is -- otherwise the tour reads as "somebody else's inbox".
                if is_onboarding(text):
                    tours += 1
                    if tours > MAX_TOUR_TAPS:
                        raise MailboxNotReady(
                            f"Gmail is still showing its welcome tour after "
                            f"{MAX_TOUR_TAPS} taps: {text[:160]}")
                    self._log("info", "clicking through Gmail's welcome tour "
                                      "(%d/%d)", tours, MAX_TOUR_TAPS)
                    if self.driver is not None:
                        self.driver.tap_label(_TOUR_BUTTONS)
                    time.sleep(5)
                    continue

                if not checked_owner:
                    if inbox_shows_address(text, self.address):
                        checked_owner = True
                        self._log("info", "reading %s", self.address)
                    elif sync_is_off(text):
                        raise MailboxNotReady(
                            f"{self.address} is on the phone but Gmail is not "
                            f"syncing it -- account settings, Data usage, "
                            f"'Sync Gmail'")
                    elif text.strip():
                        # Somebody's inbox, but we cannot prove it is ours.
                        # These phones carry a resident Google account whose
                        # inbox has held an Instagram code before.
                        raise WrongMailbox(
                            f"the inbox on screen is not {self.address}: "
                            f"{text[:160]}")

                code = find_code(text, self.address)
                if code:
                    self._log("info", "code found in the mailbox")
                    return code

                if sync_is_off(text):
                    raise MailboxNotReady(
                        f"{self.address} is not syncing -- turn 'Sync Gmail' on")

                self._log("info", "no code yet; waiting")
                time.sleep(poll_seconds)
            return ""
        finally:
            self.back_to_instagram()
            time.sleep(6)

    def release(self, count_failure: bool = False) -> None:
        """No-op, so an email run and an SMS run release the same way."""
        return None
