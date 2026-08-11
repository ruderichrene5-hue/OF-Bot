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

import html
import re
from dataclasses import dataclass

from adb_bot.clients import airtable as at

# The only two attributes of a uiautomator node that carry words a *person*
# would read. Everything else in a hierarchy dump -- resource-id, class,
# package -- is developer naming that happens to be made of English.
_VISIBLE_ATTRS = re.compile(r'\b(?:text|content-desc)="([^"]*)"')

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
#
# Two markers were removed on 2026-08-11: a bare "we detected" and a bare
# "we suspect". Both were already covered by the longer phrases that remain
# ("we detected unusual activity"), so they added no real detection -- only
# surface area. "We detected a new login", "we detected an issue" and any OCR
# noise landing on those two words were enough to flag an account as needing a
# human, and a flagged profile stops posting entirely until somebody clears it
# by hand. A marker here has to be a phrase that only ever appears on a
# checkpoint; anything shorter is a guess with a very expensive false positive.
_HUMAN_VERIFICATION_MARKERS = (
    "confirm you're human",
    "confirm youre human",
    "confirm you re human",
    "confirm you are human",
    "help us confirm",
    "we detected unusual activity",
    "suspicious activity",
    "verify it's you",
    "verify its you",
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


def visible_text_from_dump(xml: str | None) -> str:
    """The words actually on screen, pulled out of a uiautomator hierarchy dump.

    Callers used to hand `classify_block_text` the raw XML, which meant every
    marker was being matched against resource-ids, class names and package names
    as well as against anything a user could read. Instagram ships thousands of
    ids and they are written in English, so that is a large surface for an
    accidental substring hit -- and the cost of one is not a retry, it is a
    profile parked as `Human Verification Required` until a person looks at it.
    On 2026-08-11, 17 of 29 flagged profiles carried that reason.

    Only `text` and `content-desc` survive here. A real checkpoint puts its
    words in exactly those two attributes -- that is what makes it readable --
    so nothing detectable is lost, while `id/we_detected_banner_stub` stops
    counting as a screen that says "we detected".

    Not XML-parsed on purpose: a dump can be truncated or malformed and this
    must still answer. A regex over attributes degrades to "fewer words", where
    a parser would raise and leave the caller with nothing.

    `html.unescape` rather than the XML one because the entity that matters here
    is the apostrophe. uiautomator writes it as `&apos;` / `&#39;`, neither of
    which the XML unescaper expands by default, and half the checkpoint phrases
    we look for contain one -- "confirm you're human" would never have matched a
    screen that was showing exactly that.
    """
    if not xml:
        return ""
    return html.unescape(" ".join(m.group(1) for m in _VISIBLE_ATTRS.finditer(xml) if m.group(1)))


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
