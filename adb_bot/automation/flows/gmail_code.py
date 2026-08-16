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

    def open_gmail(self) -> None:
        """Bring Gmail up. `am start`, never `monkey`, never `force-stop`."""
        self._log("info", "switching to Gmail")
        self._shell(f"am start -n {GMAIL_ACTIVITY}")

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
        self.open_gmail()
        time.sleep(8)

        try:
            checked_owner = False
            while time.monotonic() < deadline:
                text = self._read()

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
