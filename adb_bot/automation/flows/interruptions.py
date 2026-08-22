"""Handle the interruptions Instagram throws in the middle of a flow.

The flows call :func:`check_and_handle` whenever they expected a screen and
didn't get it. This module classifies what is actually on screen and either
recovers from it or tells the caller to stop.

Handled cases:

1. **A stuck / blank "Edit profile" page** (title shown, spinner, no fields).
   Recovery: press Back, confirm the "Edit profile" button is on the profile
   screen again, and tap it to continue.

2. **"Confirm you're human"** verification. A bot cannot solve this, so the
   caller is told to close the profile and report ``human verification
   requested``.

3. **Permission prompts** -- Instagram's "To use Location services..." screen
   (button *Continue*), then Android's own dialog (*WHILE USING THE APP*), and
   often a follow-up photos/files permission. These are granted in sequence.
   Granting can bounce the app back to the main feed, so the caller is told to
   restart its navigation rather than assume it is still where it was.

4. **New-device onboarding / Meta ads-consent chain** -- a fresh profile is
   walked through a series of full-screen blockers before it ever reaches the
   feed: "Choose if we process your data for ads" (*Get started*), the
   subscribe-vs-free-with-ads choice (select *Use free of charge with ads*,
   then *Continue*), the cookies consent (*Agree*), "You can manage your ad
   experience" (*OK*), and "Allow access to contacts" (*Allow*) followed by the
   Android contacts permission dialog (*ALLOW*). :func:`handle_blocking_prompts`
   detects each via the UI dump, taps the advancing button, then re-checks and
   moves to the next screen until the chain is cleared.

Detection reads the UI dump when it is available and falls back to OCR text,
because several Instagram screens never reach an "idle" state and cannot be
dumped. Buttons are only ever tapped from a UI dump using an EXACT label match
-- never from OCR -- so "Allow" can never be confused with "Don't allow".
"""

from __future__ import annotations

import time

from adb_bot.core.adb_commands import back
from adb_bot.automation import ban_detection

# --- What was found on screen -------------------------------------------------
INTERRUPTION_NONE = "none"
INTERRUPTION_STUCK_EDIT_PROFILE = "stuck_edit_profile"
INTERRUPTION_HUMAN_VERIFICATION = "human_verification"
INTERRUPTION_BANNED = "banned"
INTERRUPTION_ACTION_BLOCK = "action_block"
INTERRUPTION_PERMISSION = "permission"
INTERRUPTION_ONBOARDING = "onboarding"

# --- What the caller should do next ------------------------------------------
OUTCOME_NONE = "none"                                # nothing to handle
OUTCOME_HANDLED = "handled"                          # recovered; retry the step
OUTCOME_RESTART = "restart"                          # recovered, but we may be back at the feed
OUTCOME_HUMAN_VERIFICATION = "human_verification"    # stop; close the profile (human must solve)
OUTCOME_ACCOUNT_BANNED = "banned"                    # stop; account disabled/suspended
OUTCOME_ACTION_BLOCK = "action_block"                # stop; temporary IG throttle
OUTCOME_UNRESOLVED = "unresolved"                    # could not recover

# Outcomes that mean "IG flagged the account" -- the caller stops and reports the
# kind. The value equals the ban_detection kind, so it doubles as the account_flag.
ACCOUNT_FLAG_OUTCOMES = (OUTCOME_HUMAN_VERIFICATION, OUTCOME_ACCOUNT_BANNED, OUTCOME_ACTION_BLOCK)


def account_flag_for(outcome: str | None) -> str | None:
    """Return the account_flag kind for an outcome, or None if it isn't a flag."""
    return outcome if outcome in ACCOUNT_FLAG_OUTCOMES else None

_HUMAN_VERIFICATION_MARKERS = (
    "confirm you're human",
    "confirm youre human",
    "confirm you re human",
    "to use your account",
    "takes about 30 seconds",
    "we detected unusual activity",
)

