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
