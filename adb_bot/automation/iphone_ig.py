"""Instagram flows on the physical iPhones, driven over WebDriverAgent
(`iphone_farm_wda`). The iOS counterpart of the ADB flows in `flows/`.

    .venv/bin/python -m adb_bot.automation.iphone_ig warmup  iphone1 A6 --minutes 10 --follows 5
    .venv/bin/python -m adb_bot.automation.iphone_ig whoami  iphone1

Rules these flows keep:
- An account is ONLY ever opened through its container Shortcut (A1..A25 /
  B1..B25): the Shortcut closes Instagram, switches the Crane container, starts
  the container's Shadowrocket proxy and checks the exit IP before it opens
  Instagram. Launching Instagram directly would run the wrong container
  without its proxy. `open_account` therefore refuses to continue unless the
  Shortcut ended in Instagram AND the phone reports that container's VPN up.
- Every tap lands on a random point inside the element (never its centre),
  every swipe is a curved, eased multi-point path with its own speed, and
  every wait is drawn from a range -- see `Human`.

Instagram's iOS app exposes stable accessibility ids (checked 2026-10-06,
Instagram on iOS 16.7): mainfeed-tab, reels-tab, direct-inbox-tab,
explore-tab, profile-tab, create, activity, follow-button, more_options,
story-tray-cell-self, story-tray-profile-picture ("<handle>. Profile
picture"), media-cell, header-view.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time

from adb_bot.automation import iphone_farm_config
from adb_bot.automation.iphone_farm_wda import Phone, WDAError, connect, resolve, wait_for_foreground

IG_BUNDLE = "com.burbn.instagram"
LOG_DIR = os.path.expanduser("~/.adb_bot/iphone_ig")


class FlowError(RuntimeError):
    pass


# screens Instagram shows instead of the app when an account is held; flows must stop, not click through
CHECKPOINT_TEXTS = ("Confirm you're human", "Confirm it's you", "We suspended your account", "Your account has been disabled",
                    "Help us confirm", "Enter the code", "We've detected unusual activity", "Was this you")


# ---------------------------------------------------------------- human input

class Human:
    """Taps, swipes and pauses with the variance a thumb has. WDA cannot vary
    touch radius or pressure (measured 2026-10-06), so position, timing and
    path are what we can make human."""

    def __init__(self, p: Phone, log=None):
        self.p, self.log = p, log or (lambda *a: None)
        # Every touch goes through XXTouch with the phone's OWN recorded thumb (Rene, 09.10: "alles mit
        # XXTouch, auf gar keinen Fall das normale"; reconfirmed 11.10: "nur mit xxtouch", no WDA
        # substitute even when the licence is down -- fix it, don't paper over it with WDA taps).
        self.xx = None
        ref = getattr(p, "ref", None)
        from adb_bot.automation import iphone_farm_config, iphone_xxtouch
        if not (ref and iphone_farm_config.relay().get("touch", "xxtouch") != "wda"):
            return                                             # this phone is deliberately WDA-only (config)
        _phone = iphone_farm_config.load().get(ref) or {}
        if tuple(_phone.get("screen_px") or iphone_xxtouch.SCREEN_PX) != iphone_xxtouch.SCREEN_PX:
            # 13.10: iPhone 7/8 (G/H/I) have no recorded XXTouch gesture library at all -- this is not
            # a recoverable licence hiccup (Rene's "fix it, don't paper over" below is about THAT), it is
            # a model this library was simply never built for. Stay on WDA taps, no XXTouch attempt.
            self.log(f"{ref}: {_phone.get('model', '?')} has no XXTouch recordings -- WDA taps only")
            return
        xx = iphone_xxtouch.for_phone(ref)
        # 11.10: the service forgets its licence after a respring/reinstall although XXTExplorer
        # still has it cached (hit live on E1/E3/E4 today, 403 "requires a valid licence", every
        # single run on iPhone E). Opening the app once hands it back to the service; probe here
        # so recovery happens before the first real tap, not mid-warmup.
        probe = xx._post("/spawn", b"touch.init(0)\nsys.msleep(1)\n")
        if probe.get("code") == 403:
            self.log("XXTouch licence restricted -- opening XXTExplorer once to re-sync")
            p.launch("ch.xxtou.XXTExplorer")
            time.sleep(8)
            back_to_instagram(p)
            time.sleep(2)
            probe = xx._post("/spawn", b"touch.init(0)\nsys.msleep(1)\n")
        if probe.get("code") not in (0, None):
            # 11.10: this used to be caught by a blanket `except Exception` right here and just
            # logged -- self.xx stayed None and tap_xy/swipe fell through to their OWN separate
            # WDA branch regardless, silently substituting WDA for every touch of the whole run
            # despite the log line claiming "no WDA substitute". Raising for real is the fix --
            # confirmed live: E1/E3/E4's warmups today all ran on WDA taps this way.
            raise RuntimeError(f"XXTouch unusable on {ref}: {probe} -- not falling back to WDA")
        self.xx = xx

    @staticmethod
    def pause(lo: float, hi: float):
        # skewed toward the short end, with the occasional long look
        time.sleep(lo + (hi - lo) * random.random() ** 1.6)

    def _rect(self, el) -> dict:
        if el is None:
            raise FlowError("element to tap is not on screen")
        return el.rect() if hasattr(el, "rect") else el

    def tap(self, el, hold: tuple[float, float] = (0.06, 0.14)):
        """Tap a random point in the inner part of an element (or a {x,y,width,height} dict)."""
        r = self._rect(el)
        x = r["x"] + r["width"] * random.uniform(0.3, 0.7)
        y = r["y"] + r["height"] * random.uniform(0.3, 0.7)
        self.tap_xy(x, y, hold)

    def tap_xy(self, x: float, y: float, hold: tuple[float, float] = (0.06, 0.14)):
        if self.xx:                                       # recorded tap: its own duration, drift and pressure
            self.xx.tap(x + random.gauss(0, 1.5), y + random.gauss(0, 1.5))
            return
        x += random.gauss(0, 1.5)
        y += random.gauss(0, 1.5)
        ms = int(random.uniform(*hold) * 1000)
        self._actions([{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                       {"type": "pointerDown"}, {"type": "pause", "duration": ms},
                       {"type": "pointerUp"}])

    def swipe(self, x1, y1, x2, y2, seconds: float | None = None):
        """Curved, eased drag. Coordinates in points or 0..1 fractions."""
        (x1, y1), (x2, y2) = self.p._pt(x1, y1), self.p._pt(x2, y2)
        if self.xx:                                       # a recorded swipe bent onto this path
            self.xx.swipe(x1, y1, x2, y2, seconds)
            return
        seconds = seconds or random.uniform(0.22, 0.45)
        steps = random.randint(7, 11)
        bow = random.uniform(-0.12, 0.12) * math.hypot(x2 - x1, y2 - y1)   # thumbs arc
        nx, ny = -(y2 - y1), (x2 - x1)
        norm = math.hypot(nx, ny) or 1
        acts = [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"}]
        prev = 0.0
        for i in range(1, steps + 1):
            t = i / steps
            e = 1 - (1 - t) ** 2.2                                           # fast start, soft end
            b = math.sin(math.pi * t) * bow
            x = x1 + (x2 - x1) * e + nx / norm * b + random.gauss(0, 0.8)
            y = y1 + (y2 - y1) * e + ny / norm * b + random.gauss(0, 0.8)
            acts.append({"type": "pointerMove", "duration": max(8, int((t - prev) * seconds * 1000)),
                         "x": round(x), "y": round(y)})
            prev = t
        acts.append({"type": "pointerUp"})
        self._actions(acts)

    def reel_next(self) -> None:
        """Advance one reel. XXTouch: one of Rene's recorded flicks as recorded (own length, own tempo);
        WDA phones (7/8): the eased swipe, but short and quick like a real flick, not half the screen."""
        if self.xx:
            try:
                self.xx.flick(up=True)
                return
            except Exception as exc:                          # noqa: BLE001 -- no clean recordings: fall through
                self.log(f"flick fallback: {str(exc)[:60]}")
        w, h = self.p.size()
        y1 = h * random.uniform(0.6, 0.75)
        self.swipe(w * random.uniform(0.4, 0.65), y1, w * random.uniform(0.4, 0.65), y1 - h * random.uniform(0.15, 0.3),
                   random.uniform(0.1, 0.25))

    def reel_burst(self, count: int) -> None:
        """Flick through `count` reels quickly (a person skimming), then the caller decides where to stop."""
        if self.xx:
            try:
                self.xx.flick_burst(count)
                return
            except Exception as exc:                          # noqa: BLE001
                self.log(f"burst fallback: {str(exc)[:60]}")
        for _ in range(count):
            w, h = self.p.size()
            y1 = h * random.uniform(0.6, 0.75)
            self.swipe(w * random.uniform(0.4, 0.65), y1, w * random.uniform(0.4, 0.65), y1 - h * random.uniform(0.15, 0.3),
                       random.uniform(0.08, 0.16))
            time.sleep(random.uniform(0.2, 0.6))

    def reel_hold(self, seconds: float) -> None:
        """Finger resting on the reel (pauses playback) -- middle of the screen, away from the right-hand
        buttons and the caption at the bottom."""
        w, h = self.p.size()
        x, y = w * random.uniform(0.25, 0.6), h * random.uniform(0.3, 0.55)
        self.hold(x, y, seconds)

    def scroll_feed(self, strength: float = 1.0):
        """One flick up through a feed, start/end points wandering a little."""
        w, h = self.p.size()
        x = w * random.uniform(0.35, 0.7)
        y1 = h * random.uniform(0.66, 0.8)
        dist = h * random.uniform(0.32, 0.5) * strength
        self.swipe(x, y1, x + random.uniform(-18, 18), y1 - dist)

    def hold(self, x: float, y: float, seconds: float = 1.0):
        """Long press (context menus)."""
        if self.xx:
            self.xx.hold(x, y, seconds)
        else:
            self.p.long_press(x, y, seconds)

    def drag(self, x1: float, y1: float, x2: float, y2: float, hold: float = 0.35, seconds: float = 0.6):
        """Press, wait, move, lift (stickers)."""
        if self.xx:
            self.xx.drag(x1, y1, x2, y2, hold, seconds)
            return
        self._actions([{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"},
                       {"type": "pause", "duration": int(hold * 1000)},
                       {"type": "pointerMove", "duration": int(seconds * 1000), "x": round(x2), "y": round(y2)},
                       {"type": "pause", "duration": random.randint(100, 250)}, {"type": "pointerUp"}])

    def _actions(self, acts: list):
        self.p._req("POST", self.p._s("/actions"),
                    {"actions": [{"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
                                  "actions": acts}]})


# ---------------------------------------------------------------- Instagram helpers

def visible(p: Phone, name: str) -> list:
    """Elements with this accessibility id that are on screen right now."""
    w, h = p.size()
    out = []
    for el in p.find_all("predicate string", f"name == {json.dumps(name)} AND visible == 1"):
        try:
            r = el.rect()
        except WDAError:
            continue
        if r["width"] > 0 and 0 <= r["y"] and r["y"] + r["height"] <= h - 80:      # not under the tab bar
            out.append((el, r))
    return out


def current_handle(p: Phone) -> str | None:
    """The logged-in handle, read off the feed's own story bubble ("<handle>. Profile picture")."""
    for el in p.find_all("predicate string", "name == 'story-tray-profile-picture' AND label ENDSWITH '. Profile picture'")[:1]:
        lab = el.attr("label") or ""
        return lab.removesuffix(". Profile picture") or None
    return None