_PERMISSION_MARKERS = (
    "allow instagram to access",
    # Android words the notifications prompt "send you", not "access" -- so
    # none of the markers below matched it and the dialog was invisible to this
    # handler. It sat in front of a password-reset flow through eight rounds of
    # "answering an android permission dialog" that answered nothing, because
    # detection had already failed before any button was looked for.
    "allow instagram to send you",
    "to send you notifications",
    "to use location services",
    "location services",
    "while using the app",
    "only this time",
    # Both apostrophes. Android renders U+2019 here, and a marker typed with
    # the ASCII one never matches it -- the same mismatch that stalled a
    # created account on this exact dialog earlier today.
    "don't allow",
    "don’t allow",
    "dont allow",
    "access photos",
    "photos and videos",
    "access files",
    "media and files",
    "access your contacts",
    "set up on new device",
)

# Buttons that GRANT / advance a permission dialog, in the order we prefer them.
# Matched EXACTLY, so "allow" can never match "Don't allow".
_GRANT_BUTTON_LABELS = (
    "while using the app",
    "allow all",
    "allow",
    "continue",
    "ok",
)

# Meta / Instagram "Set up on new device" onboarding + ads-consent chain shown
# to a fresh profile before the feed. Any of these on screen means we're on a
# blocker; each is advanced by a single primary button (the ads-subscription
# screen additionally needs an option selected first -- see
# :func:`_advance_onboarding_screen`).
_ONBOARDING_MARKERS = (
    "set up on new device",
    "process your data for ads",
    "process your personal data",
    "choose if we process",
    "subscribe or continue using our products",
    "free of charge with ads",
    "cookies on our products",
    "consent to meta processing",
    "manage your ad experience",
    "personalised ads",
    "personalized ads",
    "allow access to contacts",
    "find people to follow",
    "sync your contacts",
    "syncing your contacts",
)

# Buttons that ADVANCE the onboarding/consent chain, in preferred order. Matched
# EXACTLY so "continue" never matches "Continue with personalised ads" and
# "allow" never matches "Don't allow". "get started"/"agree"/"ok" come before
# "continue" so a dialog's own button wins over a greyed "Continue" on the base
# screen behind it.
_ONBOARDING_ADVANCE_LABELS = (
    "get started",
    "agree",
    "ok",
    "continue",
    "allow",
    "skip",
    "done",
    "next",
)

# Fields that prove the Edit profile form actually loaded.
_EDIT_PROFILE_FIELD_MARKERS = ("username", "pronouns", "bio")


def _ig():
    # Imported lazily so this module and instagram.py can reference each other.
    from adb_bot.automation.flows import instagram as ig
    return ig


def _snippet(text, limit: int = 200) -> str:
    """A compact, single-line slice of screen text for troubleshooting logs."""
    collapsed = " ".join((text or "").split())
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _dump_text(root) -> str:
    """All visible text/content-desc in a UI dump, lowercased."""
    if root is None:
        return ""
    parts = []
    for node in root.iter():
        attrs = node.attrib
        for key in ("text", "content-desc"):
            value = str(attrs.get(key, "") or "").strip()
            if value:
                parts.append(value)
    return " ".join(parts).lower()


def _screen_text(target, logger=None, flow=None, root=None) -> tuple[str, object]:
    """Return (lowercased screen text, ui_dump_root). Falls back to OCR when the
    screen can't be dumped."""
    ig = _ig()
    if root is None:
        root = ig._adb_capture_ui_dump(target, logger=logger)
    text = _dump_text(root)
    if not text and flow is not None and hasattr(flow, "_ocr_screen_text"):
        text = flow._ocr_screen_text(target, logger=logger) or ""
    return text, root


