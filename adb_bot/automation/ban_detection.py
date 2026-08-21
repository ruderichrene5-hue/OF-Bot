"""Classify the block screens Instagram shows when it flags an account, and map
each kind to the Airtable fields the bot writes back (checklist section 5:
Ban / Verification Detection).

This is the shared "brain" used by every detection point -- the u2 flows and the
dump/OCR interruptions handler -- so ban vs. verification vs. action-block is
decided in exactly one place. It's pure text-in / label-out and unit-tested; the
Airtable writes live in `incidents.py`.

Four kinds, deliberately distinct because they need different reactions:

- **banned** -- the account is disabled/suspended. Permanent as far as the bot is
  concerned: set Lifecycle Stage = Banned so every loop skips it.
- **human_verification** -- a checkpoint/challenge only a human (Rene) can solve.
  Set Needs Human Verification so the loops skip it until he clears it.
- **action_block** -- a temporary "try again later" throttle. NOT a ban: we log
  it and back off, but leave the account's stage alone so it resumes next run.
- **login_confirm** -- the "Was this you?" new-login notice. The odd one out: it
  is not an incident at all, has no Airtable mapping, and is not in `ALL_KINDS`.
  It exists only so the flows can tell this screen apart from the checkpoint it
  sounds like, tap "This Was Me" and carry on. See `looks_like_login_confirm`.

Precedence when a screen matches more than one set: banned > login_confirm >
human_verification > action_block (the most severe wins, except that
login_confirm has to outrank the checkpoint whose words it borrows).
"""

from __future__ import annotations

from dataclasses import dataclass

from adb_bot.clients import airtable as at

# --- incident kinds -----------------------------------------------------------
KIND_BANNED = "banned"
KIND_HUMAN_VERIFICATION = "human_verification"
KIND_ACTION_BLOCK = "action_block"
KIND_LOGIN_CONFIRM = "login_confirm"

# The three kinds that mean "flag the account and stop". Callers test membership
# of this tuple to decide whether an outcome is a flag (`instagram.py` does it
# twice), so `login_confirm` is deliberately NOT in it: that screen is cleared by
# tapping one button, and nothing about it should reach Airtable.
ALL_KINDS = (KIND_BANNED, KIND_HUMAN_VERIFICATION, KIND_ACTION_BLOCK)

# --- screen markers (lowercased substrings) -----------------------------------
# Account disabled / suspended / permanently removed.
_BANNED_MARKERS = (
    "your account has been suspended",
    "account has been suspended",
    "we suspended your account",
    "your account has been disabled",
    "account has been disabled",
    "we disabled your account",
    "your account was disabled",
    "your account has been permanently disabled",
    "your account has been deleted",
    "we removed your account",
    "account has been removed",
)

# --- "Was this you?" login confirmation ---------------------------------------
# Instagram's new-login notice: "We Detected An Unusual Login", "Someone tried to
# log in to your account", "Was this you?" -- over a pair of buttons, one of
# which just says yes. It blocks the app exactly like a checkpoint does, but it
# is not one: nothing has to be solved, proved or received, so the bot answers it
# itself instead of parking the profile for a person.
#
# It has to be classified BEFORE `human_verification`, because its own wording
# ("we detected", "suspicious login attempt") matches that list -- which is how
# every one of these screens has been reported as "Human Verification Required"
# up to now, spending a profile's whole posting schedule on a button nobody
# pressed.
#
# The affirmative buttons. Also the detection signal: this screen is defined by
# having a "yes, that was me" control, not by prose Instagram rewords between
# builds. Both apostrophes appear in the wild (ASCII and typographic), so both
# are listed.
#
# EVERY tap of these is an EXACT, whole-label match -- never a substring. The
# refusing button ("This Wasn't Me" / "Secure Account") is the one control on the
# fleet that must never be pressed by accident: it starts a password reset and
# locks the account out of the bot for good. Note that even as substrings these
# are safe -- "this wasn't me" does not contain "this was me" -- but the exact
# match is what the guarantee rests on.
LOGIN_CONFIRM_BUTTON_LABELS = (
    "this was me",
    "that was me",
    "it was me",
    "yes, this was me",
    "yes, that was me",
    "yes, it was me",
    "yes it was me",
    "yes, it's me",
    "yes, it\u2019s me",
    "yes, this is me",
    "this is me",
    "it's me",
    "it\u2019s me",
    "yes, that's me",
    "yes, that\u2019s me",
)

# What the screen is about. Required IN ADDITION to a button label, so a stray
# message bubble reading "it was me" can never be mistaken for the screen and
# get the account flagged.
_LOGIN_CONFIRM_CONTEXT_MARKERS = (
    "was this you",
    "is this you",
    "unusual login",
    "suspicious login",
    "login attempt",
    "tried to log in",
    "tried to login",
    "new login",
    "we noticed a login",
    "logged in from",
    "logging in from",
    "log in from",
    "device you don't usually use",
    "device you dont usually use",
    "device you don\u2019t usually use",
    "we detected",
    "unrecognised device",
    "unrecognized device",
    "recognise this",
    "recognize this",
    "sign-in attempt",
    "signed in from",
    "secure your account",
    "was it you",
)


