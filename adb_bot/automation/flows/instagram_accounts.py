"""Instagram in-app account switching, for phones that carry two IG accounts.

Some MLX profiles are one cloud phone running one Instagram install with *two*
accounts logged in -- the same model, a second handle. MultiLogin marks them
with a profile tag ("Second Account" on the Jil/Jasmin phones, "2 accounts" on
the Nikki ones); see :mod:`adb_bot.automation.second_accounts`. Posting to both
is how a phone we already pay for produces twice the reels.

The switch itself is the account picker behind the profile header:

    profile tab
      -> action_bar_username_container   (clickable; holds the handle + chevron)
         -> bottom sheet listing every logged-in account

The sheet's shape, confirmed on device (Jasmin 5, IG on Android 16):

    RecyclerView
      ViewGroup  clickable  content-desc="jasmindiecoolee"                    <- active
        View     text="jasmindiecoolee"
      ViewGroup  clickable  content-desc="naughty_jasminn, 1 follow and 16 more"
        View     text="naughty_jasminn"
        View     text="1 follow and 16 more"
      Button     clickable  content-desc="Add Instagram account"
      Button     clickable  content-desc="Go to Accounts Center"

Account rows are ``ViewGroup``; the two things we must never tap are ``Button``.
That class split is what :func:`parse_switcher_accounts` keys on, so a build
that renames the labels (or localises them) still can't trick the parser into
tapping "Add Instagram account" -- which would walk into a signup form on a
production account. The label deny-list is a second belt on top of it.

Nothing here posts. The flow calls :func:`ensure_account` after Instagram is up
and before the composer opens; if it cannot prove the intended handle is
active, it returns False and the caller must abandon the post rather than post
as the wrong account.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# The profile header's handle + the container that opens the picker.
ACTION_BAR_TITLE = "com.instagram.android:id/action_bar_title"
ACTION_BAR_USERNAME_CONTAINER = "com.instagram.android:id/action_bar_username_container"

PROFILE_TAB_SELECTORS = (
    {"resourceId": "com.instagram.android:id/profile_tab"},
    {"resourceIdMatches": r"com\.instagram\.android:id/(profile_tab|main_profile_tab)"},
    {"descriptionStartsWith": "Profile"},
)

# Tapping the handle opens the sheet. The container is the clickable node; the
# title itself is a non-clickable TextView inside it, so it only works by
# virtue of the tap landing on the parent -- ask for the container first.
SWITCHER_TRIGGER_SELECTORS = (
    {"resourceId": ACTION_BAR_USERNAME_CONTAINER},
    {"resourceId": ACTION_BAR_TITLE},
    {"resourceIdMatches": r"(?i)com\.instagram\.android:id/.*action_bar.*(username|title).*"},
)

# Rows in the sheet that are actions, not accounts. Matched case-insensitively
# against the row's content-desc/text. The class check already excludes these;
# this is the second line of defence, because tapping "Add Instagram account"
# on a live phone starts an account-creation flow.
_NON_ACCOUNT_LABELS = (
    "add instagram account", "add account", "log into an existing account",
    "go to accounts center", "accounts center", "dismiss", "meta logo",
    "create new account", "log out",
)

# An Instagram handle: letters, digits, dot, underscore, 1-30 chars.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

IG_PACKAGE = "com.instagram.android"


def normalize_handle(handle: str | None) -> str:
    """Handles are compared case-insensitively and without a leading '@'.

    Airtable and the MLX remarks both write them as "@name"; the phone renders
    them bare, so every comparison has to go through here or a perfectly good
    match reads as a mismatch and the post is abandoned.
    """
    if not handle:
        return ""
    return str(handle).strip().lstrip("@").strip().lower()


def _is_account_label(label: str) -> bool:
    return normalize_handle(label) != "" and label.strip().lower() not in _NON_ACCOUNT_LABELS


def _handle_from_desc(desc: str) -> str:
    """"naughty_jasminn, 1 follow and 16 more" -> "naughty_jasminn"."""
    return (desc or "").split(",")[0].strip()


def parse_switcher_accounts(xml: str) -> list[str]:
    """Handles listed in an open account-switcher sheet, in on-screen order.

    Pure: takes a uiautomator hierarchy dump, returns handles. Rows are the
    clickable ``ViewGroup``s that carry a handle content-desc AND a child whose
    text is that same handle -- requiring both is what keeps stray system-UI
    nodes (the status bar's "Vodafone, signal full.", the navigation bar's
    "Back"/"Home") out of the list. Those are a different package anyway, which
    is checked first.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    handles: list[str] = []

    def child_texts(node) -> list[str]:
        out = []
        for child in node.iter():
            if child is node:
                continue
            text = (child.attrib.get("text") or "").strip()
            if text:
                out.append(text)
        return out

    for node in root.iter("node"):
        attrib = node.attrib
        if attrib.get("package") != IG_PACKAGE:
            continue
        if attrib.get("clickable") != "true":
            continue
        # Buttons in this sheet are the actions ("Add Instagram account",
        # "Go to Accounts Center", "Dismiss"), never an account.
        if attrib.get("class", "").endswith("Button"):
            continue

        handle = _handle_from_desc(attrib.get("content-desc", ""))
        if not handle or not _is_account_label(handle) or not _HANDLE_RE.match(handle):
            continue
        # The row must also *show* the handle, which an account row always does
        # and a decorative clickable container does not.
        if handle not in child_texts(node):
            continue
        if handle not in handles:
            handles.append(handle)

    return handles


