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
from adb_bot.core import adb_commands, human_timing

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
_PKG_RE = re.compile(r"\bpkg=(\S+)")
_WHEN_RE = re.compile(r"\bwhen=(\d+)")
_TEXT_FIELD_RE = re.compile(
    r"android\.(title|text|bigText|subText|summaryText)=")

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

# How many shade reads to make before disturbing Instagram by opening Gmail.
SHADE_FIRST_PASSES = 4


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

    Three things narrow the search, and each one was added because the version
    without it returned a confident wrong answer:

    * **Gmail's records only.** A `dumpsys` dump is thousands of lines of
      numbers from every app on the phone, and Instagram itself posts pushes
      that mention its own name.
    * **The human-readable fields only.** The code is in the subject Gmail
      posts; the rest of a record is machine data.
    * **The newest of them.** Several codes accumulate in one thread and only
      the last is live; an older one is already spent, and typing it drops the
      login back to the password screen looking like a bad password.
    """
    if not dump:
        return ""
    best_when, best_code = -1, ""
    when, package = -1, ""
    for line in dump.splitlines():
        stripped = line.strip()
        if "NotificationRecord(" in stripped:
            # A new record begins: everything below belongs to it until the
            # next one. Without this the scan was line-at-a-time across the
            # whole dump, and matched an unrelated app's *notification id* --
            # `pkg=com.zixun.cmp ... id=100215` -- on a line that merely
            # happened to contain the word "instagram" too. That id was then
            # typed into Instagram as a security code.
            when, package = -1, ""
            match = _PKG_RE.search(stripped)
            if match:
                package = match.group(1)
            continue
        if stripped.startswith("when="):
            match = _WHEN_RE.search(stripped)
            if match:
                when = int(match.group(1))
            continue
        # Only the human-readable fields, and only Gmail's own records. The
        # code lives in the subject line Gmail posts; every other line in a
        # record is machine data full of six-digit numbers.
        if package != GMAIL_PACKAGE:
            continue
        if not _TEXT_FIELD_RE.match(stripped):
            continue
        if "instagram" not in stripped.lower():
            continue
        match = _CODE_RE.search(stripped)
        if match and when >= best_when:
            # `>=` not `>`: several mails can share a timestamp, and later in
            # the dump is the safer tie-break. The newest code is the live one
            # -- an older one from the same thread is already spent, and
            # typing it puts the login straight back on the password screen.
            best_when, best_code = when, match.group(1)
    return best_code


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

# Gmail's settings, reached without its banner. `Gmail2PreferenceActivity` is
# not exported (`am start` throws), so this is the way in. Confirmed on
# `Blank caio 2`, 2026-08-18.
GMAIL_SETTINGS_ACTIVITY = (
    f"{GMAIL_PACKAGE}/com.android.mail.ui.settings.PublicPreferenceActivity")
_DATA_USAGE_LABELS = ("Data usage", "DATA USAGE")

# How many screens down to look for a settings row. The account page opens on
# inbox and notification settings and `Data usage` is well below the fold --
# three swipes covered it on `Blank caio 2`, and a page that has not shown the
# row by then is not the page we think it is.
#
# Four was not enough: `hgemranoo@gmail.com`'s account page (2026-08-25) carries
# extra Chat/Meet/"smart features" rows -- inbox settings, notifications,
# general, chat, default reply, signature, conversation view, smart features,
# package tracking, smart compose/reply, out-of-office, Meet's own "limit data
# usage" toggle -- and still had not reached the `Data usage` *section* by
# scroll 4, so `enable_sync()` gave up right before it, twice. Matched to
# `MAX_SYNC_SCROLLS` below, already calibrated for this same page.
MAX_SETTINGS_SCROLLS = 8

# Two goes at turning sync on. If the switch cannot be found twice, the run is
# better off saying so than tapping around Android's settings.
MAX_SYNC_ATTEMPTS = 2

# Only this exact label on the settings route. `_SYNC_SWITCH_LABELS` ends in a
# bare "Sync", which is safe beside a banner that has already named the account
# but not on a settings page where several rows begin with the word.
_SYNC_GMAIL_LABEL = ("Sync Gmail",)

# Gmail's settings, reached without needing the banner. Gmail's own
# `.Gmail2PreferenceActivity` is NOT exported and `am start` throws on it, so
# the way in is this public alias, which lands on the same screen.
GMAIL_SETTINGS_COMPONENT = (
    f"{GMAIL_PACKAGE}/com.android.mail.ui.settings.PublicPreferenceActivity")

# The per-account page is long -- Gmail's rows, then Meet's -- and `Sync Gmail`
# sits under `Data usage` near the bottom. Eight pages is more than it takes by
# hand and few enough that a page which will not scroll ends the attempt.
MAX_SYNC_SCROLLS = 8

# What `ensure_sync_on` can conclude.
RESULT_SYNC_ALREADY_ON = "already_on"
RESULT_SYNC_TURNED_ON = "turned_on"
RESULT_SYNC_UNKNOWN = "unknown"      # the authority row was not there to read
RESULT_SYNC_FAILED = "failed"

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
        """Resume Instagram's existing task, without restarting it.

        `am start -n .../MainTabActivity` names an activity, which brings up a
        fresh main tab and **discards a signup in progress**. Measured
        2026-08-26 on Rene 41: the emailed code was typed and accepted, the
        switch back landed on "Join Instagram / Get started", and the flow then
        looped -- re-entering the email, drawing a new code, submitting it, and
        being reset again, burning a code each time.

        The LAUNCHER intent resumes whatever task the app already has, which is
        what "switch apps, never force-stop" is actually asking for: the
        force-stop was already avoided, but naming the activity undid the
        signup just as thoroughly.
        """
        self._log("info", "switching back to Instagram")
        # `-n <component>`, because `am start` wants a component and treats a
        # bare package as data -- passing one switches nothing at all, which
        # leaves Gmail in front and the next screen read reports an unknown
        # screen (measured 2026-08-26).
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

    def enable_sync(self) -> bool | None:
        """Turn `Sync Gmail` on from Gmail's settings, and say whether it took.

        Every account added to a phone arrives with mail sync **off**, so this
        is the state a run with a *new* mailbox always starts in -- and refusing
        the mailbox there means no new email can ever be used inside one launch.

        Not the banner path `_turn_sync_on` takes: that banner is offered once
        and is gone by the time anything else on the screen has been tapped.
        This walks Gmail's own settings instead -- the account, then Data usage,
        then the switch -- and then asks the sync manager rather than believing
        the screen, because a checkbox that did not take looks identical to one
        that did.

        Returns what `sync_enabled` returns afterwards: True, False, or None for
        "could not tell".
        """
        if self.driver is None:
            return self.sync_enabled()
        self._log("info", "turning Gmail's sync on for %s", self.address)
        self._start(GMAIL_SETTINGS_ACTIVITY)
        time.sleep(6)
        # The address, then Data usage, then the switch. Each tap redraws, and
        # the settings list is scrollable, so every one gets a fresh read --
        # tapping stale bounds here lands on a neighbouring row.
        for labels in ((self.address,), _DATA_USAGE_LABELS,
                       _SYNC_SWITCH_LABELS):
            if not self._find_and_tap(labels):
                self._log("warning", "no %s row in Gmail's settings",
                          labels[0])
                break
            time.sleep(4)
        # Back to Gmail whatever happened, so a failure leaves the phone where
        # the rest of the run expects it rather than deep in settings.
        for _ in range(3):
            self.adb_client.shell_back(self.target)
            time.sleep(2)
        if not self.in_front():
            self.open_gmail()
        enabled = self.sync_enabled()
        self._log("info", "Gmail sync for %s is now %s", self.address,
                  {True: "on", False: "still off"}.get(enabled, "unreadable"))
        return enabled

    def _find_and_tap(self, labels) -> bool:
        """Tap a settings row, scrolling down until it appears.

        The account's own settings page opens on inbox and notification
        options; `Data usage` -- and the `Sync Gmail` switch under it -- are
        below the fold. Looking only at the first screenful found the address
        and then declared the rest missing, which read as "Gmail has no such
        setting" when it was simply further down.
        """
        for attempt in range(MAX_SETTINGS_SCROLLS + 1):
            self.driver.read_screen()
            if self._tap_row(labels):
                return True
            if attempt < MAX_SETTINGS_SCROLLS:
                self._scroll_down()
        return False

    def _scroll_down(self) -> None:
        """One screenful down the settings list.

        Fixed coordinates rather than a node's bounds: the list fills the page,
        and the thing being scrolled towards is by definition not on screen to
        measure.
        """
        self.adb_client.shell_swipe(
            self.target, 540, 1600, 540, 700,
            duration_ms=human_timing.swipe_duration_ms(350))
        time.sleep(2)

    def _tap_row(self, labels) -> bool:
        """Tap a settings row, strictly first and then by its own bounds.

        Gmail's settings list marks nothing in it clickable -- on 2026-08-18 the
        account row for `cicireynaamelia@gmail.com` was on screen, in the dump,
        and unreachable, so the sync switch behind it could not be turned on and
        a run that had already reached Instagram's code screen was thrown away.
        The strict tap is still tried first: it is the one that cannot land on
        the wrong control.
        """
        if self.driver.tap_label(labels):
            return True
        try:
            return bool(self.driver.tap_label(labels, require_clickable=False))
        except TypeError:
            # A driver that does not know the argument. Not worth failing over:
            # the strict attempt above is the one that usually works.
            return False

    def _scroll_to_sync_switch(self) -> bool:
        """Page down the account's settings until `Sync Gmail` is on screen."""
        for page in range(MAX_SYNC_SCROLLS):
            text = (self._read() or "").lower()
            if "sync gmail" in text:
                return True
            # Down a page, not to the bottom: the switch sits above Meet's own
            # rows, and a swipe to the end scrolls straight past it.
            self._shell(adb_commands.swipe(
                610, 1900, 610, 800, human_timing.swipe_duration_ms(300)))
            time.sleep(2)
            self._log("info", "looking for the sync switch (%d/%d)",
                      page + 1, MAX_SYNC_SCROLLS)
        return False

    def ensure_sync_on(self) -> str:
        """Make sure Gmail will actually fetch mail for this address.

        A Google account signed into one of these phones arrives with mail sync
        **off**, and nothing about the phone says so: Gmail opens on the right
        account and reports "Nothing in Primary", which is what an empty inbox
        looks like too. Instagram's code then never reaches the device, the
        shade and the inbox are empty for the same reason, and the run reports
        "no code arrived" -- which reads as Instagram's fault.

        Deliberately NOT driven from Gmail's "account sync is off" banner: that
        banner is offered once, on the same screen as the welcome tip, and
        dismissing the tip takes the banner with it. This route needs neither.
        """
        state = self.sync_enabled()
        if state is True:
            self._log("info", "%s is already syncing", self.address)
            return RESULT_SYNC_ALREADY_ON
        if state is None:
            # Not "off" -- the authority row simply was not in the dump. Saying
            # so and continuing beats abandoning a phone that was fine.
            self._log("warning", "cannot tell whether %s syncs (%s missing "
                                 "from dumpsys content)",
                      self.address, GMAIL_SYNC_AUTHORITY)
            return RESULT_SYNC_UNKNOWN
        if self.driver is None:
            self._log("warning", "%s is not syncing and there is no driver to "
                                 "turn it on with", self.address)
            return RESULT_SYNC_FAILED

        self._log("info", "%s is not syncing; turning Gmail's sync on",
                  self.address)
        try:
            self._start(GMAIL_SETTINGS_COMPONENT)
            time.sleep(6)
            # require_clickable=False on purpose, and `tap_label`'s own
            # docstring names this exact screen: Gmail's settings list renders
            # the account row's address on a plain view, and the only clickable
            # things on the page are "Navigate up" and "More options". Under
            # the strict rule the row is untappable and the account's settings
            # cannot be reached at all.
            if not self.driver.tap_label((self.address,),
                                         require_clickable=False):
                self._log("warning", "Gmail's settings do not list %s",
                          self.address)
                return RESULT_SYNC_FAILED
            time.sleep(5)
            if not self._scroll_to_sync_switch():
                self._log("warning", "no 'Sync Gmail' row after %d pages",
                          MAX_SYNC_SCROLLS)
                return RESULT_SYNC_FAILED
            # Same reason: the switch's label sits on a plain view beside the
            # checkbox rather than on anything Android marks clickable.
            if not self.driver.tap_label(_SYNC_GMAIL_LABEL,
                                         require_clickable=False):
                return RESULT_SYNC_FAILED

            # Ask the sync manager, not the checkbox: the tick is drawn before
            # the setting is stored, so the screen agrees a moment early.
            for _ in range(4):
                time.sleep(3)
                if self.sync_enabled() is True:
                    self._log("info", "%s is syncing now", self.address)
                    return RESULT_SYNC_TURNED_ON
            self._log("warning", "tapped 'Sync Gmail' but %s is still off",
                      GMAIL_SYNC_AUTHORITY)
            return RESULT_SYNC_FAILED
        finally:
            self.back_to_instagram()
            time.sleep(4)

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
            # Not fatal on its own: this is the state *every* freshly added
            # account is in, so refusing here would mean a new mailbox could
            # never be used inside the one launch a phone lives for.
            if self.enable_sync() is False:
                raise MailboxNotReady(
                    f"Gmail sync is off for {self.address} (dumpsys content: "
                    f"{GMAIL_SYNC_AUTHORITY} enabled=false) and the switch "
                    f"would not go on, so no mail can reach this phone -- "
                    f"Gmail > Settings > {self.address} > Data usage > "
                    f"'Sync Gmail'")

        # The shade FIRST, before Gmail is opened at all. Opening Gmail takes
        # Instagram off the screen, and coming back is what actually breaks a
        # signup: `MainTabActivity` is Instagram's launcher activity, so
        # bringing it forward resets the task and the half-finished signup is
        # gone -- measured 2026-08-26 on Rene 41, where the code was accepted
        # and the app returned to "Join Instagram", then looped drawing a fresh
        # code each lap. On a phone whose Gmail is already prepared (signed in,
        # sync on, tours dismissed) the mail lands in the shade and Instagram
        # never loses the foreground.
        for _ in range(SHADE_FIRST_PASSES):
            code = self.notification_code()
            if code:
                self._log("info", "code found in the notification shade "
                                  "without leaving Instagram")
                return code
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_seconds)

        # Nothing in the shade -- Gmail may need its sync turned on or its
        # tours cleared, which can only be done with it in front.
        self._log("info", "no code in the shade yet; opening Gmail")
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
