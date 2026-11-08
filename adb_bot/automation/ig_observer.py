"""Instagram observer: read public stats of our accounts from one watcher phone.

A separate, disposable "observer" Instagram account on one Android phone (MLX
cloud phone for now, a real Android later -- same code, it only needs an adb
serial) opens each tracked account's public profile and records:

  * status     -- ok / not_found (deleted, banned or renamed) / private
  * followers  -- exact where Instagram shows it, else expanded from "10,6K"
  * reels      -- the latest N reels from the Reels tab, each with its exact view
                  count (Instagram writes it into the grid tile's description),
                  plus likes and comments for the first `detail` reels (opened
                  one by one and swiped through)

Nothing is liked, followed or commented -- look only. One snapshot line per
account per run goes to ~/.adb_bot/observer/snapshots.jsonl; the dashboard
reads that file. Reels carry no date on screen, so each reel is identified by a
small perceptual hash of its grid thumbnail and dated by when it was first seen.

Added 2026-09-30 (operator request): overview of followers and reel views per
account for 24h / 7d / 30d, independent of which device an account lives on.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path

IG = "com.instagram.android"
STORE_DIR = Path.home() / ".adb_bot" / "observer"
SNAPSHOTS = STORE_DIR / "snapshots.jsonl"

_SUFFIX = {"k": 1_000, "tsd": 1_000, "m": 1_000_000, "mio": 1_000_000, "mrd": 1_000_000_000, "b": 1_000_000_000}


def parse_ig_number(text) -> tuple[int | None, bool]:
    """'1.037' -> (1037, True); '10,6K' -> (10600, False); '2,5 Mio.' -> (2500000, False).

    Plain numbers use '.' or ',' as a thousands separator (either locale).
    With a K/M suffix the separator is a decimal point. Returns (value, exact).
    """
    if text is None:
        return None, False
    s = str(text).strip().lower().replace(" ", " ").replace(" ", " ")
    m = re.search(r"(\d[\d.,]*)\s*(k|tsd|mio|mrd|m|b)?\b\.?", s)
    if not m:
        return None, False
    num, suf = m.group(1), m.group(2)
    if suf:
        num = num.replace(",", ".")
        if num.count(".") > 1:          # "1.234,5K" -- unlikely, but don't guess
            num = num.replace(".", "", num.count(".") - 1)
        try:
            return int(round(float(num) * _SUFFIX[suf])), False
        except ValueError:
            return None, False
    digits = re.sub(r"[^\d]", "", num)
    return (int(digits), True) if digits else (None, False)


@dataclass
class Reel:
    pos: int
    views: int | None
    thumb: str = ""            # perceptual hash of the grid tile (identity across days)
    likes: int | None = None
    comments: int | None = None


@dataclass
class Snapshot:
    handle: str
    at: float
    status: str                # ok | not_found | private | error
    followers: int | None = None
    followers_exact: bool = False
    reels: list = field(default_factory=list)
    detail: str = ""


def _dhash(image, box) -> str:
    im = image.crop(box).convert("L").resize((9, 8))
    px = list(im.get_flattened_data()) if hasattr(im, 'get_flattened_data') else list(im.getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def _ham(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _bounds(b: str):
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", b or "0,0,0,0")[:4])
    return x1, y1, x2, y2


def merge_overlap(seq: list, cur: list) -> int:
    """How many leading items of `cur` are already the tail of `seq` (whole rows of 3 first)."""
    best = 0
    for k in range(min(len(seq), len(cur)), 0, -1):
        if seq[-k:] == cur[:k]:
            if k % 3 == 0:
                return k
            best = best or k
    return best


class Observer:
    def __init__(self, serial: str, logger=None):
        import uiautomator2 as u2
        self.serial = serial
        self.d = u2.connect(serial)
        self.log = logger

    def _say(self, *a):
        if self.log:
            self.log.info(" ".join(str(x) for x in a))

    def _nodes(self):
        return list(ET.fromstring(self.d.dump_hierarchy()).iter("node"))

    def _open_profile(self, handle: str) -> None:
        subprocess.run(["adb", "-s", self.serial, "shell", "am", "start", "-a",
                        "android.intent.action.VIEW", "-d",
                        f"https://www.instagram.com/{handle}/", IG],
                       capture_output=True, timeout=30)

    def _wait_header(self, handle: str, timeout: float = 20) -> str:
        """'ok' once the profile header is up, else 'not_found' / 'private' / 'error'."""
        end = time.time() + timeout
        while time.time() < end:
            texts = " | ".join((n.get("text") or n.get("content-desc") or "") for n in self._nodes()).lower()
            if "isn't available" in texts or "user not found" in texts or "nicht verfügbar" in texts \
                    or "page isn't available" in texts or "this account doesn't exist" in texts:
                return "not_found"
            if self.d(resourceIdMatches=r".*profile_header_familiar_followers_value").exists:
                if "this account is private" in texts or "dieses konto ist privat" in texts:
                    return "private"
                return "ok"
            time.sleep(1.5)
        # A handle that doesn't exist (deleted / banned / renamed) opens a blank
        # page in Instagram's URL handler -- no text at all, no header.
        focus = subprocess.run(["adb", "-s", self.serial, "shell", "dumpsys", "window"],
                               capture_output=True, text=True, timeout=20).stdout
        app_text = [n for n in self._nodes() if (n.get("package") == IG)
                    and (n.get("text") or n.get("content-desc"))]
        if "UrlHandlerActivity" in focus and not app_text:
            return "not_found"
        return "error"

    def _followers(self):
        n = self.d(resourceIdMatches=r".*profile_header_familiar_followers_value")
        if not n.exists:
            return None, False
        return parse_ig_number(n.info.get("text") or n.info.get("contentDescription"))

    def _screen_tiles(self) -> list:
        """Fully visible Reels-tab tiles on screen, top-left to bottom-right: [(views, bounds)]."""
        out = []
        for n in self._nodes():
            desc = n.get("content-desc") or ""
            if "View Count" not in desc or not (n.get("resource-id") or "").endswith("preview_clip_thumbnail"):
                continue
            x1, y1, x2, y2 = _bounds(n.get("bounds"))
            if y2 - y1 < (x2 - x1) * 1.2:          # cut off by the screen edge
                continue
            out.append((parse_ig_number(desc.split("View Count", 1)[1])[0], (y1, x1)))
        return [v for v, _ in sorted(out, key=lambda t: t[1])]

    def _grid_reels(self, max_reels: int) -> list:
        """Latest reels with their view counts, newest first.

        Tiles autoplay a preview, so their pixels change between screenshots and
        cannot identify a reel. Instead each screen's ordered run of view counts
        is stitched onto what we have by the longest overlap (whole rows of 3),
        which is what a scroll produces."""
        tab = self.d(description="Reels", resourceIdMatches=r".*profile_tab_icon_view")
        if not tab.exists:
            return []
        tab.click()
        time.sleep(4)
        w, h = self.d.window_size()
        seq: list = []
        stale = 0
        while len(seq) < max_reels and stale < 2:
            cur = self._screen_tiles()
            k = merge_overlap(seq, cur)
            added = cur[k:]
            seq.extend(added)
            stale = 0 if added else stale + 1
            self.d.swipe(w // 2, int(h * 0.80), w // 2, int(h * 0.45), 0.5)
            time.sleep(2.5)
        return [Reel(pos=i + 1, views=v) for i, v in enumerate(seq[:max_reels])]

    def _reel_details(self, reels: list, detail: int) -> None:
        """Open the first reel from the top of the Reels tab and swipe through `detail` of them."""
        if not reels or detail <= 0:
            return
        tab = self.d(description="Reels", resourceIdMatches=r".*profile_tab_icon_view")
        w, h = self.d.window_size()
        for _ in range(6):                                   # back to the top of the grid
            self.d.swipe(w // 2, int(h * 0.35), w // 2, int(h * 0.85), 0.3)
        time.sleep(2)
        first = self.d(resourceIdMatches=r".*preview_clip_thumbnail")
        if not first.exists:
            return
        b = first.info["bounds"]
        self.d.click((b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2)
        time.sleep(5)
        for i in range(min(detail, len(reels))):
            like = self.d(resourceIdMatches=r".*like_count")
            com = self.d(resourceIdMatches=r".*comment_count")
            if like.exists:
                reels[i].likes = parse_ig_number(like.info.get("contentDescription") or like.info.get("text"))[0]
            if com.exists:
                reels[i].comments = parse_ig_number(com.info.get("contentDescription") or com.info.get("text"))[0]
            self.d.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.20), 0.25)
            time.sleep(3)
        self.d.press("back")
        time.sleep(1)

    def observe(self, handle: str, max_reels: int = 30, detail: int = 10) -> Snapshot:
        """One retry with a fresh uiautomator2 connection: the cloud phone's
        automation link drops now and then ("Remote end closed connection")."""
        snap = self._observe_once(handle, max_reels, detail)
        if snap.status == "error" and any(k in (snap.detail or "") for k in ("Remote", "Connect", "offline", "not online")):
            import uiautomator2 as u2
            time.sleep(10)
            self.d = u2.connect(self.serial)
            snap = self._observe_once(handle, max_reels, detail)
        return snap

    def _observe_once(self, handle: str, max_reels: int, detail: int) -> Snapshot:
        snap = Snapshot(handle=handle, at=time.time(), status="error")
        try:
            self._open_profile(handle)
            snap.status = self._wait_header(handle)
            if snap.status in ("ok", "private"):
                snap.followers, snap.followers_exact = self._followers()
            if snap.status == "not_found":
                self.d.press("back")
            if snap.status == "ok":
                reels = self._grid_reels(max_reels)
                self._reel_details(reels, detail)
                snap.reels = [asdict(r) for r in reels]
        except Exception as exc:                            # one bad profile must not stop the round
            snap.status, snap.detail = "error", f"{type(exc).__name__}: {exc}"[:300]
        self._say(f"observer: {handle} {snap.status} followers={snap.followers} reels={len(snap.reels)}")
        return snap


def save(snaps: list) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with SNAPSHOTS.open("a", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ig_observer")
    ap.add_argument("serial", help="adb serial of the observer phone (ip:port or USB id)")
    ap.add_argument("handles", nargs="+")
    ap.add_argument("--reels", type=int, default=30)
    ap.add_argument("--detail", type=int, default=10, help="reels to open for likes/comments")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args(argv)
    obs = Observer(a.serial)
    snaps = []
    for h in a.handles:
        s = obs.observe(h.lstrip("@"), a.reels, a.detail)
        snaps.append(s)
        views = [r["views"] for r in s.reels if r["views"] is not None]
        print(json.dumps({"handle": s.handle, "status": s.status, "followers": s.followers,
                          "reels": len(s.reels), "views_sum": sum(views),
                          "first5": [(r["views"], r["likes"], r["comments"]) for r in s.reels[:5]],
                          "detail": s.detail}, ensure_ascii=False), flush=True)
        time.sleep(3)
    if not a.no_save:
        save(snaps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