def parse_active_handle(xml: str) -> str | None:
    """The handle in the profile header (i.e. the account currently posting)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        if node.attrib.get("resource-id") == ACTION_BAR_TITLE:
            text = (node.attrib.get("text") or "").strip()
            if text:
                return text
    return None


# --- device-facing helpers ------------------------------------------------
# Everything below needs a live uiautomator2 device; the parsing above does not.

def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message, *args)


def _first_existing(d, selectors):
    for selector in selectors:
        try:
            element = d(**selector)
            if element.exists:
                return element
        except Exception:
            continue
    return None


def read_active_handle(d, logger=None) -> str | None:
    """The handle Instagram is currently signed in as, or None if unreadable.

    Read from the profile header, so the caller must be on the profile tab.
    """
    element = _first_existing(d, ({"resourceId": ACTION_BAR_TITLE},))
    if element is None:
        return None
    try:
        text = (element.info.get("text") or "").strip()
    except Exception:
        return None
    return text or None


def open_profile_tab(d, logger=None) -> bool:
    element = _first_existing(d, PROFILE_TAB_SELECTORS)
    if element is None:
        _emit(logger, "warning", "account switch: could not find the profile tab")
        return False
    try:
        element.click()
    except Exception as exc:
        _emit(logger, "warning", "account switch: tapping the profile tab failed: %s", exc)
        return False
    return True


def open_switcher(d, logger=None) -> bool:
    """Tap the profile header handle to open the account picker."""
    element = _first_existing(d, SWITCHER_TRIGGER_SELECTORS)
    if element is None:
        _emit(logger, "warning", "account switch: no account-switcher trigger on screen")
        return False
    try:
        element.click()
    except Exception as exc:
        _emit(logger, "warning", "account switch: opening the switcher failed: %s", exc)
        return False
    return True


def list_accounts(d, logger=None) -> list[str]:
    """Every handle logged into this Instagram install.

    Leaves the sheet open -- :func:`switch_to` taps a row straight after, and
    the discovery pass dismisses it itself.
    """
    try:
        return parse_switcher_accounts(d.dump_hierarchy())
    except Exception as exc:
        _emit(logger, "warning", "account switch: could not read the switcher sheet: %s", exc)
        return []


def dismiss_switcher(d, logger=None) -> None:
    """Close the sheet without changing account."""
    try:
        element = d(description="Dismiss")
        if element.exists:
            element.click()
            return
    except Exception:
        pass
    try:
        d.press("back")
    except Exception as exc:
        _emit(logger, "warning", "account switch: could not dismiss the switcher: %s", exc)


def _tap_account_row(d, handle: str, logger=None) -> bool:
    """Tap the sheet row for `handle`.

    Matched on the row's content-desc, which is either the bare handle (the
    active account) or "handle, 1 follow and 16 more". A `descriptionStartsWith`
    on the bare handle covers both, but it would also match a longer handle that
    merely starts the same way ("nikki" vs "nikki_2"), so the exact form is
    tried first.
    """
    candidates = (
        {"description": handle},
        {"descriptionStartsWith": f"{handle},"},
        {"text": handle},
    )
    element = _first_existing(d, candidates)
    if element is None:
        _emit(logger, "warning", "account switch: %r is not in the switcher sheet", handle)
        return False
    try:
        element.click()
        return True
    except Exception as exc:
        _emit(logger, "warning", "account switch: tapping %r failed: %s", handle, exc)
        return False


def ensure_account(d, wanted: str | None, settle=None, logger=None) -> bool:
    """Make `wanted` the active Instagram account. True once it provably is.

    No-ops (True) when `wanted` is empty -- a single-account profile has nothing
    to choose, and the caller should not have to special-case that.

    The verification at the end is the point of the whole function: a post that
    goes out as the wrong handle is worse than a post that doesn't go out, so
    "we tapped the row" is never treated as success. If the header can't be
    re-read, we report failure and let the caller abandon the post.

    `settle(seconds, what)` is the flow's wait helper; a plain sleep is used
    when it isn't supplied so this module stays importable on its own.
    """
    if not normalize_handle(wanted):
        return True

    wanted_key = normalize_handle(wanted)

    def wait(seconds: float, what: str) -> None:
        if callable(settle):
            settle(seconds, what)
        else:  # pragma: no cover - only when used outside the flow
            import time
            time.sleep(seconds)

    if not open_profile_tab(d, logger=logger):
        return False
    wait(3, "profile header")

    active = read_active_handle(d, logger=logger)
    if active and normalize_handle(active) == wanted_key:
        _emit(logger, "info", "account switch: already signed in as %r", active)
        return True
    _emit(logger, "info", "account switch: active=%r, want %r -- switching", active, wanted)

    if not open_switcher(d, logger=logger):
        return False
    wait(3, "account switcher")

    available = list_accounts(d, logger=logger)
    _emit(logger, "info", "account switch: switcher lists %s", available)
    match = next((h for h in available if normalize_handle(h) == wanted_key), None)
    if match is None:
        _emit(logger, "warning",
              "account switch: %r is not logged into this phone (has %s) -- refusing to "
              "post as anyone else", wanted, available or "no readable accounts")
        dismiss_switcher(d, logger=logger)
        return False

    if not _tap_account_row(d, match, logger=logger):
        dismiss_switcher(d, logger=logger)
        return False

    # Switching re-loads the whole app shell; it is much slower than a tab change.
    wait(10, "account switch to settle")

    for _ in range(3):
        now = read_active_handle(d, logger=logger)
        if now and normalize_handle(now) == wanted_key:
            _emit(logger, "info", "account switch: now signed in as %r", now)
            return True
        wait(4, "profile header after switch")

    _emit(logger, "warning",
          "account switch: tapped %r but the header still reads %r -- abandoning rather "
          "than posting as the wrong account", wanted, read_active_handle(d, logger=logger))
    return False


def discover_accounts(d, settle=None, logger=None) -> dict:
    """Read which accounts this phone has, without changing anything.

    Returns ``{"active": handle|None, "accounts": [handle, ...],
    "switcher_read": bool}``. Used by the discovery pass that fills Airtable in,
    so the handles come from the phone rather than from a hand-typed MultiLogin
    remark (which we found to be stale on the first profile we checked).

    `switcher_read` is the one field the caller must not ignore. "The switcher
    listed one account" and "the switcher never opened" produce the same empty
    second handle, and treating the second as the first would record a
    two-account phone as single-account over one flaky tap -- quietly halving
    its posting for good. Only a sheet we actually read is evidence.
    """
    def wait(seconds: float, what: str) -> None:
        if callable(settle):
            settle(seconds, what)
        else:  # pragma: no cover
            import time
            time.sleep(seconds)

    result = {"active": None, "accounts": [], "switcher_read": False}
    if not open_profile_tab(d, logger=logger):
        return result
    wait(3, "profile header")
    result["active"] = read_active_handle(d, logger=logger)

    if not open_switcher(d, logger=logger):
        return result
    wait(3, "account switcher")
    listed = list_accounts(d, logger=logger)
    dismiss_switcher(d, logger=logger)
    if not listed:
        # The sheet opened but parsed to nothing -- that is a parse we do not
        # trust, not proof of a one-account phone.
        return result
    result["accounts"] = listed
    result["switcher_read"] = True
    return result
