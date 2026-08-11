"""Give each queued post a caption, without giving them all the same one.

The Posting Queue has carried an empty Caption since the day it was created:
`queue_runner` deliberately left it unset ("inventing a caption rotation here
would put text on posts that nobody chose"), and the Airtable automation built
to fill it -- "Caption Rotation" -- was never deployed. So the Caption Pool's 500
lines have never reached a post. This module is the rotation that was missing.

Two properties matter, and only the first is obvious:

1. **Each account walks the pool in order**, so a caption is not repeated on an
   account until the other 499 have been used.

2. **Accounts start at different places in the pool.** This is the one that
   actually protects the fleet. A naive rotation starts everyone at CAP-001, and
   since all accounts post on the same slot grid, every post in the 09:00 slot
   would carry identical text -- roughly 270 accounts publishing the same
   sentence within minutes of each other, which is a far louder footprint than
   having no caption at all. The starting offset is therefore derived from a
   stable hash of the account key, spreading the fleet across the pool on day
   one and keeping it spread.

State is a small JSON file: key -> index of the last caption used. It is
advisory, not critical -- losing it re-staggers the fleet and costs nothing but
a few repeated captions.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

STATE_FILENAME = "caption_rotation.json"


def _stable_offset(key: str, size: int) -> int:
    """A per-account starting point in the pool.

    Deliberately hashlib and not `hash()`: Python randomises string hashing per
    process, so `hash()` would move every account's caption on every restart and
    the "walk the pool in order" property would be a lie.
    """
    if size <= 0:
        return 0
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % size


class CaptionRotation:
    """Hands out the next caption for an account, in order, from a stagger."""

    def __init__(self, path=None):
        self.path = Path(path) if path else (get_app_data_dir() / STATE_FILENAME)
        self._state = None

    # -- state -----------------------------------------------------------

    def load(self) -> dict:
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._state = {str(k): int(v) for k, v in (raw or {}).items()}
        except FileNotFoundError:
            self._state = {}
        except Exception:
            # Corrupt state re-staggers the fleet rather than stopping the queue.
            self._state = {}
        return self._state

    def save(self) -> bool:
        if self._state is None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self._state, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    # -- rotation --------------------------------------------------------

    def next_for(self, key: str, pool: list):
        """The next caption for `key`, advancing the rotation.

        `pool` is `AirtableClient.caption_pool()` output. Returns None when the
        pool is empty or the key is blank -- the caller then queues the row with
        no caption, which is exactly the behaviour that existed before.
        """
        if not key or not pool:
            return None
        state = self.load()
        size = len(pool)
        if key in state:
            index = (state[key] + 1) % size
        else:
            index = _stable_offset(key, size)
        state[key] = index
        return pool[index]

    def peek_for(self, key: str, pool: list):
        """What `next_for` would return, without advancing. For dry runs."""
        if not key or not pool:
            return None
        state = self.load()
        size = len(pool)
        index = ((state[key] + 1) % size) if key in state else _stable_offset(key, size)
        return pool[index]
