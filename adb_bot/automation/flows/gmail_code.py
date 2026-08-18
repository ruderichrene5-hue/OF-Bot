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
    # What a synced-but-empty Primary tab actually says on these phones, seen
    # on `Blank caio 2` 2026-08-18. Its absence here is why an inbox that was
    # never syncing could not be told from one with no mail in it yet.
    "nothing in primary",
    "you've finished!",
)

# The authority Gmail syncs a Google account's mail under. `dumpsys content`
# prints one row per authority as `name  syncable  enabled  ...`, and that row
# is the only honest answer to "will mail arrive on this phone?".
GMAIL_SYNC_AUTHORITY = "gmail-ls"


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


def code_from_notifications(dump: str | None) -> str:
    """Instagram's code out of a `dumpsys notification` dump, or "".

    The cheapest and least ambiguous place to look: no app has to be in front,
    so there is no welcome tour, no compose window and no "is this our inbox?"
    to get wrong. Gmail posts the mail's subject -- "123456 is your Instagram
    code" -- and the shade keeps it whatever else is on screen.

    Only ever a code on the same line as Instagram's name. A `dumpsys` dump is
    thousands of lines of numbers, so there is deliberately no fallback to
    "any six digits somewhere in the text".
    """
    if not dump:
        return ""
    for line in dump.splitlines():
        if "instagram" in line.lower():
            match = _CODE_RE.search(line)
            if match:
                return match.group(1)
    return ""


def sync_enabled_in_dump(dump: str | None) -> bool | None:
    """Whether `gmail-ls` is enabled, from a `dumpsys content` dump.

    True/False when the authority row is present, None when it is not -- and
    None must never be read as False, because "I could not tell" and "sync is
    off" call for different actions.

    The row looks like this, columns being authority, syncable, enabled::

        gmail-ls    -1    false   Total  0  0  0 ...

    `syncable=-1` means Android has not yet decided, which is the state a
    freshly signed-in account sits in; it says nothing about whether mail will
    arrive, so only the `enabled` column is read here.
    """
    if not dump:
        return None
    for line in dump.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == GMAIL_SYNC_AUTHORITY:
            if fields[2] in ("true", "false"):
                return fields[2] == "true"
    return None


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
    # A second promo, behind the first: "close google meet, now in gmail --
    # video meetings with live captioning and screen sharing for up to 100
    # people" (`Blank caio 2`, 2026-08-17). Gmail stacks these.
    "now in gmail",
    "video meetings with live captioning",
    # And a third, on the inbox itself: "welcome to your new inbox -- mail
    # categories group messages of the same type". It sits over the message
    # list, which is the only part of the screen worth reading.
    "welcome to your new inbox",
    "mail categories group messages",
)

_TOUR_BUTTONS = ("Got it", "GOT IT", "Take me to Gmail", "TAKE ME TO GMAIL",
                 "Close", "CLOSE", "Dismiss tip", "Dismiss", "No thanks",
                 "NO THANKS", "Next", "NEXT", "OK", "Continue", "CONTINUE",
                 "Done")

# The banner Gmail shows when the account is on the phone but not syncing --
# tappable, and it leads to the switch that fixes it.
_SYNC_BANNER_LABELS = ("Account sync is off. Turn it on in Account settings.",
                       "Account sync is off", "Turn it on", "TURN ON")
_SYNC_SWITCH_LABELS = ("Sync Gmail", "Sync mail", "Sync")

# Two goes at turning sync on. If the switch cannot be found twice, the run is
# better off saying so than tapping around Android's settings.
MAX_SYNC_ATTEMPTS = 2

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
                  "receiver", "share", "search", "account", "send")

# How long to let Gmail take. A just-installed Gmail is unpacking and doing its
# first sync, and six seconds was not enough: the check said "not in front",
# the next candidate was started over the top of it, and the one that finally
# won was whatever started fastest rather than the mailbox (2026-08-17).
FRONT_WAIT_SECONDS = 24
FRONT_POLL_SECONDS = 3

# What was actually blocking Gmail for five launches: it starts, immediately
# asks for a runtime permission, and its own dialog sits in front of it. The
# focused window is the permission controller's, so Gmail never "arrives" and
# every candidate activity after it is started behind the same dialog.
PERMISSION_PACKAGE = "com.android.permissioncontroller"

# Notifications are the permission that matters here -- the code is read from
# the shade -- and it can be granted without a dialog at all.
NOTIFICATION_PERMISSION = "android.permission.POST_NOTIFICATIONS"