def detect_interruption(target, logger=None, flow=None, expect=None) -> tuple[str, object]:
    """Classify the current screen. `expect` is what the caller wanted to see
    (currently only ``"edit_profile"``), used to recognise a stuck page."""
    ig = _ig()
    text, root = _screen_text(target, logger=logger, flow=flow)

    # Account-flag screens (ban / verification / action-block) via the shared
    # classifier, plus the extra IG-specific verification phrases kept locally.
    kind = ban_detection.classify_block_text(text)
    if kind is None and any(marker in text for marker in _HUMAN_VERIFICATION_MARKERS):
        kind = ban_detection.KIND_HUMAN_VERIFICATION
    if kind == ban_detection.KIND_BANNED:
        ig._emit(logger, "warning", "Account appears banned/suspended for %s", target)
        return INTERRUPTION_BANNED, root
    if kind == ban_detection.KIND_HUMAN_VERIFICATION:
        ig._emit(logger, "warning", "Human verification requested for %s", target)
        return INTERRUPTION_HUMAN_VERIFICATION, root
    if kind == ban_detection.KIND_ACTION_BLOCK:
        ig._emit(logger, "warning", "Action block detected for %s", target)
        return INTERRUPTION_ACTION_BLOCK, root

    if any(marker in text for marker in _ONBOARDING_MARKERS):
        ig._emit(logger, "info", "New-device onboarding/consent screen detected for %s", target)
        return INTERRUPTION_ONBOARDING, root

    if any(marker in text for marker in _PERMISSION_MARKERS):
        ig._emit(logger, "info", "Permission prompt detected for %s", target)
        return INTERRUPTION_PERMISSION, root

    if expect == "edit_profile":
        # The caller reached Edit profile but its fields never appeared (blank
        # page / spinner), or the screen could not be read at all.
        if not any(marker in text for marker in _EDIT_PROFILE_FIELD_MARKERS):
            ig._emit(logger, "info", "Edit profile page looks stuck/blank for %s", target)
            return INTERRUPTION_STUCK_EDIT_PROFILE, root

    return INTERRUPTION_NONE, root


def _advance_permission_screen(target, adb_client, root, logger=None) -> bool:
    """Tap the grant/continue button on a permission dialog via an EXACT label
    match from the UI dump. Returns True if a button was tapped.

    The exact match guarantees we hit "Allow" / "While using the app" and never
    "Don't allow". Callers must pass a non-None `root`.
    """
    ig = _ig()
    for label in _GRANT_BUTTON_LABELS:
        center = ig._find_center_by_exact_label(root, (label,))
        if center is not None:
            ig._emit(logger, "info", "Fallback (permission): clicking grant button '%s' at %s for %s", label, center, target)
            ig._adb_tap(target, center[0], center[1], adb_client, logger=logger, description=f"Clicking '{label}'")
            return True
    ig._emit(logger, "warning", "Fallback (permission): no grant/continue button found for %s", target)
    return False


def _advance_onboarding_screen(target, adb_client, root, text, logger=None) -> bool:
    """Advance one screen of the new-device onboarding / ads-consent chain by
    tapping its primary button (EXACT label match from the UI dump). Returns
    True if it tapped something. Callers must pass a non-None `root`.
    """
    ig = _ig()

    # The ads subscription choice keeps its "Continue" button disabled until an
    # option is picked, so select "Use free of charge with ads" first, then
    # Continue (re-dumping so we tap the now-enabled button).
    if "free of charge with ads" in text:
        radio = ig._find_center_by_exact_label(root, ("use free of charge with ads",))
        if radio is not None:
            ig._emit(logger, "info", "Fallback (onboarding): ads-consent choice for %s; selecting 'Use free of charge with ads' at %s", target, radio)
            ig._adb_tap(target, radio[0], radio[1], adb_client, logger=logger, description="Selecting 'Use free of charge with ads'")
            time.sleep(1.5)
            root = ig._adb_capture_ui_dump(target, logger=logger, idle_retries=2) or root
            cont = ig._find_center_by_exact_label(root, ("continue",))
            if cont is not None:
                ig._emit(logger, "info", "Fallback (onboarding): clicking 'Continue' at %s for %s", cont, target)
                ig._adb_tap(target, cont[0], cont[1], adb_client, logger=logger, description="Clicking 'Continue'")
            else:
                ig._emit(logger, "info", "Fallback (onboarding): 'Continue' not enabled yet for %s; will click it on the next round", target)
            # Even if Continue isn't found yet, the option is selected; the next
            # round will find and tap it.
            return True

    for label in _ONBOARDING_ADVANCE_LABELS:
        center = ig._find_center_by_exact_label(root, (label,))
        if center is not None:
            ig._emit(logger, "info", "Fallback (onboarding): clicking '%s' at %s for %s", label, center, target)
            ig._adb_tap(target, center[0], center[1], adb_client, logger=logger, description=f"Clicking '{label}'")
            return True

    ig._emit(logger, "warning", "Fallback (onboarding): no known advance button on the screen for %s", target)
    return False