# every "back"/"close" Instagram uses on pushed screens (profile pages, viewers, composers)
BACK_PREDICATE = ("type == 'XCUIElementTypeButton' AND visible == 1 AND (name == 'BackButton' OR name == 'cancel-button' "
                  "OR name ENDSWITH 'back-button' OR name == 'gallery-close-button' OR name == 'discard-story-preview' "
                  "OR name == 'story-dismiss-button' "
                  "OR label == 'Back' OR label == 'Close' OR label == 'Close gallery' OR label == 'Dismiss')")


def go_tab(h: Human, tab: str):
    """mainfeed | reels | direct-inbox | explore | profile"""
    els = h.p.find_all("accessibility id", f"{tab}-tab")
    for _ in range(3):                                # on a sub-screen (Edit profile, ...): back out
        if els:
            break
        back = h.p.find_all("predicate string", "name == 'BackButton' AND visible == 1")
        if not back:
            break
        h.tap(back[0])
        h.pause(1.0, 2.0)
        els = h.p.find_all("accessibility id", f"{tab}-tab")
    for _ in range(6):                                    # inside a pushed screen: walk back out
        if els:
            break
        back = h.p.find("predicate string", BACK_PREDICATE)
        if back:
            h.tap(back)
        else:                                             # no button: iOS edge-swipe back, then a full-screen viewer swipe-down
            w, ht = h.p.size()
            h.swipe(2, ht * 0.5, w * 0.7, ht * 0.52, 0.3)
            h.pause(0.6, 1.0)
            if not h.p.find_all("accessibility id", f"{tab}-tab"):
                h.swipe(w * 0.5, ht * 0.3, w * 0.5, ht * 0.9, 0.3)
        h.pause(1.0, 1.8)
        els = h.p.find_all("accessibility id", f"{tab}-tab")
    if not els:
        raise FlowError(f"tab {tab} not found -- is Instagram in front?")
    h.tap(els[0])
    h.pause(1.2, 2.5)


def dismiss_popups(h: Human) -> str | None:
    """System alerts (notifications, tracking) and Instagram's own sheets that block a flow."""
    text = h.p.alert_text()
    if text:
        buttons = h.p.alert_buttons()
        for pref in ("Don't Allow", "Ask App Not to Track", "Not Now", "Cancel"):
            if pref in buttons:
                h.p.alert_dismiss(pref) if pref != "Ask App Not to Track" else h.p.alert_accept(pref)
                return f"alert: {text[:60]} -> {pref}"
    for label in ("Not now", "Not Now"):
        hits = [e for e in h.p.by_text(label) if e.visible()]
        if hits:
            h.tap(hits[0])
            return f"sheet -> {label}"
    return None


# ---------------------------------------------------------------- opening an account


def back_to_instagram(p) -> None:
    """12.10 (Rene: "immer Kurzbefehl"): bring Instagram back after a short detour (Files, XXTouch app) ONLY if
    it is still running -- same session, same container, same proxy. A dead Instagram is never launched
    directly here: the caller's recovery reopens it through the container Shortcut."""
    state = p.app_state(IG_BUNDLE)
    if state not in (2, 3, 4):                     # 1 = not running
        raise FlowError("Instagram is not running -- reopen it only via the container Shortcut")
    p.activate(IG_BUNDLE)

def open_account(device: str, container: str, expect_handle: str | None = None, log=print) -> Phone:
    """Run the container's Shortcut and wait for Instagram with that container's proxy up."""
    rec_id, _ = resolve(device)
    # 11.10: was [ABC] only -- D and E are real farm phones too (iphone_signup.py has run dozens
    # of containers on both today), this just never got updated after they were added.
    # 20.10 (Rene): extended from A-E to A-L to cover G/H/I (fleet since 10-12) and new F/J/K/L.
    if not re.fullmatch(r"[A-L]\d{1,2}", container):
        raise FlowError(f"bad container {container!r}")
    ok, msg = iphone_farm_config.run_shortcut(rec_id, container)
    if not ok:
        raise FlowError(f"shortcut {container}: {msg}")
    p = connect(device, timeout=60)
    # 11.10: this used to just poll activeAppInfo for 90 s with nothing tapping the container
    # Shortcut's own Allow prompts (SSH-to-self, Shadowrocket, ipify) -- those fire on every run,
    # not only the first, so warmup/post/picture/pfp timed out doing nothing on a real phone.
    # wait_for_foreground is the same tested prompt-handling signup's open_fresh_container uses.
    # 12.10 later, Rene live: a single 240 s shot was too short on a slow night, and the shortcut
    # itself sometimes just never gets going at all (iphone_signup.open_fresh_container's own
    # "patience over abort" fix, 300 s x 2 launches, applied here too) -- this path (warmup/post/
    # picture/pfp/the Highlight rollout) hit the exact same stalls tonight and had none of it.
    up = wait_for_foreground(p, IG_BUNDLE, container, 300, log=log)
    tries = 1
    while not up and tries < 2:
        tries += 1
        log(f"{container}: Instagram not up yet after launch {tries - 1} -- relaunching the shortcut")
        ok, msg = iphone_farm_config.run_shortcut(rec_id, container)
        if not ok:
            raise FlowError(f"shortcut {container}: {msg}")
        up = wait_for_foreground(p, IG_BUNDLE, container, 300, log=log)
    if not up:
        raise FlowError(f"shortcut {container} did not end in Instagram within {tries * 300} s "
                        f"({tries} launches) -- proxy check failed? first-run Allow prompt?")
    vpn = iphone_farm_config.vpn_state(rec_id)
    if not vpn.get("vpn") or (vpn.get("container") or "").split(" ")[0] != container:
        raise FlowError(f"Instagram is open but the VPN is not {container}'s: {vpn}")
    time.sleep(random.uniform(2.5, 4.5))                  # let the feed load like a person would
    # a checkpoint instead of the app: stop right here, touch nothing (A5, 2026-10-07)
    for marker in CHECKPOINT_TEXTS:
        hit = p.find("predicate string", f"label CONTAINS[c] {json.dumps(marker)}")
        if hit:
            raise FlowError(f"{container}: account is at a checkpoint ({(hit.attr('label') or marker)[:80]!r}) -- not touching it")
    handle = current_handle(p)
    if not handle:                                        # feed header not there: read the profile title instead
        try:
            go_tab(Human(p, log), "profile")
            t = p.find("predicate string", "name == 'user-switch-title-button'")
            handle = (t.attr("label") if t else None) or None
        except (FlowError, WDAError):
            handle = None
    log(f"open {container}: Instagram up, VPN {vpn.get('container')}, account @{handle}")
    if expect_handle and handle and handle.lower() != expect_handle.lower().lstrip("@"):
        raise FlowError(f"{container} opened @{handle}, expected @{expect_handle}")
    return p


# ---------------------------------------------------------------- warm-up