def looks_like_login_confirm(text: str | None) -> bool:
    """True for the "Was this you?" new-login notice -- a screen the bot may
    answer itself by tapping the affirmative button.

    Deliberately an AND: an affirmative button label AND some login context. A
    button alone is not enough (a message could read "it was me"), and context
    alone is not enough (the checkpoint screens describe unusual activity too,
    and those a person must solve). Failing this test costs nothing -- the screen
    then classifies as `human_verification`, which is what happened to all of
    them before this existed.
    """
    if not text:
        return False
    haystack = text.lower()
    if not any(label in haystack for label in LOGIN_CONFIRM_BUTTON_LABELS):
        return False
    return any(marker in haystack for marker in _LOGIN_CONFIRM_CONTEXT_MARKERS)


# "Confirm you're human" / suspicious-activity checkpoint a human must solve.
_HUMAN_VERIFICATION_MARKERS = (
    "confirm you're human",
    "confirm youre human",
    "confirm you re human",
    "confirm you are human",
    "help us confirm",
    "we detected unusual activity",
    "we detected",
    "suspicious activity",
    "verify it's you",
    "verify its you",
    "we suspect",
    "confirm your identity",
    "enter the code we sent",
    "we need more information to confirm",
    # Instagram's SMS checkpoint (ChallengeActivity). Read off `Kathi 7` on
    # 2026-08-14: "Enter the 6-digit confirmation code we sent via SMS to
    # +31...". The older "enter the code we sent" marker does not match it --
    # "6-digit confirmation" sits in the middle -- so the checkpoint read as an
    # ordinary flow failure, was retried five times and produced a false
    # `Retries Exhausted` on an account that simply needs a code typed in.
    "enter confirmation code",
    "confirmation code we sent",
    # The two controls that screen offers. Kept because the body text is the
    # part Instagram rewords between builds, while these buttons have not.
    "update mobile number",
    "request new code",
)

# Temporary "action blocked / try again later" throttle.
_ACTION_BLOCK_MARKERS = (
    "action blocked",
    "action is blocked",
    "try again later",
    "we restrict certain activity",
    "temporarily blocked",
    "you're temporarily blocked",
    "youre temporarily blocked",
    "we limit how often",
    "this action was blocked",
    "you can't use this feature right now",
    "you cant use this feature right now",
)

# Precedence order: most severe first. `login_confirm` sits second because it is
# the one kind that is *less* severe than what its own words suggest -- it has to
# be taken out of the human-verification list's way, and only a banned account
# outranks it (a disabled account can still be showing an old login notice).
_ORDERED = (
    (KIND_BANNED, _BANNED_MARKERS),
    (KIND_HUMAN_VERIFICATION, _HUMAN_VERIFICATION_MARKERS),
    (KIND_ACTION_BLOCK, _ACTION_BLOCK_MARKERS),
)


def classify_block_text(text: str | None) -> str | None:
    """Return the incident kind for on-screen `text`, or None if it's not a
    known block screen. `text` should already be lowercased screen text (UI dump
    or OCR); we lowercase again defensively."""
    if not text:
        return None
    haystack = text.lower()
    # Ahead of the loop, and after nothing: a banned account is checked first
    # because its screen is final, then the login notice, because its wording
    # would otherwise be swallowed by the human-verification markers below.
    if any(marker in haystack for marker in _BANNED_MARKERS):
        return KIND_BANNED
    if looks_like_login_confirm(haystack):
        return KIND_LOGIN_CONFIRM
    for kind, markers in _ORDERED:
        if any(marker in haystack for marker in markers):
            return kind
    return None


# --- kind -> Airtable mapping -------------------------------------------------
@dataclass(frozen=True)
class IncidentMapping:
    """How one incident kind is written back to Airtable."""

    kind: str
    label: str                       # human-readable, used in notes/logs
    lifecycle_stage: str | None      # Accounts.Lifecycle Stage to set, or None
    needs_verification: bool         # tick Accounts.Needs Human Verification?
    event_type: str                  # Ban & Flag History.Event Type
    issue_type: str                  # Posting Queue.Issue Type (posting loop)


INCIDENTS: dict[str, IncidentMapping] = {
    KIND_BANNED: IncidentMapping(
        KIND_BANNED, "banned",
        lifecycle_stage=at.STAGE_BANNED, needs_verification=False,
        event_type=at.EVENT_FULL_BAN, issue_type=at.ISSUE_BANNED_BLOCKED,
    ),
    KIND_HUMAN_VERIFICATION: IncidentMapping(
        KIND_HUMAN_VERIFICATION, "human verification required",
        lifecycle_stage=None, needs_verification=True,
        event_type=at.EVENT_WARNING, issue_type=at.ISSUE_HUMAN_VERIFICATION,
    ),
    KIND_ACTION_BLOCK: IncidentMapping(
        KIND_ACTION_BLOCK, "action block",
        lifecycle_stage=None, needs_verification=False,
        event_type=at.EVENT_ACTION_BLOCK, issue_type=at.ISSUE_BANNED_BLOCKED,
    ),
}


def mapping_for(kind: str | None) -> IncidentMapping | None:
    return INCIDENTS.get(kind) if kind else None