def _matched_markers(text) -> frozenset:
    """The set of onboarding/permission markers present in `text`. Used to tell
    whether a tap actually moved us to a new screen (the marker set changes) or
    left us stuck on the same one (it doesn't)."""
    return frozenset(m for m in (_ONBOARDING_MARKERS + _PERMISSION_MARKERS) if m in text)


def handle_permission_prompts(target, adb_client, logger=None, flow=None, max_rounds: int = 6) -> bool:
    """Grant Instagram's permission prompts in sequence (location, then
    photos/files). Returns True if at least one prompt was dismissed."""
    ig = _ig()
    handled_any = False

    for _ in range(max_rounds):
        text, root = _screen_text(target, logger=logger, flow=flow)
        if not any(marker in text for marker in _PERMISSION_MARKERS):
            break

        # Only tap from a UI dump with an exact label match. Permission dialogs
        # are static system/app screens, so the dump works here -- and an exact
        # match guarantees we never hit "Don't allow".
        if root is None:
            ig._emit(logger, "warning", "Permission prompt for %s could not be read; leaving it alone", target)
            break

        if not _advance_permission_screen(target, adb_client, root, logger=logger):
            break
        handled_any = True
        time.sleep(2.5)

    return handled_any


def handle_blocking_prompts(target, adb_client, logger=None, flow=None, max_rounds: int = 14) -> bool:
    """Clear the chain of new-device onboarding / ads-consent screens AND the
    runtime permission dialogs a fresh profile hits before the feed.

    Each round: read the current screen from the UI dump (OCR only tells us
    something is there -- we never tap from OCR), tap the advancing button for
    whichever screen is up (permission dialogs first, since they overlay the
    app), then loop and re-check. Stops when nothing known is on screen, when a
    screen stops advancing (the marker set repeats), or after ``max_rounds``.
    Returns True if it advanced at least one screen.
    """
    ig = _ig()
    handled_any = False
    last_markers = None
    stuck = 0

    for round_index in range(1, max_rounds + 1):
        root = ig._adb_capture_ui_dump(target, logger=logger, idle_retries=2)
        text = _dump_text(root)
        source = "ui-dump"
        if not text and flow is not None and hasattr(flow, "_ocr_screen_text"):
            text = flow._ocr_screen_text(target, logger=logger) or ""
            source = "ocr"

        is_permission = any(marker in text for marker in _PERMISSION_MARKERS)
        is_onboarding = any(marker in text for marker in _ONBOARDING_MARKERS)
        if not is_permission and not is_onboarding:
            if round_index == 1:
                ig._emit(logger, "info", "Fallback (blocking prompts): none on screen for %s; nothing to clear", target)
            else:
                ig._emit(logger, "info", "Fallback (blocking prompts): chain cleared for %s after %s screen(s)", target, round_index - 1)
            break

        markers = sorted(_matched_markers(text))
        kind = "permission+onboarding" if (is_permission and is_onboarding) else ("permission" if is_permission else "onboarding")
        ig._emit(
            logger, "info",
            "Fallback (blocking prompts) round %s for %s: SEES a %s screen via %s; markers=%s; text=%r",
            round_index, target, kind, source, markers, _snippet(text),
        )

        if root is None:
            ig._emit(
                logger, "warning",
                "Fallback (blocking prompts): screen for %s is only readable via OCR (no UI dump), so there is no safe button to click; leaving it. OCR text=%r",
                target, _snippet(text),
            )
            break

        marker_set = frozenset(markers)
        stuck = stuck + 1 if marker_set == last_markers else 0
        last_markers = marker_set
        if stuck >= 2:
            ig._emit(logger, "warning", "Fallback (blocking prompts): screen for %s did not change after clicking (still markers=%s); giving up", target, markers)
            break

        # Try the onboarding screen's specific button first (Get started / Agree
        # / OK win over a generic "Continue" that may belong to the base screen
        # behind a modal). Fall back to the permission-grant buttons -- this also
        # covers the Android runtime dialog (While using the app / Allow), whose
        # buttons the onboarding list doesn't match, so onboarding-advance
        # returns False and permission-advance takes over.
        advanced = False
        if is_onboarding:
            advanced = _advance_onboarding_screen(target, adb_client, root, text, logger=logger)
        if not advanced and is_permission:
            advanced = _advance_permission_screen(target, adb_client, root, logger=logger)
        if not advanced:
            ig._emit(logger, "warning", "Fallback (blocking prompts): recognised the screen for %s (markers=%s) but found no known button to click; leaving it", target, markers)
            break

        handled_any = True
        time.sleep(2.5)

    return handled_any