def warmup(p: Phone, minutes: float | None = None, follows: int | None = None, log=print, extras: bool = True) -> dict:
    """Scroll the feed and Reels for `minutes`, following `follows` accounts
    that Instagram itself shows with a Follow button (suggested posts in the
    feed, Reels), spread over the session -- never two in a row."""
    h = Human(p, log)
    minutes = minutes if minutes is not None else random.uniform(8, 10)   # Rene 2026-10-07: 8-10 min, never the same
    follows = follows if follows is not None else random.randint(3, 6)    # Rene 2026-10-07: 3-6 follows per warm-up day
    log(f"warm-up {minutes:.1f} min, {follows} follows")
    end = time.monotonic() + minutes * 60
    # one follow per equal slice of the session, at a random point inside it, >= 60 s apart --
    # three follows inside 70 s looked like a bot (2026-10-07 test)
    span = minutes * 60 * 0.85
    slots = [span * (i + random.uniform(0.15, 0.85)) / max(follows, 1) + minutes * 60 * 0.08 for i in range(follows)]
    for i in range(1, len(slots)):
        slots[i] = max(slots[i], slots[i - 1] + 60)
    start = time.monotonic()
    stats = {"swipes": 0, "follows": [], "reels_watched": 0, "popups": [], "extras": [], "minutes": round(minutes, 1)}
    where = "reels"                                       # Rene 2026-10-07: warm-up always on the Reels tab
    go_tab(h, "reels")

    n = errors = 0
    while time.monotonic() < end:
        n += 1
        try:
            where = _warmup_step(h, n, where, slots, start, stats, log, extras)
            errors = 0
        except WDAError as exc:
            # USB/WDA hiccups (capture start, cable, Mac on battery) -- wait and pick up again
            errors += 1
            log(f"WDA error {errors}: {exc}")
            if errors >= 6:
                from adb_bot.automation import iphone_diag
                iphone_diag.keep_screen(getattr(p, "ref", "?"), "warmup-wda", str(exc), wda=None, xx=h.xx, log_fn=log)
                raise
            time.sleep(10 * errors)
            p.sid = None
        except Exception as exc:                          # noqa: BLE001
            # 22.10 (Rene): a screen the warm-up does not understand -> screenshot + text first, then stop
            from adb_bot.automation import iphone_diag
            shot = iphone_diag.keep_screen(getattr(p, "ref", "?"), "warmup-stuck", f"{type(exc).__name__}: {exc}",
                                           wda=p, xx=h.xx, log_fn=log)
            log(f"warm-up stopped at step {n}: {exc} (screen: {shot or 'none'})")
            raise
    stats["follows_missed"] = len(slots)
    return stats


def _warmup_step(h: Human, n: int, where: str, slots: list, start: float, stats: dict, log, extras: bool = True) -> str:
    if n % 5 == 1:                                    # popups are rare; checking costs ~2 s
        pop = dismiss_popups(h)
        if pop:
            stats["popups"].append(pop)
            log(pop)
    due = slots and time.monotonic() - start >= slots[0]
    if due:
        target = _follow_one(h, stats, log)
        if target:
            slots.pop(0)                                  # else: this creator is already followed -- next reel
    # now and then something other than scrolling, like a person would (each picked at random, rare)
    if extras and not due and n > 2 and random.random() < 0.18:
        name = _random_extra(h, where, log)
        if name:
            stats["extras"].append(name)
    if where == "feed":
        h.scroll_feed(strength=random.uniform(0.6, 1.3))
        stats["swipes"] += 1
        # most posts get a glance, some get a real look
        h.pause(1.0, 3.5) if random.random() < 0.8 else h.pause(5, 14)
    else:
        h.reel_next()                                     # 22.10: a recorded flick as recorded, not a fixed half-screen drag
        stats["swipes"] += 1
        stats["reels_watched"] += 1
        h.pause(3, 16) if random.random() < 0.85 else h.pause(18, 35)
    return where


EXTRA_WEIGHTS = {"like": 4, "story": 1, "explore": 1, "scroll-back": 3, "comments": 2, "profile-visit": 2, "linger": 3}


def _random_extra(h: Human, where: str, log, choices: dict | None = None) -> str | None:
    """One small human detour; always ends back on a tab (go_tab walks back out of anything).
    `choices` (22.10, iphone_warmup day scripts): a {name: weight} subset of EXTRA_WEIGHTS -- day 1 only
    likes/lingers, day 4 has the whole repertoire."""
    p = h.p
    pool = choices or EXTRA_WEIGHTS
    choice = random.choices(list(pool), weights=list(pool.values()))[0]
    try:
        if choice == "like":
            likes = [e for e, r in visible(p, "like-button") if (e.attr("label") or "") == "Like"]
            if not likes:
                return None
            h.pause(1.5, 4.0)
            h.tap(likes[0])
        elif choice == "story":
            if where != "feed":
                go_tab(h, "mainfeed")
                go_tab(h, "mainfeed")                     # second tap scrolls to the top, where the tray is
            cells = [c for c in p.find_all("predicate string", "name BEGINSWITH 'story-tray-cell-' AND NOT name == 'story-tray-cell-self'")
                     if c.attr("visible")]
            if not cells:
                return None
            h.tap(random.choice(cells[:4]))
            for _ in range(random.randint(1, 4)):         # watch a few, tap ahead now and then
                h.pause(3, 9)
                # upper half only: the reply field sits at ~58 % height, and a tap there opens the
                # keyboard on a DM reply (happened 2026-10-07 -- nothing typed, but never again)
                h.tap_xy(*p._pt(random.uniform(0.75, 0.92), random.uniform(0.2, 0.42)))
            h.pause(1, 3)
            if p.find("predicate string", "name == 'Return' AND type == 'XCUIElementTypeButton'"):
                h.tap_xy(*p._pt(0.5, 0.3))                # a reply keyboard got opened: close it, type nothing
                h.pause(0.8, 1.2)
            close = p.find("predicate string", "name == 'story-dismiss-button'")
            h.tap(close) if close else h.swipe(0.5, 0.3, 0.5, 0.85, 0.3)
            h.pause(1, 2)
            go_tab(h, "reels")
        elif choice == "explore":
            go_tab(h, "explore")
            for _ in range(random.randint(2, 5)):
                h.scroll_feed(random.uniform(0.6, 1.1))
                h.pause(1.5, 4)
            tiles = [c for c in p.find_all("predicate string", "type == 'XCUIElementTypeCell' AND visible == 1") if c.rect()["height"] > 80]
            if tiles and random.random() < 0.6:
                h.tap(random.choice(tiles[:6]))
                h.pause(4, 12)
            go_tab(h, "reels")
        elif choice == "scroll-back":
            for _ in range(random.randint(1, 2)):          # back to the previous reel
                w, ht = p.size()
                h.swipe(w * random.uniform(0.4, 0.6), ht * 0.25, w * random.uniform(0.4, 0.6), ht * random.uniform(0.75, 0.85))
                h.pause(3, 10)
        elif choice == "comments":
            btns = [e for e, r in visible(p, "comment-button")]
            if not btns:
                return None
            h.tap(btns[0])
            h.pause(3, 9)
            # 11.10: the swipe-down this used to rely on does not reliably collapse Instagram's
            # iOS comment sheet (confirmed live, D4: it stayed open and the warmup loop kept
            # blind-swiping *inside* the comment list for the rest of the 5 min session, so the
            # follow-button never became visible again -- all 3 scheduled follows silently
            # missed). The sheet's own close affordance is a literal "Dismiss" button; use that
            # first, the swipe only as a fallback for a comment-sheet variant without one.
            dismiss = p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Dismiss' AND visible == 1")
            if dismiss:
                h.tap(dismiss)
            else:
                h.swipe(0.5, 0.4, 0.5, 0.95, 0.3)
            h.pause(1, 2)
            go_tab(h, "reels")
        elif choice == "profile-visit":
            users = [e for e in p.find_all("predicate string", "type == 'XCUIElementTypeButton' AND label BEGINSWITH 'Post by ' AND visible == 1")]
            if not users:
                return None
            h.tap(users[0])                               # the reel's creator
            h.pause(3, 8)
            if random.random() < 0.5:
                h.scroll_feed(0.8)
                h.pause(2, 5)
            back = p.find("predicate string", BACK_PREDICATE)
            h.tap(back) if back else go_tab(h, "reels")
        else:                                             # "linger": just look at one thing for a while
            h.pause(8, 20)
    except (FlowError, WDAError) as exc:
        log(f"extra {choice} skipped: {str(exc)[:80]}")
        try:
            go_tab(h, "reels")
        except Exception:
            pass
        return None
    log(f"extra: {choice}")
    return choice


def _follow_one(h: Human, stats: dict, log) -> str | None:
    """Follow the first fully visible account Instagram offers a Follow button for."""
    for el, r in visible(h.p, "follow-button"):
        label = el.attr("label") or ""                    # "Follow <handle>"
        if not label.startswith("Follow ") or label.startswith("Following"):
            continue
        handle = label.removeprefix("Follow ").strip()
        h.pause(0.8, 2.2)                                 # read who it is first
        h.tap(r)
        h.pause(1.0, 2.0)
        after = (el.attr("label") or "") if _alive(el) else ""
        ok = not after.startswith("Follow ")
        stats["follows"].append({"handle": handle, "confirmed": ok, "t": time.strftime("%H:%M:%S")})
        log(f"follow @{handle}: {'ok' if ok else 'button unchanged'}")
        return handle
    return None


