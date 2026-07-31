"""Classify the block screens Instagram shows when it flags an account, and map
each kind to the Airtable fields the bot writes back (checklist section 5:
Ban / Verification Detection).

This is the shared "brain" used by every detection point -- the u2 flows and the
dump/OCR interruptions handler -- so ban vs. verification vs. action-block is
decided in exactly one place. It's pure text-in / label-out and unit-tested; the
Airtable writes live in `incidents.py`.

Three kinds, deliberately distinct because they need different reactions:

- **banned** -- the account is disabled/suspended. Permanent as far as the bot is
  concerned: set Lifecycle Stage = Banned so every loop skips it.
- **human_verification** -- a checkpoint/challenge only a human (Rene) can solve.
  Set Needs Human Verification so the loops skip it until he clears it.
- **action_block** -- a temporary "try again later" throttle. NOT a ban: we log
  it and back off, but leave the account's stage alone so it resumes next run.

Precedence when a screen matches more than one set: banned > human_verification >
action_block (the most severe wins).
"""

from __future__ import annotations

from dataclasses import dataclass

from adb_bot.clients import airtable as at

# --- incident kinds -----------------------------------------------------------
KIND_BANNED = "banned"
KIND_HUMAN_VERIFICATION = "human_verification"
KIND_ACTION_BLOCK = "action_block"

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

# Precedence order: most severe first.
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
