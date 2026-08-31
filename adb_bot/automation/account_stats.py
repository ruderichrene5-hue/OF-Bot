"""Per-account Instagram stats for the GeeLark dashboard: handle, follower
count, post count.

There is no dedicated scan pass for this -- it piggybacks on the profile-tab
visit `InstagramReelUploadU2Flow` already makes every time it posts (see
`read_account_stats` on that class), so a value here is only as fresh as the
account's last post. That trade-off was explicit: a separate scan would cost
its own time on the fleet's 4 real proxy modems, and stats a few days stale
are still useful for a dashboard, unlike a stale posting decision.

Storage is an append-only JSON-lines file, same pattern and same reasoning as
post_ledger.py: a torn write costs one line, not the whole store, and two
loops writing at once cannot corrupt each other's state. Later lines for the
same phone_id win.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

STATS_FILENAME = "geelark_account_stats.jsonl"


@dataclass
class AccountStatsRecord:
    phone_id: str
    at: float
    handle: str = ""
    followers: int = -1          # -1 = unreadable this time, not "zero"
    followers_exact: bool = False   # False means rounded ("12.3K"), not exact
    posts: int = -1
    posts_exact: bool = False


class AccountStatsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else (get_app_data_dir() / STATS_FILENAME)

    def _iter_records(self):
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield AccountStatsRecord(**json.loads(line))
                    except Exception:
                        # A torn or hand-edited line must not take the whole
                        # store down with it -- skip it and keep reading.
                        continue
        except FileNotFoundError:
            return

    def load(self) -> dict:
        """phone_id -> the most recent AccountStatsRecord for that phone."""
        folded: dict = {}
        for record in self._iter_records():
            folded[record.phone_id] = record
        return folded

    def record(self, phone_id: str, *, handle: str | None = None,
              followers=None, posts=None) -> AccountStatsRecord | None:
        """Append a snapshot. `followers`/`posts` are reel_verify.Count (or
        None) -- the same shape the flow's own baseline-count reads use."""
        if not phone_id:
            return None
        rec = AccountStatsRecord(
            phone_id=str(phone_id),
            at=time.time(),
            handle=str(handle or ""),
            followers=int(getattr(followers, "value", -1)) if followers is not None else -1,
            followers_exact=bool(getattr(followers, "exact", False)) if followers is not None else False,
            posts=int(getattr(posts, "value", -1)) if posts is not None else -1,
            posts_exact=bool(getattr(posts, "exact", False)) if posts is not None else False,
        )
        self._append(rec)
        return rec

    def _append(self, record: AccountStatsRecord) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(record), ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except Exception:
            # A stats write failing must never take a posting cycle down with
            # it -- this store is a convenience for the dashboard, not a
            # dependency of anything that posts.
            return False