def _alive(el) -> bool:
    try:
        el.rect()
        return True
    except WDAError:
        return False


# ---------------------------------------------------------------- posting

def _wait_id(p: Phone, name: str, timeout: float = 20) -> dict | None:
    """Rect of the first visible element with this accessibility id, waiting for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for el in p.find_all("predicate string", f"name == {json.dumps(name)} AND visible == 1")[:1]:
            try:
                return el.rect()
            except WDAError:
                pass
        time.sleep(1)
    return None


def _tap_id(h: Human, name: str, timeout: float = 20, what: str = ""):
    r = _wait_id(h.p, name, timeout)
    if not r:
        raise FlowError(f"{what or name}: '{name}' did not appear within {timeout:.0f} s")
    h.tap(r)


def post_count(h: Human) -> int | None:
    """Posts on the logged-in profile, from the profile header ("2 posts")."""
    go_tab(h, "profile")
    for el in h.p.find_all("accessibility id", "user-detail-header-media-button")[:1]:
        m = re.match(r"([\d.,]+)", (el.attr("value") or "").strip())
        if m:
            return int(re.sub(r"[.,]", "", m.group(1)))
    return None


def post_photo(p: Phone, cell: int = 0, caption: str = "", log=print) -> dict:
    """Post one picture from the camera roll as a feed post.

    `cell` is its position in Instagram's gallery strip (0 = newest). Instagram
    only sees the photos the phone gave it access to -- with "Limited" access
    that is a hand-picked handful, so set Photos access to Full for Instagram.
    Confirmed by the profile's post count going up by one."""
    h = Human(p, log)
    pop = dismiss_popups(h)
    if pop:
        log(pop)
    before = post_count(h)
    go_tab(h, "mainfeed")
    go_tab(h, "mainfeed")                            # second tap scrolls to the top, where `create` is
    _tap_id(h, "create", what="feed")
    if not _wait_id(p, "next-button", 15):
        raise FlowError("composer did not open")
    if not p.by_text("New post"):
        raise FlowError("composer is not in POST mode")
    h.pause(1.0, 2.5)                                # look at the gallery first
    _tap_id(h, f"gallery-photo-cell-{cell}", 10, what=f"gallery picture {cell} (Photos access limited?)")
    h.pause(1.0, 2.0)
    _tap_id(h, "next-button", what="gallery -> edit")
    _wait_id(p, "edit-button", 20)                   # edit screen (filters, audio, text)
    h.pause(1.0, 2.5)
    _tap_id(h, "next-button", what="edit -> caption")
    r = _wait_id(p, "caption-cell-text-view", 20)
    if not r:
        raise FlowError("caption screen did not open")
    if caption:
        h.tap(r)
        h.pause(1.5, 2.5)                            # caption editor + keyboard slide in
        p.type(caption)
        h.pause(0.8, 1.6)
        _tap_id(h, "OK", 10, what="caption editor")
        h.pause(1.0, 2.0)
    _tap_id(h, "share-button", what="caption -> share")
    log("share tapped, uploading")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:               # back on the feed once the composer is gone
        time.sleep(3)
        try:
            if _wait_id(p, "mainfeed-tab", 1) and not _wait_id(p, "share-button", 1):
                break
        except WDAError:
            pass
    time.sleep(random.uniform(8, 14))                # "Posting..." bar on the feed
    after = post_count(h)
    ok = before is not None and after is not None and after == before + 1
    log(f"post count {before} -> {after}: {'posted' if ok else 'NOT CONFIRMED'}")
    go_tab(h, "mainfeed")
    return {"posted": ok, "before": before, "after": after, "cell": cell, "caption": caption}


def _avatar_crop(p: Phone, r: dict) -> bytes:
    """Pixels of the avatar circle on Edit profile, to tell whether the picture changed."""
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(p.screenshot())).convert("L")
    k = img.width / p.size()[0]                      # screenshot pixels per point
    box = tuple(int(v * k) for v in (r["x"] + r["width"] * .2, r["y"] + r["height"] * .2,
                                     r["x"] + r["width"] * .8, r["y"] + r["height"] * .8))
    return img.crop(box).resize((24, 24)).tobytes()


def set_profile_picture(p: Phone, cell: int = 0, log=print) -> dict:
    """Profile -> Edit profile -> Edit picture -> Choose from library -> gallery
    picture `cell` -> Done. Instagram uploads straight away (no crop step on
    iOS, checked 2026-10-06); confirmed by the avatar pixels changing."""
    h = Human(p, log)
    pop = dismiss_popups(h)
    if pop:
        log(pop)
    go_tab(h, "profile")
    hits = [e for e in p.by_text("Edit profile") if e.visible()]
    if not hits:
        raise FlowError("no Edit profile button on the profile")
    h.tap(hits[0])
    avatar = _wait_id(p, "Edit profile picture", 15)
    if not avatar:
        raise FlowError("Edit profile did not open")
    h.pause(1.5, 3.0)                                 # the old picture has to be loaded to compare
    before = _avatar_crop(p, avatar)
    _tap_id(h, "change-profile-photo-button", what="edit picture")
    h.pause(1.0, 2.0)
    lib = p.wait_text("Choose from library", 8)
    if lib is None:
        raise FlowError("picture sheet has no 'Choose from library'")
    h.tap(lib)                                            # XXTouch, not WDA's element click
    if not _wait_id(p, "done-button", 15):
        raise FlowError("library picker did not open")
    h.pause(1.0, 2.5)
    _tap_id(h, f"gallery-photo-cell-{cell}", 10, what=f"gallery picture {cell} (Photos access limited?)")
    h.pause(1.5, 3.0)                                 # look at the round crop preview
    _tap_id(h, "done-button", what="library -> done")
    log("picture chosen, uploading")
    changed = False
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not changed:
        time.sleep(4)
        avatar = _wait_id(p, "Edit profile picture", 2)
        if avatar:
            changed = _avatar_crop(p, avatar) != before
    log(f"profile picture {'changed' if changed else 'NOT CHANGED'}")
    h.pause(1.0, 2.0)
    _tap_id(h, "BackButton", 5, what="edit profile -> back")
    go_tab(h, "mainfeed")
    return {"changed": changed, "cell": cell}


# ---------------------------------------------------------------- profile edits

def human_type(h: Human, text: str, el=None):
    """Type in bursts of 1-6 characters with reading/thinking gaps in between."""
    i = 0
    while i < len(text):
        n = random.randint(1, 6)
        chunk = text[i:i + n]
        (el.type(chunk) if el is not None else h.p.type(chunk))
        i += n
        h.pause(0.08, 0.45) if random.random() < 0.85 else h.pause(0.6, 1.6)


def open_edit_profile(h: Human):
    go_tab(h, "profile")
    btn = h.p.find("predicate string", "name == 'profile-action-bar-button' AND label == 'Edit profile'")
    if not btn:
        raise FlowError("Edit profile button not found on the profile tab")
    h.tap(btn)
    h.pause(1.8, 3.0)
    if not h.p.find("predicate string", "name == 'profile-bio'"):
        raise FlowError("Edit profile screen did not open")


def _back(h: Human):
    b = h.p.find("predicate string", "name == 'BackButton'")
    if b:
        h.tap(b)
        h.pause(1.2, 2.2)


def _value(p: Phone, predicate: str, tries: int = 5) -> str:
    """An element's value, looked up fresh each try: Instagram re-renders these screens while we
    read them, so a once-found element goes stale ("stale element reference", 2026-10-06)."""
    for _ in range(tries):
        try:
            el = p.find("predicate string", predicate)
            if el:
                return el.attr("value") or ""
        except WDAError:
            pass
        time.sleep(1.2)
    return ""


def get_bio(p: Phone, log=print) -> str:
    h = Human(p, log)
    open_edit_profile(h)
    bio = _value(p, "name == 'profile-bio'")
    _back(h)
    return bio


def set_bio(p: Phone, bio: str, log=print) -> dict:
    """Replace the bio (max 150 chars) and read it back from the profile."""
    if len(bio) > 150:
        raise FlowError(f"bio is {len(bio)} chars, Instagram allows 150")
    h = Human(p, log)
    open_edit_profile(h)
    before = _value(h.p, "name == 'profile-bio'")
    h.tap(h.p.find("predicate string", "name == 'profile-bio'"))
    h.pause(1.5, 2.5)
    box = h.p.find("predicate string", "type == 'XCUIElementTypeTextView' AND label == 'Write a profile bio'")
    if not box:
        raise FlowError("bio editor did not open")
    box.clear()
    h.pause(0.4, 1.0)
    if (box.attr("value") or "") not in ("", "Write a profile bio"):
        raise FlowError(f"bio field did not clear: {box.attr('value')!r}")
    lines = bio.split("\n")
    for i, line in enumerate(lines):
        human_type(h, line, box)
        if i < len(lines) - 1:
            box.type("\n")
    h.pause(0.8, 1.8)
    typed = _value(h.p, "type == 'XCUIElementTypeTextView' AND label == 'Write a profile bio'")
    done = h.p.find("predicate string", "name == 'done-button'")
    h.tap(done)
    h.pause(2.0, 3.5)
    after = _value(h.p, "name == 'profile-bio'")
    _back(h)
    ok = after.strip() == bio.strip()
    log(f"bio: {before!r} -> {after!r} ({'ok' if ok else 'MISMATCH, typed ' + repr(typed)})")
    return {"before": before, "after": after, "ok": ok}


