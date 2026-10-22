"""Touch input for the farm iPhones through XXTouch Elite, with a real finger's pressure.

Rene, 2026-10-08: "wir müssen überall alles mit xxtouch machen, nicht das normale, auf gar
keinen Fall". WDA taps have a constant force 0.150 and radius 21.92 (measured 10-06); XXTouch's
touch.move(id, x, y, pressure) gives real, varying force. So:
  * WDA only READS the screen (element tree, where a button is),
  * every touch -- tap, swipe, keyboard key -- is played by XXTouch from a library of gestures
    cut out of a recording of Rene's own thumb (~/.adb_bot/xxtouch/rec_*.lua, 2026-10-07, 111
    gestures, pressure 183-1850). Each tap replays a random recorded tap's pressure curve and
    timing at the new spot; each swipe bends a recorded swipe's path (wobble, speed, pressure)
    onto the new start/end. Pressure is scaled +-15 % and timing +-12 % per touch so no two
    phones -- or two taps -- repeat a recording exactly.

XXTouch's HTTP API (port 46952 on the phone) answers 403 from the LAN because remote access is
OFF on purpose; we reach 127.0.0.1:46952 through an SSH tunnel to the phone (LAN ip through the
relay Mac, or USB for phones without Wi-Fi). POST /spawn runs a Lua script from the body; only one
script at a time (/is_running: code 3 = busy, 0 = idle). ~0.45 s per spawn, so a gesture is one
script, never one script per touch event.

Coordinates in: iOS points (what WDA reports). XXTouch wants pixels: points x scale (3 on the
iPhone X farm phones).
"""

from __future__ import annotations

import atexit
import glob
import json
import math
import os
import random
import re
import socket
import subprocess
import threading
import time

import requests

from adb_bot.automation import iphone_farm_config

REC_GLOB = os.path.expanduser("~/.adb_bot/xxtouch/rec_*.lua")
API_PORT = 46952
_tunnels: dict[str, tuple[subprocess.Popen, int]] = {}
_lock = threading.Lock()

# 11.10: all farm phones are this one iPhone X-family size in pixels (scale 3 x the 375x812 WDA
# points). A recorded gesture's own wobble is added on top of the target coordinate with no bounds
# check, so a tap/swipe aimed near a screen edge can push a touch.move a few px past it -- XXTouch's
# runtime does not clamp either, it throws and aborts the whole script ("Coordinate (x, y) exceeds
# coordinate space (1125, 2436)"). Confirmed live 11.10, iPhone D: identical crash on 4 separate
# warmup runs in a row, each one burning a full 240 s open_account timeout behind it. Clamping here
# is the fix -- 1px margin so a touch.off at the very edge cannot round back out of bounds.
SCREEN_PX = (1125, 2436)


def _clamp_xy(x: float, y: float, screen: tuple = SCREEN_PX) -> tuple[float, float]:
    return (min(max(x, 1), screen[0] - 1), min(max(y, 1), screen[1] - 1))


class XXError(RuntimeError):
    pass


# ---------------------------------------------------------------- gesture library

_RA = re.compile(r"RA\((\d+),touch\.(on|move|off),(\d+),(-?\d+),(-?\d+)(?:,(\d+))?(?:,(\d+))?\)")


def _load_library() -> tuple[list, list]:
    """Recorded gestures -> (taps, swipes). Every gesture is a list of (t_ms, x_px, y_px, pressure),
    t relative to its touch-down."""
    taps, swipes = [], []
    for path in sorted(glob.glob(REC_GLOB)):
        cur = None
        for m in _RA.finditer(open(path, encoding="utf-8").read()):
            t, op, _id, x, y, pr, _ang = m.groups()
            t, x, y = int(t), int(x), int(y)
            if op == "on":
                cur = {"t0": t, "pts": [(0, x, y, 0)]}
            elif cur is not None:
                cur["pts"].append((t - cur["t0"], x, y, int(pr or 0)))
                if op == "off":
                    pts = cur["pts"]
                    dist = math.hypot(pts[-1][1] - pts[0][1], pts[-1][2] - pts[0][2])
                    (swipes if dist > 40 else taps).append(pts)
                    cur = None
    return taps, swipes