_ALLOW_BUTTONS = ("Allow", "ALLOW", "While using the app", "Only this time",
                  "Continue", "CONTINUE", "OK")

# Gmail asks for a couple of permissions in a row. More than a few means the
# dialog is not going away.
MAX_PERMISSION_DIALOGS = 4


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
        # Granted outright rather than tapped: the dialog is the thing that
        # blocks Gmail, and this is the permission the shade read depends on.
        self._shell(f"pm grant {GMAIL_PACKAGE} {NOTIFICATION_PERMISSION}")
        self._start(GMAIL_ACTIVITY)
        if self._wait_in_front():
            return True

        # The hard-coded activity is gone on these phones -- `am start` answers
        # "Activity class ... does not exist" -- so ask the package manager
        # what Gmail's launcher actually is.
        # Both the resolver and the package dump name the launcher activity,
        # in different formats and neither reliably, so the component is picked
        # out of whatever text comes back rather than by line position.
        # `monkey` is deliberately not a fallback: it starts nothing on these
        # phones, confirmed four separate times.
        # Ask about the *launcher* intent specifically. A bare
        # `resolve-activity` resolves an empty intent and hands back whatever
        # matches that, which is not necessarily something `am start` can even
        # open -- three runs on 2026-08-17 chased components that were never
        # going to come up.
        launcher = ("cmd package resolve-activity --brief "
                    "-a android.intent.action.MAIN "
                    "-c android.intent.category.LAUNCHER " + GMAIL_PACKAGE)
        for query in (launcher, f"dumpsys package {GMAIL_PACKAGE}"):
            for component in _components(self._shell(query) or ""):
                self._log("info", "starting Gmail as %s", component)
                self._start(component)
                if self._wait_in_front():
                    return True
        return False

    def _start(self, component: str) -> None:
        """`am start`, saying so when the phone refuses.

        The output used to be thrown away, so "Activity class ... does not
        exist" -- the single most useful line in the whole chain -- never
        reached the log.
        """
        out = self._shell(f"am start -n {component}") or ""
        if "does not exist" in out or "Error" in out:
            self._log("warning", "the phone would not start %s: %s",
                      component, out.strip()[:160])

    def _wait_in_front(self, seconds: int = FRONT_WAIT_SECONDS) -> bool:
        """Give Gmail time to arrive before deciding it did not."""
        waited, dialogs = 0, 0
        while True:
            if self.in_front():
                return True
            # A permission dialog in front is Gmail's own, and Gmail is right
            # behind it. Clearing it is progress, so it does not count against
            # the wait -- but only a few times, since a dialog that will not go
            # away is its own kind of stuck.
            if dialogs < MAX_PERMISSION_DIALOGS and self._clear_permission_dialog():
                dialogs += 1
                self._log("info", "cleared a permission dialog in front of "
                                  "Gmail (%d/%d)", dialogs,
                          MAX_PERMISSION_DIALOGS)
                time.sleep(FRONT_POLL_SECONDS)
                continue
            if waited >= seconds:
                # What *is* in front, and is Gmail even alive? "Did not come
                # up" was all the log said for four launches, and it does not
                # distinguish a crash from a window that never drew.
                focus = self._shell("dumpsys window | grep mCurrentFocus")
                alive = self._shell(f"pidof {GMAIL_PACKAGE}")
                self._log("warning", "Gmail did not reach the front in %ds; "
                                     "focus=%s pid=%s", seconds,
                          (focus or "<none>").strip()[:120],
                          (alive or "<not running>").strip()[:40])
                return False
            time.sleep(FRONT_POLL_SECONDS)
            waited += FRONT_POLL_SECONDS

    def back_to_instagram(self) -> None:
        self._log("info", "switching back to Instagram")
        self._shell(f"am start -n {INSTAGRAM_ACTIVITY}")

    def _read(self) -> str:
        if self.driver is None:
            return ""
        return self.driver.read_screen() or ""

    def permission_dialog_in_front(self) -> bool:
        focus = self._shell("dumpsys window | grep mCurrentFocus")
        return PERMISSION_PACKAGE in (focus or "")

    def _clear_permission_dialog(self) -> bool:
        """Allow whatever Gmail is asking for. True if a dialog was cleared."""
        if self.driver is None or not self.permission_dialog_in_front():
            return False
        self.driver.read_screen()
        return bool(self.driver.tap_label(_ALLOW_BUTTONS))

    def _turn_sync_on(self) -> bool:
        """Follow Gmail's own "account sync is off" banner to the switch.

        The banner is tappable and leads to the account's settings, where
        `Sync Gmail` is a checkbox. Returns whether the switch was found and
        tapped -- and always comes back to Gmail, so a failed attempt leaves
        the phone where it started rather than in Android's settings.
        """
        if self.driver is None:
            return False
        if not self.driver.tap_label(_SYNC_BANNER_LABELS):
            return False
        time.sleep(6)
        self.driver.read_screen()
        turned = bool(self.driver.tap_label(_SYNC_SWITCH_LABELS))
        time.sleep(3)
        self.adb_client.shell_back(self.target)
        time.sleep(4)
        if not self.in_front():
            self.open_gmail()
        return turned

    def notification_code(self) -> str:
        """Instagram's code from the notification shade, or ""."""
        return code_from_notifications(
            self._shell("dumpsys notification --noredact"))

    def sync_enabled(self) -> bool | None:
        """Whether Gmail will actually fetch mail here. None = could not tell.

        Asked of the sync manager rather than of Gmail's screen, because the
        screen only offers its "account sync is off" banner once: dismiss it --
        or dismiss the welcome tip stacked on top of it -- and an inbox that
        can never fill looks exactly like one that simply has no mail yet.
        """
        return sync_enabled_in_dump(self._shell("dumpsys content"))

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

        # Asked once, before any waiting. On 2026-08-18 `Blank caio 2` sat here
        # for its whole 210-second budget against `gmail-ls enabled=false`: no
        # mail could arrive, the shade stayed empty, the inbox stayed empty,
        # and the run reported "no code arrived" -- which reads as Instagram's
        # fault and is not. Turning the switch on made six Instagram codes
        # appear at once, the oldest six days old.
        if self.sync_enabled() is False:
            raise MailboxNotReady(
                f"Gmail sync is off for {self.address} (dumpsys content: "
                f"{GMAIL_SYNC_AUTHORITY} enabled=false), so no mail can reach "
                f"this phone -- Gmail > Settings > {self.address} > Data usage "
                f"> 'Sync Gmail'")

        # Gmail not coming to the front is no longer fatal: the notification
        # shade carries the same code and needs nothing in front at all. Four
        # launches on 2026-08-17 ended here with the mail very likely already
        # delivered.
        on_screen = self.open_gmail()
        if not on_screen:
            self._log("warning", "Gmail would not come to the front; reading "
                                 "the notification shade only")

        try:
            checked_owner, tours, escapes, syncs = False, 0, 0, 0
            while time.monotonic() < deadline:
                # The shade first, every pass. It is the one place that cannot
                # be a welcome tour, a compose window or somebody else's inbox.
                code = self.notification_code()
                if code:
                    self._log("info", "code found in the notification shade")
                    return code

                if not on_screen:
                    time.sleep(poll_seconds)
                    continue

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

                # Sync before the tour, because they arrive on the same screen
                # and the tour's button takes the banner with it. On 2026-08-18
                # `Blank caio 2` rendered "account sync is off" and "welcome to
                # your new inbox" together; the tour branch won, tapped `OK`,
                # and the banner never came back. `sync_is_off` was then false
                # for every later pass, the "Signed in as ..." header satisfied
                # the ownership check, and the run polled an inbox that could
                # never fill for its whole 210-second budget before reporting
                # `mailbox`. The tour is cosmetic and its buttons keep working
                # a pass later; the switch is the one thing on that screen the
                # rest of the run depends on.
                if sync_is_off(text):
                    if syncs < MAX_SYNC_ATTEMPTS and self._turn_sync_on():
                        syncs += 1
                        self._log("info", "turned Gmail's sync on (%d/%d)",
                                  syncs, MAX_SYNC_ATTEMPTS)
                        time.sleep(poll_seconds)
                        continue
                    raise MailboxNotReady(
                        f"{self.address} is on the phone but Gmail is not "
                        f"syncing it -- account settings, Data usage, "
                        f"'Sync Gmail'")

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
                        if syncs < MAX_SYNC_ATTEMPTS and self._turn_sync_on():
                            syncs += 1
                            time.sleep(poll_seconds)
                            continue
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
                    # Gmail says so on a banner that is itself the way to fix
                    # it, so try the switch before giving up on the mailbox.
                    if syncs < MAX_SYNC_ATTEMPTS and self._turn_sync_on():
                        syncs += 1
                        self._log("info", "turned Gmail's sync on (%d/%d)",
                                  syncs, MAX_SYNC_ATTEMPTS)
                        time.sleep(poll_seconds)
                        continue
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