# ---------------------------------------------------------------- media in / profile picture

def drive_download_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def import_photo(p: Phone, url: str, log=print):
    """Put an image into the phone's Photos library: open it in Safari, long-press, Save to Photos.

    Over the internet on purpose: with the container's VPN up, EVERYTHING goes through the
    proxy, so a file on the Mac/LAN is unreachable (Safari: "Proxy Authentication Required",
    2026-10-06). Drive's uc?export=download link works; Safari first asks View/Download."""
    h = Human(p, log)
    try:
        p._req("POST", p._s("/orientation"), {"orientation": "PORTRAIT"})
    except WDAError:
        pass
    p.open_url(url)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        view = p.find("predicate string", "name == 'View' AND type == 'XCUIElementTypeButton'")
        if view:
            view.click()                                  # native WDA tap -- see landscape note below
            h.pause(2.5, 4)
            break
        time.sleep(1.5)
    img = [e for e in p.find_all("predicate string", "type == 'XCUIElementTypeImage'")
           if (lambda r: r["x"] >= 0 and r["width"] > 200)(e.rect())]
    if not img:
        raise FlowError(f"Safari shows no image for {url}")
    r = img[0].rect()
    # 12.10 live bug on B: the /orientation PORTRAIT request above doesn't stick -- Safari (and
    # Files, in media_to_photos/save_to_photos below) comes up LANDSCAPE regardless (phone is
    # physically mounted rotated; Instagram itself is portrait-locked so this never showed up
    # until a landscape-capable app was driven tonight). XXTouch's tap/hold targets the RAW
    # physical digitizer in the device's native portrait pixel grid and does not know about the
    # software rotation, so a hold computed from WDA's (correctly rotated) element rect lands
    # nowhere near the image -- long-press silently does nothing, no context menu, no
    # "Save to Photos". WDA's own touchAndHold DOES rotate-correct internally, and this step
    # never touches Instagram, so the "XXTouch only" fingerprint rule doesn't apply here --
    # use it directly instead of Human.hold().
    _, screen_h = p.size()
    visible_h = max(1, min(r["height"], screen_h - r["y"]))
    p.long_press(r["x"] + r["width"] * random.uniform(0.35, 0.65), r["y"] + visible_h * random.uniform(0.25, 0.6),
                 random.uniform(1.0, 1.4))
    h.pause(1.2, 2.0)
    save = p.find("predicate string", "name == 'Save to Photos'")
    if not save:
        raise FlowError("no 'Save to Photos' in Safari's image menu")
    save.click()
    h.pause(1.5, 2.5)
    log(f"imported {url}")


def grant_newest_photos(h: Human, count: int = 1):
    """Instagram only sees photos it was given ('limited access'). From an Instagram gallery
    screen: Manage -> Select more photos -> tick the newest `count` -> Done."""
    manage = h.p.find("predicate string", "name == 'Manage' AND type == 'XCUIElementTypeButton'")
    if not manage:
        return                                            # full access, nothing to do
    h.tap(manage)
    h.pause(1.2, 2.0)
    h.tap(h.p.find("predicate string", "name == 'Select more photos'"))
    h.pause(2.5, 3.5)
    tiles = [e for e in h.p.find_all("predicate string", "type == 'XCUIElementTypeImage' AND (label BEGINSWITH 'Photo,' OR label BEGINSWITH 'Video,')")]
    tiles.sort(key=lambda e: (e.rect()["y"], e.rect()["x"]))   # the grid shows newest first
    for t in tiles[:count]:
        if not t.attr("selected"):
            h.tap(t)
            h.pause(0.5, 1.1)
    h.tap(h.p.find("predicate string", "name == 'Done' AND type == 'XCUIElementTypeButton'"))
    h.pause(2.0, 3.0)


def set_profile_picture(p: Phone, gallery_index: int = 0, grant_newest: bool = True, log=print,
                        label_contains: str | None = None) -> dict:
    """Edit profile -> Edit picture -> Choose from library -> gallery cell `gallery_index`
    (0 = newest Instagram can see) -> Done, then wait for the upload."""
    h = Human(p, log)
    open_edit_profile(h)
    h.tap(p.find("predicate string", "name == 'change-profile-photo-button'"))
    h.pause(1.8, 2.8)
    choose = [e for e in p.by_text("Choose from library") if e.visible()]
    if not choose:
        raise FlowError("'Choose from library' not offered")
    h.tap(choose[0])
    h.pause(2.5, 3.5)
    if grant_newest:
        grant_newest_photos(h, 1)
    if label_contains:
        cell = None
        for _ in range(8):                                # older photos sit further down the grid
            cells = [c for c in p.find_all("predicate string", f"name BEGINSWITH 'gallery-photo-cell-' AND label CONTAINS {json.dumps(label_contains)}")
                     if c.attr("visible")]
            if cells:
                cell = cells[0]
                break
            h.swipe(0.5, 0.88, 0.5, 0.62, 0.35)
            h.pause(0.8, 1.4)
    else:
        # same timing bug as create_story's gallery check (fixed 13.10) -- poll, don't shot once
        cell = _wait_for(p, f"name == 'gallery-photo-cell-{gallery_index}'", 12)
    if not cell:
        raise FlowError(f"gallery cell {label_contains or gallery_index} not there")
    label = cell.attr("label")
    h.tap(cell)
    h.pause(1.2, 2.2)
    h.tap(p.find("predicate string", "name == 'done-button'"))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not p.find("predicate string", "name == 'profile-bio'"):
        time.sleep(2)
    ok = bool(p.find("predicate string", "name == 'profile-bio'"))
    _back(h)
    log(f"profile picture <- {label}: {'ok' if ok else 'upload did not finish'}")
    return {"picked": label, "ok": ok}


# ---------------------------------------------------------------- creating content

def open_creation(h: Human, kind: str):
    """Profile tab '+' -> creation menu -> creation-<kind> (post | reel | story | story-highlight).
    Not the feed's '+': that one reopens whatever mode was used last (camera, which needs a
    camera permission we deliberately denied)."""
    go_tab(h, "profile")
    # 13.10 live bug: right after media_to_photos() (Files/Photos detour, or a cold-ish return
    # from Reels fullscreen) the profile screen can take a beat longer to render than usual --
    # a bare p.find() here fired "element to tap is not on screen" on 2/2 accounts in a row.
    add = _wait_for(h.p, "name == 'profile-add-button'", 8)
    if not add:
        raise FlowError("profile-add-button not on screen after go_tab('profile')")
    h.tap(add)
    h.pause(1.5, 2.5)
    item = _wait_for(h.p, f"name == 'creation-{kind}'", 6)
    if not item:
        raise FlowError(f"creation menu has no {kind}")
    h.tap(item)
    h.pause(2.5, 3.5)