_TAPS, _SWIPES = _load_library()
BY_PHONE = os.path.expanduser("~/.adb_bot/xxtouch/by_phone")
_LIB_CACHE: dict[str, tuple[list, list]] = {}
# Rene's finger taps mostly read force 0..0.01; recorded pressures replayed 1:1 came out 0.12-0.29 (touch
# test page, 2026-10-09). Scale them down, but not to a constant.
PRESSURE = (0.05, 0.55)


def library_for(ref: str) -> tuple[list, list]:
    """The phone's OWN recordings (by_phone/<A-E>, stream iphoneN -> N-th letter); the shared pool when a
    phone has none."""
    letter = ref[-1].upper() if len(ref) == 1 else None
    m = re.search(r"(\d+)$", ref or "")
    if m and not letter:
        letter = "ABCDEFGHIJ"[int(m.group(1)) - 1]
    if letter not in _LIB_CACHE:
        global REC_GLOB
        saved, REC_GLOB = REC_GLOB, os.path.join(BY_PHONE, letter or "_", "rec_*.lua")
        try:
            lib = _load_library()
        finally:
            REC_GLOB = saved
        _LIB_CACHE[letter] = lib if lib[0] and lib[1] else (_TAPS, _SWIPES)
    return _LIB_CACHE[letter]


def library_size() -> tuple[int, int]:
    return len(_TAPS), len(_SWIPES)


def _tap_lua(x: float, y: float, taps: list | None = None) -> str:
    """One tap at pixel (x, y) with a recorded thumb's pressure curve, slightly wandering like a
    real finger pad does."""
    taps = taps or _TAPS
    if not taps:
        raise XXError(f"no recorded taps in {REC_GLOB}")
    rec = random.choice(taps)
    pk = random.uniform(*PRESSURE)
    tk = random.uniform(0.88, 1.12)
    ox, oy = rec[0][1], rec[0][2]
    x, y = _clamp_xy(x, y)
    lines = [f"touch.on(7,{round(x)},{round(y)})"]
    prev = 0
    for t, rx, ry, pr in rec[1:]:
        t = int(t * tk)
        lines.append(f"sys.msleep({max(1, t - prev)})")
        prev = t
        dx, dy = (rx - ox) + random.gauss(0, 0.6), (ry - oy) + random.gauss(0, 0.6)
        px, py = _clamp_xy(x + dx, y + dy)
        lines.append(f"touch.move(7,{round(px)},{round(py)},{max(1, int(pr * pk)) if pr else 1},50)")
    lines[-1] = re.sub(r"touch\.move\(7,(-?\d+),(-?\d+),\d+,50\)", r"touch.off(7,\1,\2)", lines[-1])
    return "\n".join(lines)


