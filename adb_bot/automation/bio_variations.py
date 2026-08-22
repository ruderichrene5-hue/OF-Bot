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
