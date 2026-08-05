from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Profile:
    id: str
    status: str
    ip: str | None = None
    port: str | None = None
    pwd: str | None = None
    caption: str | None = None
    bio: str | None = None
    picture: str | None = None
    media_path: str | None = None   # per-run reel/post video (Posting Queue loop)
    # Posting Queue row this run belongs to. The reel flow stamps it on the
    # local post ledger, and the deferred recheck matches ledger entries to
    # Airtable rows by it -- without it every entry is unmatchable and a
    # Verifying row can never be resolved.
    queue_id: str | None = None
    # Which Instagram account to post as, on the phones that have two logged
    # into one Instagram install. Empty (the normal case) means "post as
    # whoever is signed in"; set, the reel flow switches to it first and
    # abandons the post if it cannot confirm the switch took.
    ig_handle: str | None = None

    @property
    def target(self) -> str | None:
        if self.ip and self.port:
            return f"{self.ip}:{self.port}"
        return None

    @property
    def is_ready(self) -> bool:
        return (
            self.status == "active"
            and bool(self.ip)
            and bool(self.port)
            and bool(self.pwd)
        )
