"""Minimal VNC (RFB 3.8) client for the physical iPhone farm: send taps and
swipes to a phone's CatVNC, and grab a screenshot when needed.

Why not just noVNC: CatVNC only speaks Raw encoding (a full iPhone 2 frame is
1.75 MB, ~1.6 s from Thailand through this server), so anything that waits on
framebuffer updates is slow. Input alone is a few bytes per event. Coordinates
are fractions of the screen (0..1) and are scaled to the phone's framebuffer,
which is what CatVNC maps touches against (562x1218 on iPhone 1, 450x975 on
iPhone 2 -- different CatVNC scale settings, same iPhone X screen).

The `start_mirroring` routine drives the Control Center by position, so it is
iPhone X / iOS 16 specific (positions measured 2026-10-03).
"""

from __future__ import annotations

import socket
import struct
import time
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:                                   # older cryptography
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes


def _rx(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("VNC connection closed")
        buf += chunk
    return buf


class Phone:
    def __init__(self, ip: str, password: str, timeout: float = 8):
        host, _, port = ip.partition(":")                # "host:port" too (e.g. the server's VNC tunnel 127.0.0.1:1590N)
        self.sock = socket.create_connection((host, int(port or 5900)), timeout)
        _rx(self.sock, 12)
        self.sock.sendall(b"RFB 003.008\n")
        types = _rx(self.sock, _rx(self.sock, 1)[0])
        if 2 not in types and 1 in types:                  # CatVNC without a password (iPhone H, 2026-10-12)
            self.sock.sendall(b"\x01")
            if _rx(self.sock, 4) != b"\0\0\0\0":
                raise PermissionError("VNC refused the session")
            self.sock.sendall(b"\x01")
            self.width, self.height = struct.unpack(">HH", _rx(self.sock, 4))
            _rx(self.sock, 16)
            _rx(self.sock, struct.unpack(">I", _rx(self.sock, 4))[0])
            return
        if 2 not in types:
            raise ConnectionError(f"no VNC password auth offered ({list(types)})")
        self.sock.sendall(b"\x02")
        challenge = _rx(self.sock, 16)
        key = bytes(int(f"{c:08b}"[::-1], 2) for c in password.encode()[:8].ljust(8, b"\0"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            enc = Cipher(TripleDES(key), modes.ECB()).encryptor()
        self.sock.sendall(enc.update(challenge) + enc.finalize())
        if _rx(self.sock, 4) != b"\0\0\0\0":
            raise PermissionError("wrong VNC password")
        self.sock.sendall(b"\x01")                         # shared session: don't kick the dashboard viewer
        self.width, self.height = struct.unpack(">HH", _rx(self.sock, 4))
        _rx(self.sock, 16)                                 # pixel format
        _rx(self.sock, struct.unpack(">I", _rx(self.sock, 4))[0])

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _pointer(self, fx: float, fy: float, down: bool) -> None:
        x = max(0, min(self.width - 1, round(fx * self.width)))
        y = max(0, min(self.height - 1, round(fy * self.height)))
        self.sock.sendall(struct.pack(">BBHH", 5, 1 if down else 0, x, y))

    def tap(self, fx: float, fy: float, hold: float = 0.1) -> None:
        self._pointer(fx, fy, False)
        time.sleep(0.04)
        self._pointer(fx, fy, True)
        time.sleep(hold)
        self._pointer(fx, fy, False)

    def swipe(self, fx1: float, fy1: float, fx2: float, fy2: float, steps: int = 25) -> None:
        self._pointer(fx1, fy1, False)
        time.sleep(0.04)
        self._pointer(fx1, fy1, True)
        time.sleep(0.06)
        for i in range(1, steps + 1):
            self._pointer(fx1 + (fx2 - fx1) * i / steps, fy1 + (fy2 - fy1) * i / steps, True)
            time.sleep(0.016)
        time.sleep(0.08)
        self._pointer(fx2, fy2, False)

    def key(self, keysym: int) -> None:
        self.sock.sendall(struct.pack(">BBxxI", 4, 1, keysym))
        time.sleep(0.03)
        self.sock.sendall(struct.pack(">BBxxI", 4, 0, keysym))
        time.sleep(0.05)

    SHIFTED = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ~!@#$%^&*()_+{}|:"<>?')

    def type_text(self, text: str) -> None:
        """Type into the focused field (X11 keysyms: Latin-1 = code point).
        CatVNC drops case unless Shift is held, so shifted characters get an
        explicit Shift press around them (passwords!)."""
        for ch in text:
            if ch in self.SHIFTED:
                self.sock.sendall(struct.pack(">BBxxI", 4, 1, 0xFFE1))
                self.key(ord(ch))
                self.sock.sendall(struct.pack(">BBxxI", 4, 0, 0xFFE1))
                time.sleep(0.03)
            else:
                self.key(0xFF0D if ch == "\n" else ord(ch))

    def screenshot(self, path: str) -> str:
        """Full Raw frame -> PNG. Slow over the internet (MBs), fine on demand."""
        from PIL import Image
        # 32bpp true colour, little endian, R at bit 16 -> BGRX bytes
        self.sock.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        self.sock.sendall(struct.pack(">BxHi", 2, 1, 0))  # Raw only
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        self.sock.settimeout(60)
        while True:
            kind = _rx(self.sock, 1)[0]
            if kind == 0:
                break
            if kind == 2:                                   # bell
                continue
            if kind == 3:                                   # server cut text
                _rx(self.sock, 3)
                _rx(self.sock, struct.unpack(">I", _rx(self.sock, 4))[0])
                continue
            raise ConnectionError(f"unexpected VNC message {kind}")
        _rx(self.sock, 1)
        img = Image.new("RGB", (self.width, self.height))
        for _ in range(struct.unpack(">H", _rx(self.sock, 2))[0]):
            x, y, w, h, enc = struct.unpack(">HHHHi", _rx(self.sock, 12))
            if enc != 0:
                raise ConnectionError(f"unexpected encoding {enc}")
            img.paste(Image.frombytes("RGB", (w, h), _rx(self.sock, w * h * 4), "raw", "BGRX"), (x, y))
        img.save(path)
        return path


# iPhone X / iOS 16 Control Center positions (fractions of the screen)
CC_SWIPE = (0.97, 0.0, 0.97, 0.6)       # pull down from the top-right corner (0.85 is ignored)
CC_MIRRORING = (0.386, 0.403)            # "Screen Mirroring" tile
PICKER_FIRST_ROW_Y = 0.465               # first receiver in the picker
PICKER_ROW_HEIGHT = 0.064
PICKER_STOP = (0.5, 0.80)                # "Stop Mirroring" when already mirroring


CC_WIFI_BUTTON = (0.145, 0.30)           # blue ring of the Wi-Fi button (its centre is the white glyph)


def _grab(p: "Phone", rtsp_url: str | None, path: str) -> None:
    """A current picture of the screen. CatVNC's framebuffer goes stale (it kept
    showing the Calculator while the Control Center was open), so prefer one
    frame of the phone's live AirPlay video when it is mirroring already."""
    import subprocess
    if rtsp_url:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-rtsp_transport", "tcp",
                            "-i", rtsp_url, "-frames:v", "1", path], capture_output=True, timeout=20)
        if r.returncode == 0:
            return
    p.screenshot(path)


def control_center_open(p: "Phone", rtsp_url: str | None = None) -> bool:
    """Blind taps are only safe once the Control Center is really up: on
    2026-10-03 a missed swipe let the mirroring taps land on Settings and
    switch the Shadowrocket VPN off. Checks the Wi-Fi button for its blue."""
    import os
    import tempfile
    from PIL import Image
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        _grab(p, rtsp_url, path)
        img = Image.open(path).convert("RGB")
        r, g, b = img.getpixel((int(CC_WIFI_BUTTON[0] * img.width), int(CC_WIFI_BUTTON[1] * img.height)))
    finally:
        os.unlink(path)
    return b > 180 and r < 90 and g > 70


def start_mirroring(ip: str, password: str, picker_row: int, rtsp_url: str | None = None) -> None:
    """Control Center -> Screen Mirroring -> receiver at `picker_row`
    (receivers are listed alphabetically: "Farm iPhone 1", "Farm iPhone 2", ...).
    The phone must be awake and unlocked. Raises instead of tapping blind when
    the Control Center did not open."""
    with Phone(ip, password) as p:
        for _ in range(2):
            p.swipe(*CC_SWIPE)
            time.sleep(1.5)
            if control_center_open(p, rtsp_url):
                break
        else:
            raise ConnectionError("Control Center did not open -- nothing was tapped")
        p.tap(*CC_MIRRORING)
        time.sleep(2.5)                                     # picker discovers receivers
        p.tap(0.4, PICKER_FIRST_ROW_Y + picker_row * PICKER_ROW_HEIGHT)
        time.sleep(2.0)
        p.tap(0.5, 0.08)                                    # close the picker
        time.sleep(0.8)
        p.tap(0.5, 0.08)                                    # close the Control Center
