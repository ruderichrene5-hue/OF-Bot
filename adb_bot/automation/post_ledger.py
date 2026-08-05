"""Remember that a reel was sent, the instant it is sent.

Verification asks "did it land?" and sometimes genuinely cannot answer --
Instagram uploads asynchronously and any fixed window is a race. This module
answers a different question, one that is *always* answerable locally: "did we
already tap Share for this clip on this account?"

That distinction is the whole point. The expensive failure was never an
inaccurate log, it was a **double post**: unconfirmed -> clip stays queued ->
row marked Failed -> someone retries -> the same reel goes out twice. Once this
ledger exists a retry can consult it and refuse, so *being uncertain stops being
dangerous* -- which is in turn what lets the in-run verification window shrink
from five minutes to well under one.

The record is written **before** verification runs and independently of
Airtable, so it survives the two failures that actually happen: a crash during
verification, and an Airtable write that never lands.

Storage is an append-only JSON-lines file. Appending is crash-safe in a way that
rewriting a whole JSON document is not -- a torn write costs one line, not the
ledger. Later lines for the same key win, so `resolve()` is just another append
and two loops writing at once cannot corrupt each other's state.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

# What we know about a share, in increasing order of certainty.
STATUS_SHARED = "shared"          # Share was tapped; outcome not yet known
STATUS_CONFIRMED = "confirmed"    # proven live (post count / banner / recheck)
STATUS_DISPROVED = "disproved"    # proven NOT live -- safe to send again

LEDGER_FILENAME = "posted_reels.jsonl"
DEFAULT_MAX_AGE_DAYS = 30

_HASH_CHUNK = 1024 * 1024


def ledger_key(profile_id, media_hash, ig_handle="") -> str:
    """Identity of "this clip, on this account".

    The handle is only appended when there is one, so a single-account phone
    keys as "<profile>:<hash>" -- byte-identical to every record written before
    two-account phones existed.
    """
    handle = str(ig_handle or "").strip().lstrip("@").strip().lower()
    base = f"{profile_id}:{media_hash}"
    return f"{base}:{handle}" if handle else base


@dataclass
class ShareRecord:
    """One "we tapped Share" event, plus whatever we later learned about it."""

    profile_id: str
    media_hash: str
    status: str = STATUS_SHARED
    shared_at: float = 0.0
    resolved_at: float = 0.0
    media_path: str = ""
    caption: str = ""
    queue_id: str = ""
    detail: str = ""
    # Which Instagram account on that phone the clip went to. One MLX profile
    # can hold two accounts, so `profile_id` alone stops identifying an account
    # -- without this, the first account to post a clip would block the second
    # from ever posting it. Empty on the single-account phones (and on every
    # record written before this field existed), which keys them exactly as
    # before, so the whole existing ledger keeps working untouched.
    ig_handle: str = ""
    # The account's post count as it was just before this share. The deferred
    # recheck has no other baseline to work from -- it arrives fifteen minutes
    # later with no memory of the run -- so carrying it here is what turns the
    # recheck into an exact +1 comparison instead of another guess. -1 means it
    # could not be read (rounded "1.2K" counts are stored as not-exact).
    baseline_count: int = -1
    baseline_exact: bool = False

    @property
    def key(self) -> str:
        return ledger_key(self.profile_id, self.media_hash, self.ig_handle)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - (self.shared_at or 0.0))

    def blocks_repost(self) -> bool:
        """Whether this record should stop the same clip going out again.

        Only positive *disproof* clears the way. An unresolved share blocks,
        because "we don't know" and "it didn't post" are different things and
        conflating them is exactly what caused double posts.
        """
        return self.status in (STATUS_SHARED, STATUS_CONFIRMED)


def media_fingerprint(media_path) -> str:
    """A stable content id for a clip: sha256 of the file.

    Content-based rather than path-based on purpose -- the spoof pipeline moves
    and renames variants, and a queue row can be re-planned, but the bytes we
    actually uploaded are the thing we must not upload twice. Returns "" when the
    file can't be read, and callers treat that as "no idea", never as "safe".
    """
    try:
        path = Path(media_path)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


class PostLedger:
    """Append-only record of every reel this machine has sent."""

    def __init__(self, path=None):
        self.path = Path(path) if path else (get_app_data_dir() / LEDGER_FILENAME)

    # -- reading ---------------------------------------------------------

    def _iter_records(self):
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield ShareRecord(**json.loads(line))
                    except Exception:
                        # A torn or hand-edited line must not take the ledger
                        # down with it -- skip it and keep reading.
                        continue
        except FileNotFoundError:
            return

    def load(self) -> dict:
        """key -> the most recent record for that key."""
        folded: dict = {}
        for record in self._iter_records():
            folded[record.key] = record
        return folded

    def lookup(self, profile_id: str, media_hash: str, ig_handle: str = ""):
        if not profile_id or not media_hash:
            return None
        return self.load().get(ledger_key(profile_id, media_hash, ig_handle))

    def already_shared(self, profile_id: str, media_path, ig_handle: str = "") -> bool:
        """True when this clip has been sent to this account and nothing has
        since disproved it. The guard a retry path should consult *before*
        posting.

        An unreadable file yields no fingerprint, and therefore no opinion --
        this returns False and the caller falls back to its own judgement,
        because blocking every post on a hashing failure would be worse than
        the risk it removes.
        """
        record = self.lookup(profile_id, media_fingerprint(media_path), ig_handle)
        return bool(record and record.blocks_repost())

    # -- writing ---------------------------------------------------------

    def _append(self, record: ShareRecord) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(record), ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except Exception:
            # The ledger is a safety net, not a dependency: never let a write
            # failure abort a post that is already in flight.
            return False

    def record_share(self, profile_id: str, media_path, caption: str = "",
                     queue_id: str = "", media_hash: str | None = None,
                     baseline_count=None, ig_handle: str = "") -> ShareRecord | None:
        """Note that Share was just tapped. Call this *before* verification.

        `baseline_count` is the reel_verify.Count read before the upload (or
        None); it is what lets the deferred recheck do an exact comparison.

        Returns the record, or None if no fingerprint could be taken (in which
        case there is nothing meaningful to remember).
        """
        digest = media_hash if media_hash is not None else media_fingerprint(media_path)
        if not profile_id or not digest:
            return None
        record = ShareRecord(
            profile_id=str(profile_id),
            media_hash=digest,
            status=STATUS_SHARED,
            shared_at=time.time(),
            media_path=str(media_path or ""),
            caption=str(caption or "")[:500],
            queue_id=str(queue_id or ""),
            ig_handle=str(ig_handle or "").strip().lstrip("@").strip().lower(),
            baseline_count=int(getattr(baseline_count, "value", -1)),
            baseline_exact=bool(getattr(baseline_count, "exact", False)),
        )
        self._append(record)
        return record

    def resolve(self, profile_id: str, media_hash: str, status: str, detail: str = "",
                ig_handle: str = "") -> bool:
        """Record what we eventually learned. `status` is STATUS_CONFIRMED or
        STATUS_DISPROVED; only the latter re-opens the clip for another send."""
        if not profile_id or not media_hash:
            return False
        existing = self.lookup(profile_id, media_hash, ig_handle)
        record = ShareRecord(
            profile_id=str(profile_id),
            media_hash=str(media_hash),
            status=status,
            shared_at=existing.shared_at if existing else time.time(),
            resolved_at=time.time(),
            media_path=existing.media_path if existing else "",
            caption=existing.caption if existing else "",
            queue_id=existing.queue_id if existing else "",
            ig_handle=(existing.ig_handle if existing
                       else str(ig_handle or "").strip().lstrip("@").strip().lower()),
            detail=str(detail or "")[:300],
        )
        return self._append(record)

    def pending(self, older_than_seconds: float = 0.0) -> list:
        """Shares still unresolved -- what the deferred recheck pass works
        through. `older_than_seconds` skips ones too fresh to be worth looking
        at yet."""
        out = [r for r in self.load().values()
               if r.status == STATUS_SHARED and r.age_seconds >= older_than_seconds]
        out.sort(key=lambda r: r.shared_at)
        return out

    def prune(self, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> int:
        """Rewrite the file without records older than `max_age_days`, and
        collapse the append history to one line per key. Returns how many were
        dropped. Old entries are safe to lose: the media queue will not offer a
        month-old clip again, and the queue row is long since closed."""
        cutoff = time.time() - (max_age_days * 86400)
        keep = [r for r in self.load().values() if (r.shared_at or 0.0) >= cutoff]
        total = len(self.load())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                for record in sorted(keep, key=lambda r: r.shared_at):
                    handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            temp.replace(self.path)
        except Exception:
            return 0
        return total - len(keep)
