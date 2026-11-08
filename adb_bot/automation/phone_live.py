"""Physical-iPhone-farm "Live" page: one card per iPhone with a play button
that opens it as a draggable, resizable window (Multilogin-style).

Rene, 2026-10-02: "die Handys erstmal hier in der Übersicht haben ... schauen,
wie groß die sind, wie es mir dann gefällt". The windows are a layout preview
for now -- there is no video source yet. The stream comes later from the
mini-PC the phones are USB-cabled to (QuickTime-style USB capture, 30-60 FPS),
not from this server. Devices come from the same Reels Device table as /phones.
"""

from __future__ import annotations

import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adb_bot.automation import iphone_farm_config, phone_dashboard

# "iPhone 1 (weiß)" -> ("iPhone 1", "weiß")
_NAME_RE = re.compile(r"^(.*?)\s*\(([^)]*)\)\s*$")


def _port_open(host: str | None, port: int) -> bool:
    if not host:
        return False
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


def _vnc_answers(port: int) -> bool:
    """CatVNC really answers ("RFB ..." greeting) through the server's VNC tunnel. The tunnel's local port
    is always open, so only the greeting proves the phone is there."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.settimeout(3)
            return s.recv(12).startswith(b"RFB")
    except OSError:
        return False


def _reachable(farm: dict, relay_host: str | None) -> bool:
    """2026-10-12: "active" = the phone's CatVNC answers through adbbot-vnc-tunnel (127.0.0.1:1590N, WiFi or USB
    behind it) -- the very path touches take. Before, A-E were judged by a leftover WDA port on the Mac and the
    new WiFi phones (G, H, I) by their LAN ip, which this server cannot reach -> always grey although usable."""
    # 12.10 evening (Rene: "nur USB"): active = the phone delivers USB video right now
    if farm.get("usb_stream"):
        return bool((iphone_farm_config.video_status() or {}).get(farm["usb_stream"]))
    n = str(farm.get("stream", "")).removeprefix("iphone")
    if n.isdigit():
        return _vnc_answers(15900 + int(n))
    if farm.get("wda_port") and relay_host:
        return _port_open(relay_host, farm["wda_port"])
    return _port_open(farm.get("ip"), 5900)


def _phone(dev: dict, farm: dict, online: bool) -> dict:
    m = _NAME_RE.match(dev["name"])
    name, label = (m.group(1), m.group(2)) if m else (dev["name"], "")
    color = "weiss" if label.lower().startswith("wei") else "schwarz" if label.lower().startswith("schwarz") else ""
    # since 2026-10-07 the phones are just "iPhone A/B/C…"; keep their colour for the card icon
    color = color or {"iPhone A": "weiss", "iPhone B": "schwarz"}.get(name, "")
    return {
        "id": re.sub(r"[^a-z0-9]+", "", name.lower()) or "iphone",
        "name": name,
        "label": label,
        "color": color,
        "containers": len(dev["containers"]),
        "accounts": sum(1 for c in dev["containers"] if c["accounts"]),
        # for the account list: run the container's Shortcut (named like the container) on the phone
        "accountList": [{"username": a["username"], "status": a["status"], "kind": a["kind"],
                         "container": c["container"]}
                        for c in dev["containers"] for a in c["accounts"] if a["username"]],
        "max": dev["max_containers"] or "?",
        # "ok" = CatVNC (LAN) or WDA (USB, via the relay Mac) answers; "off" = configured
        # but unreachable (phone off / unplugged / relay down); "warn" = not in iphone_farm.json
        "state": "ok" if online else ("off" if farm else "warn"),
        "token": dev["id"] if farm else "",
        "vnc": farm.get("vnc_password", ""),
        # live picture comes as AirPlay video through the relay; VNC then only carries touches
        # "_relay.picture": "vnc" = take the picture from CatVNC (same connection as the touches, so picture
        # and phone can never be swapped) instead of the USB video -- the fallback while USB video is unreliable
        "video": bool(farm.get("stream")) and iphone_farm_config.relay().get("picture") != "vnc",
    }


def build() -> str:
    farm = iphone_farm_config.load()
    relay = iphone_farm_config.relay()
    devices = phone_dashboard.load()
    with ThreadPoolExecutor(8) as pool:
        relay_host = relay.get("ssh", "").split("@")[-1] or None
        online = list(pool.map(lambda d: _reachable(farm.get(d["id"], {}), relay_host), devices))
    phones = [_phone(d, farm.get(d["id"], {}), up) for d, up in zip(devices, online)]
    # Touches go straight to the box next to the phones when the viewer can reach it (Tailscale);
    # the detour through this server costs ~0.4 s per tap from Thailand.
    for p in phones:
        p["vncDirect"] = relay.get("vnc_ws", "") if p["token"] else ""
    tpl = Path(__file__).with_name("phone_live.html").read_text(encoding="utf-8")
    # "</" can't appear in the JSON anyway, but keep a stray name from closing the script tag
    return tpl.replace("__PHONES__", json.dumps(phones, ensure_ascii=False).replace("</", "<\\/"))