def _wait_for(p: Phone, predicate: str, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        el = p.find("predicate string", predicate)
        if el:
            return el
        time.sleep(1.5)
    return None


def _write_caption(h: Human, caption: str):
    if not caption:
        return
    box = h.p.find("predicate string", "name == 'caption-cell-text-view'")
    h.tap(box)
    h.pause(1.2, 2.0)                                     # opens a separate Caption screen
    human_type(h, caption)
    h.pause(0.8, 1.6)
    ok = h.p.find("predicate string", "name == 'OK' AND type == 'XCUIElementTypeButton'")
    if ok:
        h.tap(ok)
        h.pause(1.2, 2.0)


def create_post(p: Phone, caption: str = "", grant_newest: int = 1, gallery_index: int = 0, log=print) -> dict:
    """Feed post from the newest photo: Post -> pick -> Next (edit) -> Next -> caption -> Share."""
    h = Human(p, log)
    open_creation(h, "post")
    if grant_newest:
        grant_newest_photos(h, grant_newest)
    cell = p.find("predicate string", f"name == 'gallery-photo-cell-{gallery_index}'")
    if not cell:
        raise FlowError("gallery is empty")
    picked = cell.attr("label")
    h.tap(cell)
    h.pause(1.2, 2.2)
    for _ in range(2):                                    # gallery -> edit -> details
        h.tap(p.find("predicate string", "name == 'next-button'"))
        h.pause(2.2, 3.4)
    if not p.find("predicate string", "name == 'share-button'"):
        raise FlowError("share screen did not appear")
    _write_caption(h, caption)
    h.pause(0.8, 2.0)
    h.tap(p.find("predicate string", "name == 'share-button'"))
    done = _wait_for(p, "label BEGINSWITH 'Done posting'", 120)
    log(f"post {picked!r} caption={caption!r}: {'shared' if done else 'no Done posting banner within 120 s'}")
    return {"picked": picked, "shared": bool(done)}


def archive_newest_own_post(p: Phone, handle: str, log=print) -> bool:
    """For tests: archive (not delete -- archive is reversible) the post the feed shows on top
    right after sharing."""
    h = Human(p, log)
    # the feed keeps re-rendering right after a share (stale elements): look up, retry, and anchor on the
    # header's user button, which carries the handle as its label
    for attempt in range(4):
        try:
            own = p.find_all("predicate string", f"name == 'feed-item-header-user-button' AND label == {json.dumps(handle)}")
            if not own:
                time.sleep(2)
                continue
            y = own[0].rect()["y"]
            opts = sorted(p.find_all("predicate string", "name == 'more_options'"), key=lambda e: abs(e.rect()["y"] - y))
            break
        except WDAError:
            time.sleep(2)
    else:
        return False
    h.tap(opts[0])
    h.pause(1.5, 2.5)
    arch = p.find("predicate string", "type == 'XCUIElementTypeCell' AND label == 'Archive'")
    if not arch:
        return False
    h.tap(arch)
    h.pause(1.5, 2.5)
    # reels ask again ("Archive reel?  Archive / Cancel"); posts don't
    confirm = p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Archive'")
    if confirm:
        h.tap(confirm)
        h.pause(1.5, 2.5)
    log("archived the test post")
    return True


# ---------------------------------------------------------------- media in over SSH (no internet needed)

UPLOAD_DIR_NAME = "Farm Upload"


def _phone_ssh(device: str, cmd: str, stdin: bytes | None = None, timeout: int = 120) -> str:
    import subprocess
    from adb_bot.automation.iphone_farm_config import SSH_KEY, lan_ssh_opts
    _, ph = resolve(device)
    dest = f"mobile@{ph['ip']}"
    r = subprocess.run(["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=30", *lan_ssh_opts(dest), dest, cmd],
                       input=stdin, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise FlowError(f"ssh {dest}: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return r.stdout.decode(errors="replace")


def push_media(device: str, local_path: str, name: str | None = None) -> str:
    """Copy a file into Files > On My iPhone > Farm Upload over SSH (via the relay on the LAN).
    Works with the VPN up, unlike a download in Safari."""
    name = name or os.path.basename(local_path)
    # several app groups have a "File Provider Storage" (Photos' and iCloud Drive's too, 2026-10-06);
    # the Files app's "On My iPhone" is the one whose container metadata names LocalStorage
    find = ("for d in /var/mobile/Containers/Shared/AppGroup/*/; do "
            "grep -aqs group.com.apple.FileProvider.LocalStorage \"$d.com.apple.mobile_container_manager.metadata.plist\" "
            "&& echo \"$d\"; done | head -1")
    group = _phone_ssh(device, find).strip()
    if not group:
        raise FlowError("Files' local storage not found on the phone (open the Files app once)")
    target = f"{group}File Provider Storage/{UPLOAD_DIR_NAME}"
    # 13.10 live bug: media_to_photos() only ever deletes the ONE file it used, and only on
    # success -- any failed run (the share sheet step is the flaky one) leaves its upload behind
    # forever. Reproduced live: 10 stale files piled up in Farm Upload and save_to_photos then
    # could not select the new one at all (share sheet never got "Save Image"). Wipe the folder
    # before every push so it's always exactly the one file we're about to use.
    # rm -rf + mkdir, not `rm -f dir/*` -- the phone's shell is zsh, which errors on a glob
    # that matches nothing (an already-empty folder), breaking the very first push every time
    _phone_ssh(device, f"rm -rf '{target}' && mkdir -p '{target}'")
    with open(local_path, "rb") as fh:
        _phone_ssh(device, f"cat > '{target}/{name}'", stdin=fh.read(), timeout=600)
    return name


def save_to_photos(p: Phone, name: str, log=print):
    """Files app -> Farm Upload -> long-press the file -> Share -> Save Image / Save Video."""
    h = Human(p, log)
    p.terminate("com.apple.DocumentsApp")
    p.launch("com.apple.DocumentsApp")
    h.pause(2.0, 3.0)
    stem = os.path.splitext(name)[0]
    pred = f"type == 'XCUIElementTypeCell' AND label BEGINSWITH {json.dumps(stem + ',')}"
    cell = _wait_for(p, pred, 4)
    if not cell:                                          # walk there: Locations > On My iPhone > Farm Upload
        # 12.10 live bug: "Browse" exists twice (the bottom tab AND the top-left back breadcrumb
        # once inside a folder) -- tapping whichever one the old single-predicate search found
        # first could leave us inside iCloud Drive (its last-viewed location) instead of at the
        # root Locations list, so "On My iPhone" was never there to find and the walk silently
        # went nowhere. Back out to the root explicitly (tapping the breadcrumb up to 4x) before
        # looking for "On My iPhone" as its own step.
        for _ in range(4):
            on_iphone = p.find("predicate string", "label == 'On My iPhone' AND type == 'XCUIElementTypeCell'")
            if on_iphone:
                break
            back = p.find("predicate string", "label == 'Browse' AND type == 'XCUIElementTypeButton'")
            if back:
                back.click()
            else:
                tab = p.find("predicate string", "label == 'Browse' AND type == 'XCUIElementTypeCell'")
                if tab:
                    tab.click()
            h.pause(1.0, 1.6)
            on_iphone = p.find("predicate string", "label == 'On My iPhone' AND type == 'XCUIElementTypeCell'")
            if on_iphone:
                break
        if not on_iphone:
            raise FlowError("Files: 'On My iPhone' not found at the Locations root")
        on_iphone.click()
        h.pause(1.2, 2.0)
        folder = _wait_for(p, f"label BEGINSWITH {json.dumps(UPLOAD_DIR_NAME + ',')} AND type == 'XCUIElementTypeCell'", 6)
        if not folder:
            raise FlowError(f"Files: {UPLOAD_DIR_NAME!r} not found under On My iPhone")
        folder.click()
        cell = _wait_for(p, pred, 8)
    if not cell:
        raise FlowError(f"{name} not visible in Files > {UPLOAD_DIR_NAME}")
    # 12.10 live bug: long-pressing the file's own context menu (Get Info / Rename / Compress /
    # Duplicate / Quick Look / New Folder with Item / Tags / Copy / Move) has no Share entry at
    # all on this iOS build -- confirmed live, scrolled the menu to its end, it's just not there.
    # The selection toolbar's Share DOES carry the full system share sheet incl. Save Image, so
    # go through Select instead: top-right "More" -> Select -> tap the file -> toolbar Share.
    more = p.find("predicate string", "label == 'More' AND type == 'XCUIElementTypeButton'")
    if not more:
        raise FlowError("Files: no 'More' button to reach Select")
    more.click()
    h.pause(0.8, 1.3)
    select = _wait_for(p, "label == 'Select' AND type == 'XCUIElementTypeButton'", 4)
    if not select:
        raise FlowError("Files: no 'Select' in the More menu")
    select.click()
    h.pause(0.8, 1.3)
    cell = _wait_for(p, pred, 4)                          # re-find -- selection mode rebuilds the list
    if not cell:
        raise FlowError(f"{name} not visible after entering Select mode")
    cell.click()
    h.pause(0.8, 1.3)
    share = p.find("predicate string", "label == 'Share' AND type == 'XCUIElementTypeButton'")
    if not share:
        raise FlowError("no 'Share' in the selection toolbar")
    share.click()
    save = _wait_for(p, "label == 'Save Video' OR label == 'Save Image'", 15)
    if not save:
        # 13.10 live bug: the share sheet's action row can still be mid-layout (extensions load
        # async, per-container/per-account) -- a swipe across it nudges it to finish laying out
        # and brings any off-screen entry into the tree before giving up for real.
        p.swipe(0.8, 0.85, 0.2, 0.85, 0.3)
        h.pause(0.8, 1.2)
        save = _wait_for(p, "label == 'Save Video' OR label == 'Save Image'", 8)
    if not save:
        raise FlowError("share sheet has no Save Video / Save Image")
    save.click()
    h.pause(2.0, 3.0)
    log(f"saved {name} to Photos")


def media_to_photos(p: Phone, device: str, local_path: str, log=print) -> str:
    name = f"{time.strftime('%m%d%H%M%S')}_{os.path.basename(local_path)}"
    push_media(device, local_path, name)
    save_to_photos(p, name, log)
    # the copy in Files was only a vehicle; ours (timestamped name) goes again
    _phone_ssh(device, "rm -f /var/mobile/Containers/Shared/AppGroup/*/'File Provider Storage'/"
                       f"'{UPLOAD_DIR_NAME}'/{json.dumps(name)}")
    back_to_instagram(p)
    time.sleep(random.uniform(2.0, 3.5))
    return name


# ---------------------------------------------------------------- story / reel

def add_link_sticker(h: Human, url: str, text: str | None = None):
    """Story editor -> Stickers -> Link -> type the URL (and optional sticker text) -> Done, then nudge
    the sticker off-centre so it never sits on exactly the same spot. Verified 2026-10-07: the story
    then shows a "Visit link" button to viewers."""
    p = h.p
    h.tap(_wait_for(p, "name == 'add-stickers-button'", 10))
    h.pause(1.5, 2.5)
    cell = _wait_for(p, "name == 'link-sticker-tray-cell'", 14)
    if not cell:                                          # not in view: search the tray
        search = _wait_for(p, "name == 'search-text-input'", 5)
        if not search:
            # 13.10 live bug: neither showed up at all -- the tray can fail to open on the first
            # tap (sticker button missed, or the editor was still settling from create_story's
            # gallery-pick animation). One retry: close whatever's up, tap Stickers again.
            close = p.find("predicate string", "name == 'Close Sticker Tray'")
            if close:
                close.click()
                h.pause(0.5, 0.9)
            h.tap(_wait_for(p, "name == 'add-stickers-button'", 10))
            h.pause(1.5, 2.5)
            cell = _wait_for(p, "name == 'link-sticker-tray-cell'", 14)
            if not cell:
                search = _wait_for(p, "name == 'search-text-input'", 5)
            if not cell and not search:
                raise FlowError("sticker tray has neither the Link cell nor a search field")
        if not cell:
            h.tap(search)
            human_type(h, "link")
            cell = _wait_for(p, "name == 'link-sticker-tray-cell'", 8)
    if not cell:
        raise FlowError("no Link sticker in the tray (account not allowed links yet?)")
    h.tap(cell)
    h.pause(1.5, 2.5)
    human_type(h, url)                                    # the URL field has focus; the shown example.com is a placeholder
    h.pause(0.6, 1.4)
    if text:
        custom = [e for e in p.by_text("Customize sticker text") if e.visible()]
        if custom:
            h.tap(custom[0])
            h.pause(0.8, 1.4)
            human_type(h, text)
    h.tap(p.find("predicate string", "name == 'Done' AND type == 'XCUIElementTypeButton'"))
    h.pause(1.5, 2.5)
    st = _wait_for(p, "label BEGINSWITH 'Link Sticker'", 8)
    if not st:
        raise FlowError("link sticker did not land on the story")
    r = st.rect()
    cx, cy = r["x"] + r["width"] / 2, r["y"] + r["height"] / 2
    h.drag(cx, cy, cx + random.uniform(-45, 45), cy + random.uniform(-80, 170),
           random.uniform(0.25, 0.45), random.uniform(0.45, 0.8))
    h.pause(1.0, 2.0)


def create_story(p: Phone, grant_newest: int = 1, log=print, link: str | None = None,
                 link_text: str | None = None) -> dict:
    h = Human(p, log)
    open_creation(h, "story")
    if grant_newest:
        grant_newest_photos(h, grant_newest)
    # 12.10 live bug: a single p.find() right here fired "story gallery is empty" on 2/2 accounts
    # straight after media_to_photos() -- confirmed live on nikkiknig that the SAME gallery
    # does have gallery-photo-cell-0 a few seconds later (just-added photo takes a moment to be
    # indexed into the story gallery after Photos access). Poll instead of a single shot.
    cell = _wait_for(p, "name BEGINSWITH 'gallery-photo-cell-0' OR name BEGINSWITH 'gallery-video-cell-0'", 12)
    if not cell:
        raise FlowError("story gallery is empty")
    picked = cell.attr("label")
    h.tap(cell)
    h.pause(2.5, 3.5)
    for _ in range(3):                                    # one-time Instagram explainers on top of the editor
        ok = p.find("predicate string", "(name == 'OK' OR name == 'Continue') AND type == 'XCUIElementTypeButton'")
        if not ok or p.find("predicate string", "name == 'share-to-your-story'"):
            break
        h.tap(ok)
        h.pause(1.2, 2.0)
    share = _wait_for(p, "name == 'share-to-your-story'", 15)
    if not share:
        raise FlowError("story editor did not open")
    if link:
        add_link_sticker(h, link, link_text)
        share = _wait_for(p, "name == 'share-to-your-story'", 10)
    h.pause(1.0, 2.5)
    h.tap(share)
    back = _wait_for(p, "name == 'mainfeed-tab' OR name == 'profile-tab'", 60)
    for _ in range(2):                                    # "Stories archive" notice after the first story
        ok = p.find("predicate string", "label == 'OK' AND type == 'XCUIElementTypeButton'")
        if ok:
            h.tap(ok)
            h.pause(1.0, 1.8)
    ring = p.find("predicate string", "name == 'story-tray-cell-self'")
    log(f"story {picked!r}{' link=' + link if link else ''}: {'shared' if back else 'did not return to the app'}")
    return {"picked": picked, "shared": bool(back), "link": link}


def delete_first_highlight(p: Phone, log=print) -> bool:
    """Long-press the account's first (leftmost) highlight on its own profile -> Delete highlight
    -> confirm. Used 13.10 to remove highlights posted with a wrong caption before reposting.
    Returns False (no-op) if the account has no highlights at all."""
    h = Human(p, log)
    go_tab(h, "profile")
    cell = _wait_for(p, "name BEGINSWITH 'profile-tray-cell-' OR name BEGINSWITH 'highlight-tray-cell-'", 4)
    if not cell:
        log("no highlight to delete")
        return False
    r = cell.rect()
    p.long_press(r["x"] + r["width"] * 0.5, r["y"] + r["height"] * 0.4, 1.0)
    h.pause(0.8, 1.2)
    delete = _wait_for(p, "label == 'Delete highlight'", 5)
    if not delete:
        raise FlowError("no 'Delete highlight' in the long-press menu")
    delete.click()
    h.pause(0.8, 1.2)
    confirm = _wait_for(p, "label == 'Delete' AND type == 'XCUIElementTypeButton'", 5)
    if not confirm:
        raise FlowError("no confirm 'Delete' button")
    confirm.click()
    h.pause(1.5, 2.0)
    log("highlight deleted")
    return True


def save_story_to_highlight(p: Phone, name: str, log=print) -> dict:
    """Saves the account's current story (call right after create_story()) into a brand-new
    highlight named `name`. Must be on the account's own profile already.

    The "New" cell in the Add-to-highlights tray has no element of its own -- the whole tray
    is one VoiceOver-collapsed `Adjustable` group (`name == 'Add to highlights tray'`, value
    "New, 1 of N"), so it's tapped by fraction of the tray's own rect instead of a found
    element; New is always the first/leftmost cell regardless of how many highlights already
    exist. Verified live 2026-10-12 on B1/nikkisadventure_ (account had 0 prior highlights)."""
    h = Human(p, log)
    # 13.10 live bug: create_story() can return to either mainfeed-tab or profile-tab (its own
    # _wait_for accepts both) -- if it lands on mainfeed, user-detail-header-profile-picture
    # doesn't exist there at all and this misread as "story already expired". Force profile.
    go_tab(h, "profile")
    pic = _wait_for(p, "name == 'user-detail-header-profile-picture'", 10)
    if not pic:
        raise FlowError("not on own profile -- can't open own story")
    # 13.10 live bug, reproduced repeatedly: accounts with a "Start your first note..." /
    # "Make this space yours..." prompt bubble have it sitting over the TOP of the avatar
    # circle -- Human.tap()'s 0.3-0.7 jitter lands in that top band often enough to open the
    # note composer instead of the story. Bias the tap toward the bottom of the element.
    r = pic.rect()
    h.tap_xy(r["x"] + r["width"] * random.uniform(0.35, 0.65), r["y"] + r["height"] * random.uniform(0.65, 0.9))
    h.pause(0.5, 0.8)
    hi = _wait_for(p, "name == 'story-self-highlight-button'", 8)
    if not hi:
        raise FlowError("story did not open (already expired?)")
    h.tap(hi)
    h.pause(1.3, 1.6)                                    # sheet animates in -- tapping early hits the story underneath
    tray = _wait_for(p, "name == 'Add to highlights tray'", 5)
    if not tray:
        raise FlowError("add-to-highlights sheet did not open")
    r = tray.rect()
    h.tap_xy(r["x"] + r["width"] * 0.155, r["y"] + r["height"] * 0.39)   # the "New" cell
    field = _wait_for(p, "name == 'ig-text-field'", 6)
    if not field:
        raise FlowError("new-highlight name field did not open")
    h.tap(field)
    h.pause(0.3, 0.5)
    field.clear()
    h.pause(0.2, 0.4)
    field.type(name)
    h.pause(0.4, 0.7)
    add = p.find("predicate string", "label == 'Add' AND type == 'XCUIElementTypeButton'")
    if not add:
        raise FlowError("Add button not found")
    h.tap(add)
    done = _wait_for(p, "label == 'Done' AND type == 'XCUIElementTypeButton'", 6)
    if done:
        h.tap(done)
    # 13.10 live bug: this is a pure cosmetic cleanup tap (closes the story viewer) after the
    # highlight is already saved -- a stale-element race here (the "Done" tap above can itself
    # dismiss the story) used to raise and mask an otherwise-successful save. Never let it fail
    # the result.
    try:
        x = p.find("predicate string", "name == 'story-dismiss-button'")
        if x:
            h.tap(x)
    except WDAError:
        pass
    log(f"highlight {name!r}: {'saved' if done else 'Add tapped but no confirmation seen'}")
    return {"name": name, "ok": bool(done)}


def create_reel(p: Phone, caption: str = "", grant_newest: int = 1, log=print) -> dict:
    h = Human(p, log)
    open_creation(h, "reel")
    if grant_newest:
        grant_newest_photos(h, grant_newest)
    cell = _wait_for(p, "name == 'gallery-video-cell-0' OR name BEGINSWITH 'gallery-video-cell'", 8)
    if not cell:
        raise FlowError("no video in the reel gallery")
    picked = cell.attr("label")
    h.tap(cell)
    h.pause(1.5, 2.5)
    nxt = _wait_for(p, "name == 'reels-gallery-selection-next'", 10)
    h.tap(nxt)
    nxt = _wait_for(p, "name == 'sundial-right-chevron-next-button'", 30)   # editor (trims/loads the clip)
    if not nxt:
        raise FlowError("reel editor did not open")
    h.pause(1.5, 3.0)
    h.tap(nxt)
    h.pause(2.0, 3.0)
    for _ in range(2):                                    # "Others can now download your reels" (first reel)
        cont = p.find("predicate string", "name == 'Continue' AND type == 'XCUIElementTypeButton'")
        if not cont:
            break
        h.tap(cont)
        h.pause(1.2, 2.0)
    if not _wait_for(p, "name == 'share-sheet-share-button'", 15):
        raise FlowError("reel details screen did not open")
    _write_caption(h, caption)
    h.pause(0.8, 2.0)
    h.tap(p.find("predicate string", "name == 'share-sheet-share-button'"))
    h.pause(2.0, 3.0)
    share = p.find("predicate string", "name == 'Share' AND type == 'XCUIElementTypeButton'")   # "About Reels" sheet
    if share:
        h.tap(share)
    done = _wait_for(p, "label BEGINSWITH 'Done posting'", 180)
    log(f"reel {picked!r} caption={caption!r}: {'shared' if done else 'no Done posting banner within 180 s'}")
    return {"picked": picked, "shared": bool(done)}


# ---------------------------------------------------------------- self test (test account only)

MEDIA_DIR = os.path.expanduser("~/.adb_bot/iphone_ig_testmedia")


SELFTEST_STEPS = ("warmup", "bio", "pfp", "post", "story", "reel")


def selftest(device: str, container: str, handle: str, rounds: int = 1, log=print,
             steps: tuple = ("post",)) -> list[dict]:
    """Run every flow `rounds` times on ONE test account and leave it as it was: test posts and
    reels are archived (reversible), bio and profile picture restored; stories expire after 24 h."""
    results = []
    p = open_account(device, container, handle, log)
    media = {k: os.path.join(MEDIA_DIR, v) for k, v in
             {"post": "p1.jpg", "story": "story.jpg", "pfp": "pfp.jpg", "reel": "reel.mp4"}.items()}

    def step(name, fn):
        t = time.monotonic()
        try:
            res = fn()
            ok = bool(res.get("ok", res.get("shared", True))) if isinstance(res, dict) else bool(res)
        except Exception as exc:                          # one broken step must not hide the others
            res, ok = {"error": str(exc)[:300]}, False
            try:
                back_to_instagram(p)
                go_tab(Human(p, log), "mainfeed")
            except Exception:
                pass
        results.append({"round": r, "step": name, "ok": ok, "seconds": round(time.monotonic() - t), **(res if isinstance(res, dict) else {})})
        log(f"[round {r}] {name}: {'OK' if ok else 'FAIL'} {res}")
        Human.pause(4, 9)

    # Rene 2026-10-07: never start with a warm-up, never run everything by default -- only the named
    # step(s), once. Warm-up follows/edits/posts are real actions on a real account.
    for r in range(1, rounds + 1):
        if "warmup" in steps:
            step("warmup-2min", lambda: warmup(p, 2, 1, log))
        bio_before = {}

        def bio_cycle():
            original = get_bio(p, log)
            try:
                res = set_bio(p, "sunset chaser 🌅\nbot test", log)
            finally:                                      # whatever happened, the real bio goes back
                back = set_bio(p, original, log)
            return {"ok": res["ok"] and back["ok"], "restored": back["after"]}
        if "bio" in steps:
            step("bio (set + restore)", bio_cycle)

        def pfp_cycle():
            media_to_photos(p, device, media["pfp"], log)
            try:
                a = set_profile_picture(p, 0, grant_newest=True, log=log)
            finally:                                      # the account's own picture (from Oct 5) goes back
                b = set_profile_picture(p, grant_newest=False, log=log, label_contains="OCTOBER 5")
            return {"ok": a["ok"] and b["ok"], "test": a["picked"], "restored": b["picked"]}
        if "pfp" in steps:
            step("profile picture (set + restore)", pfp_cycle)

        def post_cycle():
            media_to_photos(p, device, media["post"], log)
            res = create_post(p, "golden hour ✨", log=log)
            res["archived"] = archive_newest_own_post(p, handle, log) if res["shared"] else False
            return res
        if "post" in steps:
            step("post (+ archive)", post_cycle)

        def story_cycle():
            media_to_photos(p, device, media["story"], log)
            return create_story(p, log=log, link="https://example.com/test")
        if "story" in steps:
            step("story (+ link sticker)", story_cycle)

        def reel_cycle():
            media_to_photos(p, device, media["reel"], log)
            res = create_reel(p, "sunset mood 🌅", log=log)
            res["archived"] = archive_newest_own_post(p, handle, log) if res["shared"] else False
            return res
        if "reel" in steps:
            step("reel (+ archive)", reel_cycle)
    p.close()
    return results


# ---------------------------------------------------------------- CLI

def _logger(device: str, flow: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{device}-{flow}.log")
    f = open(path, "a", encoding="utf-8")

    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        f.write(line + "\n")
        f.flush()
    return log, path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("whoami")
    s.add_argument("device")
    s = sub.add_parser("warmup")
    s.add_argument("device")
    s.add_argument("container")
    s.add_argument("--minutes", type=float, default=None, help="default: random 8-10")
    s.add_argument("--follows", type=int, default=None, help="default: random 3-6")
    s.add_argument("--expect", help="handle the container must hold")
    s.add_argument("--no-open", action="store_true", help="Instagram is already open via the Shortcut")
    s = sub.add_parser("post")
    s.add_argument("device")
    s.add_argument("container")
    s.add_argument("--cell", type=int, default=0, help="gallery position, 0 = newest")
    s.add_argument("--caption", default="")
    s.add_argument("--expect", help="handle the container must hold")
    s.add_argument("--no-open", action="store_true", help="Instagram is already open via the Shortcut")
    s = sub.add_parser("picture")
    s.add_argument("device")
    s.add_argument("container")
    s.add_argument("--cell", type=int, default=0, help="gallery position, 0 = newest")
    s.add_argument("--expect", help="handle the container must hold")
    s.add_argument("--no-open", action="store_true", help="Instagram is already open via the Shortcut")
    s = sub.add_parser("pfp", help="set profile picture: --drive-id imports a new one first")
    s.add_argument("device")
    s.add_argument("container")
    s.add_argument("--drive-id")
    s.add_argument("--gallery-index", type=int, default=0)
    s.add_argument("--expect")
    s.add_argument("--no-open", action="store_true")
    s = sub.add_parser("selftest", help="every flow N times on ONE test account, leaving it as it was")
    s.add_argument("device")
    s.add_argument("container")
    s.add_argument("handle")
    s.add_argument("--rounds", type=int, default=1)
    s.add_argument("--steps", default="post", help=f"comma list of {','.join(SELFTEST_STEPS)} (default: post only)")
    s = sub.add_parser("bio")
    s.add_argument("device")
    s.add_argument("container")
    s.add_argument("text", help="new bio, \\n for a line break")
    s.add_argument("--expect")
    s.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "whoami":
        with connect(a.device) as p:
            print(current_handle(p))
        return 0
    log, path = _logger(a.device, a.cmd)
    if a.cmd == "warmup":
        p = connect(a.device, timeout=60) if a.no_open else open_account(a.device, a.container, a.expect, log)
        with p:
            stats = warmup(p, a.minutes, a.follows, log)
        log("done " + json.dumps(stats))
    elif a.cmd == "selftest":
        steps = tuple(x.strip() for x in a.steps.split(",") if x.strip())
        bad = [x for x in steps if x not in SELFTEST_STEPS]
        if bad:
            ap.error(f"unknown steps {bad}; choose from {SELFTEST_STEPS}")
        res = selftest(a.device, a.container, a.handle, a.rounds, log, steps)
        ok = sum(1 for x in res if x["ok"])
        log(f"SELFTEST {ok}/{len(res)} steps OK")
        for x in res:
            log(f"  round {x['round']} {x['step']:<32} {'OK  ' if x['ok'] else 'FAIL'} {x['seconds']:>4}s {x.get('error', '')}")
    elif a.cmd == "pfp":
        p = connect(a.device, timeout=60) if a.no_open else open_account(a.device, a.container, a.expect, log)
        with p:
            if a.drive_id:
                import_photo(p, drive_download_url(a.drive_id), log)
                back_to_instagram(p)
                time.sleep(2)
            res = set_profile_picture(p, a.gallery_index, grant_newest=bool(a.drive_id), log=log)
        log("done " + json.dumps(res))
    elif a.cmd == "bio":
        p = connect(a.device, timeout=60) if a.no_open else open_account(a.device, a.container, a.expect, log)
        with p:
            res = set_bio(p, a.text.replace("\\n", "\n"), log)
        log("done " + json.dumps(res, ensure_ascii=False))
    elif a.cmd == "post":
        p = connect(a.device, timeout=60) if a.no_open else open_account(a.device, a.container, a.expect, log)
        with p:
            res = post_photo(p, a.cell, a.caption, log)
        log("done " + json.dumps(res, ensure_ascii=False))
    elif a.cmd == "picture":
        p = connect(a.device, timeout=60) if a.no_open else open_account(a.device, a.container, a.expect, log)
        with p:
            res = set_profile_picture(p, a.cell, log)
        log("done " + json.dumps(res))
    print("log:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
