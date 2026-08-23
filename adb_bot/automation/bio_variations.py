"""Bio text with a randomized call-to-action, so profiles set in the same
batch do not all carry the identical bio -- an easy tell that a run of
accounts is bulk-managed, and each account's Website link is the real
click target anyway, not the bio text itself.
"""

from __future__ import annotations

import random

DEFAULT_CTAS = (
    "Check below ⬇️",
    "Look down here \U0001f447",
    "Click the link below ⬇️",
    "Link is below \U0001f447",
    "See the link below ⬇️",
)


def random_cta(pool=DEFAULT_CTAS, rand: random.Random | None = None) -> str:
    """One call-to-action phrase, picked at random from `pool`."""
    chooser = rand or random
    return chooser.choice(pool)


def build_bio(base: str = "", pool=DEFAULT_CTAS,
             rand: random.Random | None = None) -> str:
    """`base` with a random CTA appended, or just the CTA if there is no base.

    `base` is whatever fixed text a profile always carries (a name, an age,
    an emoji line) -- this only varies the part that would otherwise be
    identical across every profile in a batch.
    """
    cta = random_cta(pool, rand=rand)
    base = (base or "").strip()
    return f"{base} {cta}".strip() if base else cta


SEPARATORS = (".", "_")

# Same shape as `signup.next_username`'s tail: a short numeric range that
# reads as an ordinary handle suffix, not a sequential or repeated-digit
# string that looks machine-generated (2026-08-23).
_TAIL_MIN, _TAIL_MAX = 10, 9999

# How often the stem gets a doubled letter instead of the model's name
# exactly, e.g. "nikki" -> "nikkki" -- a visible variant, not a typo, so
# a batch of usernames for one model does not all start identically.
_DOUBLE_LETTER_CHANCE = 0.5


def _with_a_doubled_letter(stem: str, rand: random.Random) -> str:
    """`stem` with one of its own letters duplicated, e.g. "nikki" ->
    "nikkki". Never touches the first character, so the result always still
    starts with the model's actual name, letter for letter."""
    if len(stem) < 2:
        return stem
    i = rand.randrange(1, len(stem))
    return stem[:i] + stem[i] + stem[i:]


def build_username(model: str, pool=SEPARATORS,
                   rand: random.Random | None = None) -> str:
    """A username/nickname for `model`: her name (sometimes with a doubled
    letter), a separator, then a short digit tail.

    Returns "" for a blank `model` -- there is no name to build on.
    """
    stem = (model or "").strip()
    if not stem:
        return ""
    chooser = rand or random
    if chooser.random() < _DOUBLE_LETTER_CHANCE:
        stem = _with_a_doubled_letter(stem, chooser)
    separator = chooser.choice(pool)
    tail = chooser.randint(_TAIL_MIN, _TAIL_MAX)
    return f"{stem}{separator}{tail}"