def _swipe_lua(x1: float, y1: float, x2: float, y2: float, seconds: float | None = None, swipes: list | None = None) -> str:
    """A recorded swipe, rotated/scaled so it runs from (x1,y1) to (x2,y2) in pixels; keeps the
    recording's wobble, acceleration and pressure."""
    swipes = swipes or _SWIPES
    if not swipes:
        raise XXError(f"no recorded swipes in {REC_GLOB}")
    want = math.atan2(y2 - y1, x2 - x1)

    def angle_gap(rec):
        a = math.atan2(rec[-1][2] - rec[0][2], rec[-1][1] - rec[0][1])
        return abs((a - want + math.pi) % (2 * math.pi) - math.pi)
    pool = sorted(swipes, key=angle_gap)[:max(3, len(swipes) // 3)]
    rec = random.choice(pool)
    sx, sy = rec[0][1], rec[0][2]
    vx, vy = rec[-1][1] - sx, rec[-1][2] - sy
    wx, wy = x2 - x1, y2 - y1
    vlen = math.hypot(vx, vy) or 1
    scale = math.hypot(wx, wy) / vlen
    rot = math.atan2(wy, wx) - math.atan2(vy, vx)
    cr, sr = math.cos(rot), math.sin(rot)
    dur = rec[-1][0] or 1
    tk = (seconds * 1000 / dur) if seconds else random.uniform(0.88, 1.12)
    pk = random.uniform(*PRESSURE)
    x1, y1 = _clamp_xy(x1, y1)
    x2, y2 = _clamp_xy(x2, y2)
    lines = [f"touch.on(7,{round(x1)},{round(y1)})"]
    prev = 0
    pts = rec[1:]
    for i, (t, rx, ry, pr) in enumerate(pts):
        t = int(t * tk)
        lines.append(f"sys.msleep({max(1, t - prev)})")
        prev = t
        dx, dy = (rx - sx) * scale, (ry - sy) * scale
        px, py = _clamp_xy(x1 + dx * cr - dy * sr, y1 + dx * sr + dy * cr)
        if i == len(pts) - 1:
            lines.append(f"touch.off(7,{round(px)},{round(py)})")
        else:
            lines.append(f"touch.move(7,{round(px)},{round(py)},{max(1, int(pr * pk)) if pr else 1},50)")
    return "\n".join(lines)


def clean_swipes(swipes: list, up: bool = True) -> list:
    """Recordings that are real finger flicks: >= 5 samples, 60 ms-2.5 s, and (up=True) going upward.
    22.10: the libraries hold 2-sample 0-7 ms 'swipes' (touch on/off that drifted) -- replayed those are
    teleports, not flicks."""
    out = []
    for s in swipes:
        if len(s) < 5 or not 60 <= s[-1][0] <= 2500:
            continue
        if up and not (s[0][2] - s[-1][2] > 150 and abs(s[-1][1] - s[0][1]) < s[0][2] - s[-1][2]):
            continue
        out.append(s)
    return out


def _flick_lua(rec: list, jitter_px: float = 60.0) -> str:
    """One recorded swipe replayed as recorded: same start area (+- jitter), same length, same wobble and
    pressure, tempo 0.9-1.1x. This is Rene's own thumb -- the warm-up's reel scrolling uses this (22.10,
    Rene: the rescaled version felt 'stumpf')."""
    sx, sy = rec[0][1], rec[0][2]
    ox, oy = random.gauss(0, jitter_px / 2), random.gauss(0, jitter_px / 2)
    tk, pk = random.uniform(0.9, 1.1), random.uniform(*PRESSURE)
    x0, y0 = _clamp_xy(sx + ox, sy + oy)
    lines = [f"touch.on(7,{round(x0)},{round(y0)})"]
    prev = 0
    pts = rec[1:]
    for i, (t, rx, ry, pr) in enumerate(pts):
        t = int(t * tk)
        lines.append(f"sys.msleep({max(1, t - prev)})")
        prev = t
        px, py = _clamp_xy(rx + ox + random.gauss(0, 0.5), ry + oy + random.gauss(0, 0.5))
        if i == len(pts) - 1:
            lines.append(f"touch.off(7,{round(px)},{round(py)})")
        else:
            lines.append(f"touch.move(7,{round(px)},{round(py)},{max(1, int(pr * pk)) if pr else 1},50)")
    return "\n".join(lines)


# ---------------------------------------------------------------- transport

def _ssh_dest(ref: str) -> str:
    from adb_bot.automation.iphone_farm_wda import resolve
    _rec, p = resolve(ref)
    return p.get("ssh") or f"mobile@{p['ip']}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _die_with_parent():
    """14.10: every run that was stopped (kill/TERM, so atexit never ran) left its ssh -N tunnels behind -- 37 of
    them by morning, the Mac jump host's sshd then refused every new connection ("Connection reset"), which read
    as a slow WLAN all evening. The kernel now sends the tunnel SIGTERM when the Python process that opened it ends."""
    import ctypes, signal as _signal
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, _signal.SIGTERM)    # PR_SET_PDEATHSIG


def _tunnel(ref: str) -> int:
    with _lock:
        t = _tunnels.get(ref)
        if t and t[0].poll() is None:
            return t[1]
        dest = _ssh_dest(ref)
        port = _free_port()
        proc = subprocess.Popen(
            ["ssh", "-N", "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
             *iphone_farm_config.lan_ssh_opts(dest), "-L", f"127.0.0.1:{port}:127.0.0.1:{API_PORT}", dest],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=_die_with_parent)
        for _ in range(150):                     # 13.10: slow farm WLAN -- SSH via the Mac took 10-12 s to connect
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise XXError(f"{ref}: SSH tunnel to XXTouch failed (phone off or jailbreak gone?)")
                time.sleep(0.2)
        _tunnels[ref] = (proc, port)
        return port


def _drop_tunnel(ref: str) -> None:
    with _lock:
        t = _tunnels.pop(ref, None)
    if t and t[0].poll() is None:
        t[0].terminate()
        try:
            t[0].wait(3)
        except Exception:                                   # noqa: BLE001
            t[0].kill()


@atexit.register
def _close_tunnels():
    for proc, _ in _tunnels.values():
        proc.terminate()


class Touch:
    """XXTouch on one phone. `scale` = pixels per WDA point."""

    def __init__(self, ref: str, scale: float = 3.0):
        self.ref, self.scale = ref, scale
        # 13.10 (Rene): the farm mixes iPhone X (1125x2436, scale 3) with iPhone 7/8 (750x1334, scale 2).
        # Flows work in WDA points and divide OCR pixels by the scale, so a phone's own scale/screen size
        # from /etc/adbbot/iphone_farm.json (model, scale, screen_px) is all that differs between models.
        from adb_bot.automation.iphone_farm_wda import resolve
        _rec, phone = resolve(ref)
        self.scale = float(phone.get("scale") or scale)
        self.screen_px = tuple(phone.get("screen_px") or SCREEN_PX)
        self.taps, self.swipes = library_for(ref)
        self.base = f"http://127.0.0.1:{_tunnel(ref)}"
        self.http = requests.Session()

    def _post(self, path: str, data: bytes | None = None) -> dict:
        try:
            r = self.http.post(self.base + path, data=data, timeout=15)
        except requests.RequestException:
            # tunnel died -> reopen once. 22.10 (Rene: "wenn der Proxy an geht, bricht XXTouch ab"): when the
            # phone toggles its VPN, iOS resets the phone-side sockets while our ssh -N stays alive -- _tunnel()
            # would hand back the same dead forward. Drop the tunnel first so it is really rebuilt.
            _drop_tunnel(self.ref)
            self.base = f"http://127.0.0.1:{_tunnel(self.ref)}"
            self.http = requests.Session()
            r = self.http.post(self.base + path, data=data, timeout=15)
        try:
            return r.json()
        except ValueError:
            raise XXError(f"{self.ref}: XXTouch answered {r.status_code}: {r.text[:200]}") from None

    def wait_idle(self, timeout: float = 90):             # a loaded phone (IG uploading) needs >30 s for one OCR
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._post("/is_running").get("code") == 0:
                return
            time.sleep(0.05)
        raise XXError(f"{self.ref}: XXTouch script still running after {timeout:.0f} s")

    def _heal_licence(self) -> bool:
        """22.10 (Rene: "manchmal sagt XXTouch, ich habe keine Lizenz"): ngserviced forgets the licence after a
        respring although XXTExplorer still has it cached (deadline 06.11.2026 on B). Opening the app once hands
        it back -- the signup has done this since 09.10, the posting batch never did. Once per Touch instance."""
        if getattr(self, "_lic_healed", False):
            return False
        self._lic_healed = True
        dest = _ssh_dest(self.ref)
        # 22.10 (A, C after the Havoc login): opening the app alone did not bring the licence back, but a restart of
        # the XXTouch daemons did (ngserviced re-reads XXTExplorer's cached licence on start). Do both.
        cmd = ("uiopen --bundleid ch.xxtou.XXTExplorer; sleep 8; uiopen --bundleid com.burbn.instagram; sleep 2; "
               "printf '123\\n' | /var/jb/usr/bin/sudo -S -p '' /var/jb/usr/bin/killall -9 ngserviced nguiserviced 2>/dev/null; sleep 6")
        try:
            subprocess.run(["ssh", "-n", "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                            "-o", "ConnectTimeout=20", *iphone_farm_config.lan_ssh_opts(dest), dest, cmd],
                           capture_output=True, timeout=90)
        except Exception:                                   # noqa: BLE001
            return False
        _drop_tunnel(self.ref)                              # the daemon restart drops the API connection
        self.base = f"http://127.0.0.1:{_tunnel(self.ref)}"
        self.http = requests.Session()
        time.sleep(2)
        return True

    def run(self, lua: str, wait: bool = True):
        """Run one Lua script and (by default) wait until it finished."""
        for attempt in range(4):
            self.wait_idle()
            res = self._post("/spawn", ("touch.init(0)\n" + lua).encode())
            if res.get("code") == 0:
                break
            if res.get("code") == 403 and self._heal_licence():      # "requires a valid license" -> re-sync once
                continue
            if res.get("code") != 3 or attempt == 3:      # 3 = another script still running: wait, try again
                raise XXError(f"{self.ref}: XXTouch refused the script: {res}")
            time.sleep(1.5)
        if wait:
            time.sleep(0.05)
            self.wait_idle()

    # -- gestures, coordinates in WDA points
    def tap(self, x: float, y: float):
        self.run(_tap_lua(x * self.scale, y * self.scale, self.taps))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, seconds: float | None = None):
        s = self.scale
        self.run(_swipe_lua(x1 * s, y1 * s, x2 * s, y2 * s, seconds, self.swipes))

    def flick(self, up: bool = True) -> dict:
        """A recorded flick replayed 1:1 (see _flick_lua). Returns what was replayed (ms, px) for the log."""
        pool = clean_swipes(self.swipes, up=up)
        if not pool:
            raise XXError(f"{self.ref}: no clean recorded {'upward' if up else ''} flicks in the library")
        rec = random.choice(pool)
        self.run(_flick_lua(rec))
        return {"ms": rec[-1][0], "px": round(rec[0][2] - rec[-1][2]), "samples": len(rec)}

    def drag(self, x1: float, y1: float, x2: float, y2: float, hold: float = 0.35, seconds: float = 0.6):
        """Press, hold, then move slowly to (x2,y2) and lift -- dragging a sticker or a slider."""
        s = self.scale
        x1, y1, x2, y2 = x1 * s, y1 * s, x2 * s, y2 * s
        x1, y1 = _clamp_xy(x1, y1, self.screen_px)
        x2, y2 = _clamp_xy(x2, y2, self.screen_px)
        pr = int(random.uniform(400, 900))
        lines = [f"touch.on(7,{round(x1)},{round(y1)})", f"sys.msleep({int(hold * 1000)})"]
        n = max(6, int(seconds / 0.04))
        for i in range(1, n + 1):
            t = i / n
            e = t * t * (3 - 2 * t)
            px, py = _clamp_xy(x1 + (x2 - x1) * e + random.gauss(0, 0.7), y1 + (y2 - y1) * e + random.gauss(0, 0.7))
            lines += ["sys.msleep(40)", f"touch.move(7,{round(px)},{round(py)},{pr},50)"]
        lines += [f"sys.msleep({random.randint(100, 250)})", f"touch.off(7,{round(x2)},{round(y2)})"]
        self.run("\n".join(lines))

    def hold(self, x: float, y: float, seconds: float = 0.8):
        """Long press: touch down, a real finger's pressure while holding, lift."""
        s = self.scale
        x, y = _clamp_xy(x * s, y * s, self.screen_px)
        n = max(4, int(seconds / 0.05))
        lines = [f"touch.on(7,{round(x)},{round(y)})"]
        base = random.uniform(700, 1300)
        for i in range(n):
            lines.append("sys.msleep(50)")
            pr = base * (0.5 + 0.5 * math.sin(min(1, i / 4) * math.pi / 2)) + random.gauss(0, 60)
            px, py = _clamp_xy(x + random.gauss(0, 0.8), y + random.gauss(0, 0.8))
            lines.append(f"touch.move(7,{round(px)},{round(py)},{max(1, int(pr))},50)")
        lines.append(f"touch.off(7,{round(x)},{round(y)})")
        self.run("\n".join(lines))

    def set_clipboard(self, text: str):
        self.run(f"pasteboard.write({json.dumps(text)})")


_touch_cache: dict[str, Touch] = {}


def for_phone(ref: str) -> Touch:
    t = _touch_cache.get(ref)
    if t is None:
        t = _touch_cache[ref] = Touch(ref)
    return t