def handle_stuck_edit_profile(target, adb_client, logger=None, flow=None) -> bool:
    """Roll back from a stuck Edit profile page: press Back, then find and tap
    the "Edit profile" button on the profile screen again."""
    ig = _ig()
    ig._emit(logger, "info", "Rolling back from the stuck Edit profile page for %s", target)
    adb_client.run_command(f"adb -s {target} shell {back()}")
    time.sleep(3)

    # The profile screen animates, so prefer OCR to locate the button.
    center = None
    if flow is not None and hasattr(flow, "_ocr_find_text_center"):
        center = flow._ocr_find_text_center(target, ("edit profile", "edit"), logger=logger)
    if center is None:
        root = ig._adb_capture_ui_dump(target, logger=logger)
        if root is not None:
            center = ig._find_center_by_exact_label(root, ("edit profile",))

    if center is None:
        ig._emit(logger, "warning", "'Edit profile' button not found after rolling back for %s", target)
        return False

    ig._emit(logger, "info", "Re-opening Edit profile for %s at %s", target, center)
    ig._adb_tap(target, center[0], center[1], adb_client, logger=logger, description="Re-opening Edit profile")
    time.sleep(3)
    return True


def check_and_handle(target, adb_client, logger=None, flow=None, expect=None) -> str:
    """Detect an interruption on the current screen and deal with it.

    Returns one of ``OUTCOME_NONE``, ``OUTCOME_HANDLED`` (retry your step),
    ``OUTCOME_RESTART`` (navigation may have reset -- start over from the feed),
    ``OUTCOME_HUMAN_VERIFICATION`` (stop and close the profile), or
    ``OUTCOME_UNRESOLVED``.
    """
    kind, _root = detect_interruption(target, logger=logger, flow=flow, expect=expect)

    if kind == INTERRUPTION_BANNED:
        return OUTCOME_ACCOUNT_BANNED
    if kind == INTERRUPTION_HUMAN_VERIFICATION:
        return OUTCOME_HUMAN_VERIFICATION
    if kind == INTERRUPTION_ACTION_BLOCK:
        return OUTCOME_ACTION_BLOCK

    if kind in (INTERRUPTION_PERMISSION, INTERRUPTION_ONBOARDING):
        # Walk the whole chain of onboarding/consent screens and permission
        # dialogs, not just the one currently on top. Clearing them usually
        # drops the app on the main feed, so the caller must re-navigate rather
        # than assume its position.
        if handle_blocking_prompts(target, adb_client, logger=logger, flow=flow):
            return OUTCOME_RESTART
        return OUTCOME_UNRESOLVED

    if kind == INTERRUPTION_STUCK_EDIT_PROFILE:
        if handle_stuck_edit_profile(target, adb_client, logger=logger, flow=flow):
            return OUTCOME_HANDLED
        return OUTCOME_UNRESOLVED

    return OUTCOME_NONE
