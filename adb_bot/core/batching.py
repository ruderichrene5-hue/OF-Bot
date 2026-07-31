"""Keep the number of phones running at once to something the box can take.

Both runners used to launch *every* due profile up front and then hand the whole
list to a thread pool sized to match. With ~80 due accounts that meant ~80
MultiLogin profiles booted simultaneously and ~80 worker threads -- far more than
a single server can drive, and the fastest way to make every flow flaky at once.

Work is therefore processed in batches: launch a batch, run it, move on. The cap
applies to launches *and* to worker threads, so at no point are more than
`MAX_CONCURRENT_PROFILES` phones live.
"""

from __future__ import annotations

# How many MultiLogin profiles may be running at the same moment.
MAX_CONCURRENT_PROFILES = 10


def chunked(items, size: int) -> list:
    """Split `items` into consecutive lists of at most `size` (>=1)."""
    seq = list(items)
    step = max(1, int(size))
    return [seq[i:i + step] for i in range(0, len(seq), step)]


def resolve_concurrency(requested=None) -> int:
    """The effective profile-concurrency cap: a positive request, else the
    default. Never returns less than 1."""
    try:
        value = int(requested) if requested is not None else MAX_CONCURRENT_PROFILES
    except (TypeError, ValueError):
        value = MAX_CONCURRENT_PROFILES
    return max(1, value)
