"""How the server reaches each physical iPhone: /etc/adbbot/iphone_farm.json maps
an Airtable Device record id to {"ip", "vnc_password"}. The ip is the phone's LAN
address, routed through the MacBook's Tailscale subnet route (192.168.1.0/24)
until the farm moves to a mini PC. Kept out of Airtable on purpose: the Reels
base is shared with VAs.

`python -m adb_bot.automation.iphone_farm_config <out>` writes websockify's
token file (one "<record id>: <ip>:5900" line per phone) for adbbot-vnc-bridge.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

CONFIG_PATH = os.environ.get("IPHONE_FARM_CONFIG", "/etc/adbbot/iphone_farm.json")


def _write_raw(raw: dict) -> None:
    """Rewrite the phone config (root 600) atomically, keeping a dated backup of the previous one."""
    import shutil, time
    if os.path.exists(CONFIG_PATH):
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak-" + time.strftime("%m%d%H%M"))
    _atomic_json(CONFIG_PATH, raw)
    os.chmod(CONFIG_PATH, 0o600)


def _raw() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict:
    """Phones only: Device record id -> {ip, vnc_password, stream, picker_row}."""
    return {k: v for k, v in _raw().items() if not k.startswith("_") and isinstance(v, dict)}


def relay() -> dict:
    """The video relay (mediamtx) on the box the iPhones are cabled to:
    {"whep": base URL for WebRTC playback, "api": its status API}."""
    r = _raw().get("_relay")
    return r if isinstance(r, dict) else {}


def set_clipboard(rec_id: str, text: str, type_it: bool = False) -> tuple[bool, str]:
    """Put `text` on the phone's pasteboard (XXTouch over its SSH tunnel); with type_it also type it into
    the focused field on the phone (sys.input_text -- Rene 10.10: VNC key presses did not arrive)."""
    phone = load().get(rec_id) or {}
    if not phone.get("stream"):
        return False, "phone not configured"
    try:
        from adb_bot.automation import iphone_xxtouch
        t = iphone_xxtouch.for_phone(phone["stream"])
        t.set_clipboard(text)
        if type_it:
            t.run(f"sys.input_text({json.dumps(text)})")
            return True, f"typed {len(text)} characters on the phone"
        return True, f"{len(text)} characters on the phone's clipboard"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"clipboard failed: {exc}"[:200]


def get_clipboard(rec_id: str) -> tuple[bool, str]:
    """Text on the phone's pasteboard (what was copied on the phone) -> for the Mac's clipboard."""
    phone = load().get(rec_id) or {}
    if not phone.get("stream"):
        return False, "phone not configured"
    try:
        from adb_bot.automation.iphone_xx_flows import XX
        text = XX(phone["stream"], log=lambda m: None).lua_out(
            'local ok, v = pcall(pasteboard.read) OUT = (ok and type(v) == "string") and v or ""', timeout=20)
        return True, text
    except Exception as exc:                                  # noqa: BLE001
        return False, f"clipboard read failed: {exc}"[:200]


def view_stream(phone: dict) -> str | None:
    """The video stream that SHOWS this phone. "stream" is the phone's own capture slot (it also numbers
    its vnc-fwd/usbcap agents); "view_stream" is set by measuring which running capture shows the phone,
    so a reshuffle of macOS screen ids is fixed without restarting any capture (2026-10-09).
    2026-10-12: phones run over WiFi with AirPlay mirroring (receiver N -> iphoneN); USB capture is the
    backup on its own path usbN -- shown whenever it has frames, otherwise the AirPlay stream."""
    # 12.10 evening (Rene: "wollen nur USB da haben"): the page shows USB video only, AirPlay is off.
    return phone.get("usb_stream") or phone.get("view_stream") or phone.get("stream")


def whep(rec_id: str, offer_sdp: str) -> tuple[int, str]:
    """Forward a browser's WebRTC offer to the relay and return (status, answer).
    Only the signalling passes through this server; the video itself flows
    straight from the relay to the viewer (over Tailscale for now)."""
    import requests
    phone, base = load().get(rec_id) or {}, relay().get("whep")
    if not phone.get("stream") or not base:
        return 404, "no video configured for this phone"
    try:
        r = requests.post(f"{base}/{view_stream(phone)}/whep", data=offer_sdp.encode(),
                          headers={"Content-Type": "application/sdp"}, timeout=10)
    except requests.RequestException:
        return 502, "video relay not reachable (MacBook asleep?)"
    return r.status_code, r.text


SSH_KEY = os.environ.get("IPHONE_FARM_SSH_KEY", "/root/.ssh/iphone_farm")


def lan_ssh_opts(dest: str) -> list[str]:
    """Reach a phone's LAN address through the relay box (it sits on the same Wi-Fi), so SSH to the
    phones no longer depends on some other machine advertising 192.168.1.0/24 to the tailnet --
    that was the MacBook, and it sleeps on battery (2026-10-06). `_relay.lan_jump: false` turns it off."""
    r = relay()
    if not r.get("lan_jump", True) or not r.get("ssh") or not dest.split("@")[-1].startswith("192.168."):
        return []
    return ["-o", f"ProxyCommand=ssh -o BatchMode=yes -o ConnectTimeout=20 -i {SSH_KEY} -W %h:%p {r['ssh']}"]


def _ssh(rec_id: str, cmd: str, timeout: int = 15) -> tuple[bool, str]:
    phone = load().get(rec_id)
    if not phone or not (phone.get("ip") or phone.get("ssh")):
        return False, "phone not configured"
    # "ssh": an ~/.ssh/config alias for phones reached over USB through the Mac
    # (WLAN off, e.g. phone C) -- otherwise straight to the LAN ip
    dest = phone.get("ssh") or f"mobile@{phone['ip']}"
    try:
        out = subprocess.run(
            ["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=20", *lan_ssh_opts(dest), dest, cmd],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "phone did not answer (MacBook asleep or phone off?)"
    if out.returncode == 0 and "ok" in out.stdout:
        return True, "done"
    if out.returncode == 255:
        return False, "phone not reachable (MacBook asleep, phone off or hung?)"
    return False, (out.stderr.strip().splitlines() or ["ssh failed"])[-1][:200]


# Settings > Display & Brightness > Auto-Lock rows (iPhone X / iOS 16, fractions of the screen)
AUTOLOCK_URL = "prefs:root=DISPLAY&path=AUTOLOCK"
AUTOLOCK_30S = (0.2, 0.179)
AUTOLOCK_NEVER = (0.2, 0.505)


def _set_autolock(rec_id: str, row: tuple[float, float]) -> tuple[bool, str]:
    """Open the Auto-Lock page by URL (that alone lights a dark screen), tap a
    row, go back to the home screen. iOS has no CLI to lock or blank the screen,
    so "off" = Auto-Lock 30 s and "on" = Auto-Lock Never."""
    from adb_bot.automation import iphone_farm_vnc
    import time
    ok, msg = _ssh(rec_id, f"uiopen --url '{AUTOLOCK_URL}' && echo ok")
    if not ok:
        return ok, msg
    phone = load()[rec_id]
    time.sleep(2.5)
    try:
        with iphone_farm_vnc.Phone(phone["ip"], phone["vnc_password"]) as p:
            p.tap(*row)
            time.sleep(1.0)
            p.swipe(0.5, 0.999, 0.5, 0.55)                 # home
    except (OSError, ConnectionError, PermissionError) as exc:
        return False, f"VNC: {exc}"
    return True, "done"


OFF_PATH = os.path.expanduser("~/.adb_bot/iphone_farm_off.json")


def _atomic_json(path: str, data) -> None:
    """The platform serves requests in threads (opening "All" fires 5 at once): one shared ".tmp"
    name made os.replace fail for all but the first (2026-10-08) -- unique temp file per write."""
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def is_off(rec_id: str) -> bool:
    """Switched off on purpose (⏾): the watchdog must not restart its video."""
    try:
        with open(OFF_PATH, encoding="utf-8") as f:
            return bool(json.load(f).get(rec_id))
    except (OSError, ValueError):
        return False


def _set_off(rec_id: str, off: bool) -> None:
    try:
        with open(OFF_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if off:
        data[rec_id] = True
    else:
        data.pop(rec_id, None)
    _atomic_json(OFF_PATH, data)
    os.makedirs(os.path.dirname(OFF_PATH), exist_ok=True)
    with open(OFF_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def wake(rec_id: str) -> tuple[bool, str]:
    """Screen on and keep it on: Auto-Lock -> Never, clear the "off" mark, and
    start the live video right away.

    A VNC tap does not wake a dark iPhone, nor does a `uialert` popup or
    `uiopen --bundleid ...`; opening a URL does (tested 2026-10-02/03). The
    Auto-Lock page is opened by URL and its taps land on a page we just opened
    ourselves -- unlike the 2026-10-03 incident, where blind taps on an unknown
    screen hit the VPN switch in Settings."""
    _set_off(rec_id, False)
    ok, msg = _set_autolock(rec_id, AUTOLOCK_NEVER)
    if not ok:
        return ok, msg
    phone = load().get(rec_id) or {}
    if relay().get("video_always_on"):
        # Rene 10.10: never restart captures from here -- restarts leave iPhones "connected, 0 frames"
        return True, "screen on"
    if phone.get("stream") and not (video_status() or {}).get(phone["stream"]):
        import time
        time.sleep(1.0)
        ok, msg = mirror(rec_id, allow_wake=False)
        return True, "screen on, video " + ("started" if ok else f"not started yet ({msg}); the watchdog retries")
    return True, "screen on"


def screen_off(rec_id: str) -> tuple[bool, str]:
    """Let the screen go dark. iOS has no command to lock it, and it never
    auto-locks while it is AirPlay-mirroring (tested 2026-10-03: 30 s Auto-Lock,
    still lit 3 min later). So: mark it off (the watchdog leaves it alone),
    Auto-Lock -> 30 s, then end the mirroring by resetting its receiver.
    ☀︎ (wake) undoes all three."""
    _set_off(rec_id, True)
    ok, msg = _set_autolock(rec_id, AUTOLOCK_30S)
    if not ok:
        return ok, msg
    stream = (load().get(rec_id) or {}).get("stream")
    if stream:
        reset_receiver(stream)
    return True, "screen goes dark within 30 s"


def respring(rec_id: str) -> tuple[bool, str]:
    """Restart SpringBoard (the iPhone's UI), ~15 s. Fixes a frozen screen or
    app without a reboot, so the palera1n jailbreak survives. Works as
    `mobile`: sbreload prints "Error stopping service: 144 - Requestor lacks
    required entitlement" twice but SpringBoard does restart (checked by its
    process age, 2026-10-02). A real or userspace reboot needs root, which
    the server does not have.

    It DISCONNECTS the Shadowrocket VPN (PacketTunnel gone after a respring of
    iPhone 1, 2026-10-03) -- the account's Shortcut has to be run again."""
    return _ssh(rec_id, "sbreload >/dev/null 2>&1; echo ok", timeout=30)


def video_status() -> dict | None:
    """{stream name: ready?} from the relay, or None when the relay box (the
    MacBook / mini PC) does not answer at all."""
    import requests
    api = relay().get("api")
    if not api:
        return None
    try:
        items = requests.get(f"{api}/v3/paths/list", timeout=5).json().get("items", [])
    except (requests.RequestException, ValueError):
        return None
    return {i.get("name"): bool(i.get("ready")) for i in items}


def usb_video_restart(rec_id: str, allow_wake: bool = True) -> tuple[bool, str]:
    """USB relay (Mac mini, since 2026-10-06): the phone's picture comes from usbcap2 over the
    cable, so "start video" means: restart that phone's capture agent and make the screen change
    once -- iOS sends no frames at all for a static screen. The watchdog (allow_wake=False) must
    not do this every minute: an idle phone simply has no picture until something moves."""
    if not allow_wake:
        return False, "screen idle"
    phone = load().get(rec_id) or {}
    stream, target = phone.get("stream"), relay().get("ssh")
    if not stream or not target:
        return False, "no USB video configured for this phone"
    n = int(str(stream).removeprefix("iphone"))
    r = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target,
                        f"U=$(id -u); L=com.veralta.usbcap{n}; launchctl print gui/$U/$L >/dev/null 2>&1 || "
                        f"launchctl bootstrap gui/$U ~/Library/LaunchAgents/$L.plist; launchctl kickstart -k gui/$U/$L"],
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        return False, "Mac mini not reachable"
    import time
    try:
        from adb_bot.automation.iphone_farm_wda import connect as wda
        time.sleep(4)                                    # capture start re-plugs the phone's USB; WDA comes back
        p = wda(rec_id, timeout=40)
        for _ in range(8):
            try:
                if p.locked():
                    p.unlock()
                p.press("home")
                break
            except Exception:
                time.sleep(4)
        p.close()
    except Exception as exc:                             # picture may still come once someone touches the phone
        return True, f"capture restarted; could not wake the screen ({str(exc)[:60]})"
    return True, "capture restarted, screen woken"


def mirror(rec_id: str, allow_wake: bool = True) -> tuple[bool, str]:
    """Start AirPlay screen mirroring to this phone's receiver: Control Center
    -> Screen Mirroring -> its row, by VNC taps.

    Tries first WITHOUT waking: waking opens the Calculator, which would pull a
    VA out of whatever app they are in when the video merely dropped. Only if
    the Control Center does not open (dark screen) does it wake and retry --
    unless allow_wake=False (the watchdog: it must not light every phone all
    night; a dark phone just reports "screen off")."""
    if relay().get("video") == "usb":
        return usb_video_restart(rec_id, allow_wake)
    from adb_bot.automation import iphone_farm_vnc
    phone = load().get(rec_id) or {}
    if "picker_row" not in phone:
        return False, "no video receiver configured for this phone"
    import time
    args = (phone["ip"], phone["vnc_password"], int(phone["picker_row"]))
    try:
        iphone_farm_vnc.start_mirroring(*args)
        return True, "mirroring started"
    except ConnectionError:
        if not allow_wake:
            return False, "screen off"                 # Control Center did not open -> probably dark
    except (OSError, PermissionError) as exc:
        return False, f"VNC: {exc}"
    ok, msg = wake(rec_id)
    if not ok:
        return ok, msg
    time.sleep(1.5)
    try:
        iphone_farm_vnc.start_mirroring(*args)
    except (OSError, ConnectionError, PermissionError) as exc:
        return False, f"VNC: {exc}"
    return True, "mirroring started (screen was dark, woke it)"


def open_container_cli(rec_id: str, name: str, log=None) -> tuple[bool, str]:
    """14.10 (Rene: "keinen Bock mehr auf Kurzbefehle"): open container `name` (A1..I25) WITHOUT the
    Shortcut -- the same five steps the Shortcut did, driven over SSH:
      1. ig-close.sh (kill Instagram), Shadowrocket tunnel off (shadowrocket://disconnect),
         Crane: Shadowrocket -> "<name> Proxy" (cranectl, Crane's own CLI), Global Routing -> Proxy and
         tunnel on again (shadowrocket://route/proxy, shadowrocket://connect);
      2. iOS asks "Open in Shadowrocket?" for every URL opened from outside an app -> answered through
         WDA's alert API (they queue, so loop until none is left);
      3. exit IP must equal the container's proxy host from Airtable and the resolver must sit in the
         US (edns.ip-api.com), otherwise Instagram is NOT launched (G4 live: a bare container switch
         dropped the tunnel and left the Thai line);
      4. Crane: Instagram -> "<name> IG", uiopen Instagram.
    Returns (ok, message) like run_shortcut(); callers keep their wait_for_foreground/vpn_state checks."""
    import re
    import time as _time
    log = log or (lambda *a, **k: None)
    if not re.fullmatch(r"[A-Z]\d{1,2}", name or ""):
        return False, f"bad container name {name!r}"
    from adb_bot.automation.iphone_farm_shortcuts import airtable_hosts
    host = airtable_hosts().get(name)
    if not host:
        return False, f"{name}: no proxy host in Airtable Containers"
    ctl = "/var/jb/usr/local/bin/cranectl"
    step1 = (f"/var/jb/var/mobile/bin/ig-close.sh >/dev/null 2>&1; uiopen 'shadowrocket://disconnect?autoclose=true'; sleep 2; "
             f"{ctl} --switch com.liguangming.Shadowrocket 'name:{name} Proxy' && sleep 1 && "
             f"uiopen 'shadowrocket://route/proxy'; sleep 1; uiopen 'shadowrocket://connect?autoclose=true'; echo ok")
    ok, msg = _ssh(rec_id, step1, timeout=90)
    if not ok:
        return False, f"{name}: container/VPN switch failed: {msg}"
    # 2. the queued "Open in Shadowrocket?" sheets
    from adb_bot.automation.iphone_farm_wda import connect
    try:
        p = connect(rec_id, timeout=30)
        deadline = _time.monotonic() + 25
        n = 0
        while _time.monotonic() < deadline:
            txt = p.alert_text() or ""
            if "Shadowrocket" in txt or "Open in" in txt:
                try:
                    p.alert_accept("Open")
                except Exception:  # noqa: BLE001
                    p.alert_accept()
                n += 1
                _time.sleep(1.2)
                continue
            if n:
                break
            _time.sleep(1)
        log(f"{name}: {n} 'Open in Shadowrocket?' sheet(s) answered")
    except Exception as exc:  # noqa: BLE001
        log(f"{name}: WDA not usable for the Open sheets ({exc}) -- continuing, the IP check decides")
    # 3. the gate: exit IP == this container's proxy, resolver in the US
    probe = ("for i in $(seq 1 20); do ip=$(curl -s -m 6 -4 'http://ip-api.com/line/?fields=query' 2>/dev/null); "
             f"[ \"$ip\" = '{host}' ] && break; sleep 2; done; "
             "dns=$(curl -s -L -m 12 -4 'http://edns.ip-api.com/json' 2>/dev/null | tr -d '\\n ' | grep -o '\"geo\":\"[^\"]*' | cut -d'\"' -f4); "
             "echo \"ok ip=$ip dns=$dns\"")
    out = _ssh_out(rec_id, probe, timeout=120)          # _ssh() only says "done", the probe's text is needed
    m = re.search(r"ip=(\S*) dns=(.*)", out or "")
    ip_now, dns = (m.group(1), m.group(2).strip()) if m else ("?", "?")
    if ip_now != host:
        # A2 live (14.10): three queued sheets answered late left the tunnel OFF (exit = Thai line); one
        # more connect + sheet, then the probe again -- the second run came up in 63 s
        log(f"{name}: exit {ip_now}, tunnel not up yet -- connecting once more")
        _ssh(rec_id, "uiopen 'shadowrocket://connect?autoclose=true'; echo ok", timeout=60)
        try:
            p = connect(rec_id, timeout=30)
            for _ in range(12):
                txt = p.alert_text() or ""
                if "Shadowrocket" in txt or "Open in" in txt:
                    try:
                        p.alert_accept("Open")
                    except Exception:  # noqa: BLE001
                        p.alert_accept()
                _time.sleep(1)
        except Exception:  # noqa: BLE001
            pass
        out = _ssh_out(rec_id, probe, timeout=120)
        m = re.search(r"ip=(\S*) dns=(.*)", out or "")
        ip_now, dns = (m.group(1), m.group(2).strip()) if m else ("?", "?")
    if ip_now != host:
        return False, f"{name}: exit IP {ip_now} is not the container's proxy {host} -- Instagram not opened"
    if "unitedstates" not in dns.lower().replace(" ", ""):    # the probe strips blanks
        return False, f"{name}: resolver not in the US ({dns!r}) -- Instagram not opened"
    log(f"{name}: VPN up, exit {ip_now}, DNS {dns}")
    # 3b. 14.10 Rene ("LocSim aktivieren, es geht um den Container"): where locsim is installed, the phone's GPS
    # goes to the exit IP's city (ip-api lat/lon) once the IP gate passed. locsim is device-wide, so every app
    # opened in this container sees that location. Phones without locsim skip this.
    # NB: mobile's login shell is zsh, which does NOT word-split an unquoted $var -- `set -- $ll` put both
    # coords in $1 and left $2 empty, so the step silently never ran (14.10). Read lat/lon as separate lines.
    loc = _ssh_out(rec_id, "command -v locsim >/dev/null 2>&1 || exit 0; "
                           "ll=$(curl -s -m 8 -4 'http://ip-api.com/line/?fields=lat,lon' 2>/dev/null); "
                           "lat=$(echo \"$ll\" | sed -n 1p); lon=$(echo \"$ll\" | sed -n 2p); "
                           "[ -n \"$lon\" ] && locsim start -x $lat -y $lon -h 25 >/dev/null 2>&1 && echo \"locsim $lat $lon\"", timeout=60)
    if loc.strip():
        log(f"{name}: {loc.strip()}")
    # 4. Instagram in its container
    ok, msg = _ssh(rec_id, f"{ctl} --switch com.burbn.instagram 'name:{name} IG' && uiopen --bundleid com.burbn.instagram && echo ok", timeout=60)
    if not ok:
        return False, f"{name}: Instagram launch failed: {msg}"
    return True, f"done (cli) exit {ip_now}"


def _ssh_out(rec_id: str, cmd: str, timeout: int = 60) -> str:
    """Like _ssh() but returns the phone's stdout (empty on failure)."""
    phone = load().get(rec_id)
    if not phone or not (phone.get("ip") or phone.get("ssh")):
        return ""
    dest = phone.get("ssh") or f"mobile@{phone['ip']}"
    try:
        out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                              "-o", "ConnectTimeout=30", *lan_ssh_opts(dest), dest, cmd],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return out.stdout


def run_shortcut(rec_id: str, name: str) -> tuple[bool, str]:
    """Run an iOS Shortcut by name (the farm's are named after their Crane
    container, A1..A25 / B1..B25, and switch container + check the proxy).
    `_relay.container_open: "cli"` routes this to open_container_cli() instead (14.10)."""
    import re
    if relay().get("container_open") == "cli" and re.fullmatch(r"[A-Z]\d{1,2}", name or ""):
        return open_container_cli(rec_id, name)
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,40}", name or ""):
        return False, "bad shortcut name"
    from urllib.parse import quote
    import time as _time
    # 13.10 E24 live: over the Mac jump host the uiopen call took >15 s twice ("phone did not answer") while the
    # phone was fine -- the XXTouch path already waits 30 s x 3. Same patience here.
    ok, msg = False, ""
    for attempt in range(3):
        ok, msg = _ssh(rec_id, f"uiopen 'shortcuts://run-shortcut?name={quote(name)}' && echo ok", timeout=30)
        if ok:
            break
        _time.sleep(8)
    return ok, msg


def dead_mirror_sessions() -> set[str] | None:
    """Streams whose AirPlay session died while UxPlay kept serving its last
    frame. When an iPhone's screen goes dark it stops sending; UxPlay logs
    "raop_rtp_mirror error in header recv: 60 Operation timed out" but keeps
    the session and the compositor keeps repeating the last picture, so the
    stream looks healthy (it is still 60 fps) while it is frozen. Read from the
    receivers' logs on the relay. None when the relay can't be asked."""
    import subprocess
    r = relay()
    target = r.get("ssh")
    phones = {v["stream"]: int(str(v["stream"]).removeprefix("iphone")) for v in load().values() if v.get("stream")}
    if not target or not phones:
        return None
    cmd = "; ".join(
        f"echo \"{stream} $(grep -a -E 'Begin streaming|raop_rtp_mirror error|Connection closed|running is no longer'"
        f" ~/farm/logs/com.veralta.uxplay{n}.tty 2>/dev/null | tail -1 | tr -d '\\r')\""
        for stream, n in phones.items())
    try:
        out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target, cmd],
                             capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    dead = set()
    for line in out.stdout.splitlines():
        stream, _, last = line.partition(" ")
        if last and "Begin streaming" not in last:
            dead.add(stream)
    return dead


def reset_receiver(stream: str) -> bool:
    """Restart one UxPlay on the relay: drops the frozen session, so the stream
    goes properly 'off' (dashboard shows Start video) and can be re-mirrored."""
    import subprocess
    target = relay().get("ssh")
    n = int(str(stream).removeprefix("iphone"))
    # kickstart alone only kills the `script` wrapper; the uxplay child can outlive it for a moment
    # and keep its ports, and the new uxplay then logs "Error initialising socket 48" and serves nothing.
    name = f"uxplay -n Farm iPhone {n:02d}"
    cmd = (f"launchctl bootout gui/$(id -u)/com.veralta.uxplay{n} 2>/dev/null; pkill -f '{name}'; "
           f"for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '{name}' >/dev/null || break; sleep 1; done; "
           f"pkill -9 -f '{name}'; sleep 1; "
           f"for i in 1 2 3 4 5 6; do launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.veralta.uxplay{n}.plist "
           f"2>/dev/null && break; sleep 2; done")
    r = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target, cmd],
                       capture_output=True, timeout=60)
    return r.returncode == 0


CRANE_PREFS = "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist"


def vpn_state(rec_id: str) -> dict:
    """{"reachable", "vpn": Shadowrocket tunnel running?, "container": active Shadowrocket Crane
    container name, e.g. "A2 Proxy"} -- read silently over SSH (no VPN Status intent: that one pops
    a blocking "Done" panel on the phone)."""
    import base64
    import plistlib
    phone = load().get(rec_id) or {}
    # 11.10, found live (D3/iPhone D): USB-only phones have no "ip" key at all -- only "ssh", an
    # ~/.ssh/config alias that already routes over USB through the Mac mini (see iphone_farm_wda's
    # own "ip = phone.get('ssh') or phone['ip']" pattern, which this lacked). Checking only "ip"
    # made this return {"reachable": False} unconditionally for those phones, regardless of
    # whether the proxy/VPN was actually fine -- which it was (confirmed live: correct Shadowrocket
    # config, correct server, server itself answering). Only a real LAN IP gets "mobile@" prefixed
    # and the relay ProxyCommand; an ssh-config alias already carries its own user and routing.
    target = phone.get("ssh") or phone.get("ip")
    if not target:
        return {"reachable": False}
    dest = target if phone.get("ssh") else f"mobile@{target}"
    cmd = f"ps -A -o comm | grep -c PacketTunnel; base64 < {CRANE_PREFS} 2>/dev/null"
    # 13.10 E25 live: on a slow farm WLAN the SSH hop via the Mac took 10-12 s, this 5 s/12 s check said
    # "not reachable" and the signup aborted with Instagram open and the right proxy up. Longer + 3 tries.
    out = None
    for _attempt in range(3):
        try:
            out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                                  "-o", "ConnectTimeout=20", *lan_ssh_opts(target), dest, cmd],
                                 capture_output=True, text=True, timeout=40)
            if out.returncode in (0, 1):
                break
        except subprocess.TimeoutExpired:
            out = None
    if out is None:
        return {"reachable": False}
    if out.returncode not in (0, 1):
        return {"reachable": False}
    lines = out.stdout.split("\n", 1)
    vpn = lines[0].strip().isdigit() and int(lines[0].strip()) > 0
    container = None
    try:
        prefs = plistlib.loads(base64.b64decode("".join(lines[1].split())))
        sr = prefs["appSettings_com.liguangming.Shadowrocket"]
        names = {c["identifier"]: c["name"].strip() for c in sr["Containers"]}
        container = names.get(sr.get("activeContainer")) or None
    except Exception:
        pass
    return {"reachable": True, "vpn": vpn, "container": container}


def refresh(rec_id: str) -> tuple[bool, str]:
    """The ↻ button: make the live picture current again, whatever state it is in.
    Frozen session (picture stuck, see dead_mirror_sessions) -> reset its
    receiver; video off -> start mirroring (waking the screen if needed); video
    fine -> nothing, the browser just reconnects."""
    phone = load().get(rec_id) or {}
    stream = phone.get("stream")
    if not stream:
        return True, "no video configured; reconnecting"
    _set_off(rec_id, False)
    if relay().get("video_always_on"):
        # Rene 10.10: ↻ restarted the Mac's capture -- and restarts leave iPhones "connected, 0 frames".
        # With video always on it only reconnects the player in the browser.
        return True, "reconnected (capture left running)"
    if stream in (dead_mirror_sessions() or set()):
        reset_receiver(stream)
        import time
        time.sleep(4)                                    # receiver back up before the phone looks for it
    elif (video_status() or {}).get(stream):
        return True, "video was fine; reconnected"
    ok, msg = mirror(rec_id, allow_wake=True)
    return ok, ("video restarted" if ok else msg)


# ---- video only while someone watches (Rene 2026-10-08) ----
# One USB 2.0 line carries live video of ~3 phones that move at once; the automation needs none of
# it. So a phone's capture runs only while a Live-page window is open: open -> "watch" (start +
# lease), the page renews the lease every 60 s, close -> "unwatch" (stop). The watchdog stops
# captures whose lease ran out (tab closed without ✕) and never restarts an unwatched one.
LEASE_PATH = os.path.expanduser("~/.adb_bot/iphone_video_leases.json")
LEASE_SECONDS = 180


def _leases() -> dict:
    try:
        with open(LEASE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_leases(d: dict) -> None:
    _atomic_json(LEASE_PATH, d)


def watched(rec_id: str) -> bool:
    import time
    return time.time() - _leases().get(rec_id, 0) < LEASE_SECONDS


def _capture(rec_id: str, on: bool) -> tuple[bool, str]:
    phone = load().get(rec_id) or {}
    stream, target = phone.get("stream"), relay().get("ssh")
    if not stream or not target:
        return False, "no USB video configured for this phone"
    n = int(str(stream).removeprefix("iphone"))
    label = f"com.veralta.usbcap{n}"
    cmd = (f"U=$(id -u); launchctl print gui/$U/{label} >/dev/null 2>&1 || launchctl bootstrap gui/$U ~/Library/LaunchAgents/{label}.plist"
           if on else f"launchctl bootout gui/$(id -u)/{label} 2>/dev/null; true")
    r = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target, cmd],
                       capture_output=True, timeout=30)
    return (r.returncode == 0), ("ok" if r.returncode == 0 else "Mac mini not reachable")


import threading
_lease_lock = threading.Lock()


def watch(rec_id: str) -> tuple[bool, str]:
    import time
    with _lease_lock:
        d = _leases()
        fresh = time.time() - d.get(rec_id, 0) >= LEASE_SECONDS
        d[rec_id] = time.time()
        _save_leases(d)
    _set_off(rec_id, False)
    if relay().get("video_always_on"):
        # captures run all the time: only make sure it is loaded (no restart) and never nudge the screen --
        # the nudge flipped to Settings and back to the home screen, out of whatever app was open.
        return _capture(rec_id, True)
    if not fresh and (video_status() or {}).get(view_stream(load().get(rec_id) or {})):
        return True, "lease renewed"
    ok, msg = _capture(rec_id, True)
    if ok:
        threading.Thread(target=_nudge_screen, args=(rec_id,), daemon=True).start()
    return ok, ("video starting" if ok else msg)


def wda_enabled() -> bool:
    """WDA shows "Automation Running" on the phone and is a test tool Instagram could notice; touches
    go through CatVNC (later XXTouch). `_relay.wda: false` keeps it off everywhere (Rene 2026-10-09)."""
    return relay().get("wda", True) is not False


def _nudge_screen(rec_id: str) -> None:
    """No-op since 2026-10-11: usbcap2 re-encodes the last frame continuously, so a fresh capture already
    shows the current screen. The old Settings-jump nudge yanked whatever app was open -- removed."""
    return
    import time
    time.sleep(5)                                        # capture start re-plugs the USB; let it settle
    try:
        _ssh(rec_id, "uiopen --url 'prefs:root=General' && echo ok", timeout=15)
        time.sleep(1.5)
        if not wda_enabled():
            _ssh(rec_id, "killall Preferences 2>/dev/null; true", timeout=15)   # back to the home screen
            return
        from adb_bot.automation.iphone_farm_wda import connect as wda
        p = wda(rec_id, timeout=30)
        try:
            if p.locked():
                p.unlock()
            p.press("home")
        finally:
            p.close()
    except Exception:
        pass


def unwatch(rec_id: str) -> tuple[bool, str]:
    with _lease_lock:
        d = _leases()
        d.pop(rec_id, None)
        _save_leases(d)
    if relay().get("video_always_on"):
        # Rene 10.10: closing a window stopped the capture, opening restarted it -- that churn wedged
        # macOS's screen-capture service every time. With video always on, windows never touch captures.
        return True, "capture keeps running"
    _set_off(rec_id, True)
    return _capture(rec_id, False)


def usb_lines() -> dict:
    """{rec_id: {"line": "USB-A" | "USB-C 1" | "USB-C 2" | None, "on_usb": bool}} from the relay's USB
    tree: the iPhone's USB serial number is its UDID, the top byte of its locationID says which of
    the Mac mini's three USB controllers (one per USB-C port, one shared by both USB-A ports) it hangs on."""
    target = relay().get("ssh")
    out = {}
    try:
        txt = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target,
                              "ioreg -p IOUSB -l -w0 | grep -E '\"USB Serial Number\"|\"locationID\"'"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return {}
    loc, by_serial = None, {}
    for line in txt.splitlines():
        m = re.search(r'"locationID" = (\d+)', line)
        if m:
            loc = int(m.group(1))
        m = re.search(r'"USB Serial Number" = "([^"]+)"', line)
        if m and loc is not None:
            by_serial[m.group(1).lower().replace("-", "")] = loc
    names = {0x00: "USB-C 1", 0x01: "USB-C 2", 0x02: "USB-A"}
    for rec_id, p in load().items():
        loc = by_serial.get(str(p.get("udid", "")).lower().replace("-", ""))
        out[rec_id] = {"on_usb": loc is not None, "line": names.get((loc >> 24) & 0xFF, "USB") if loc is not None else None,
                       "loc": loc}
    return out


LINE_CAPACITY = 3      # live videos one USB line carried with all screens moving (measured 2026-10-08)


_battery_cache = {"at": 0.0, "data": {}}


def batteries() -> dict:
    """{udid: {"level": %, "charging": bool}} for every phone on the relay's USB (cached 60 s)."""
    import time
    if time.time() - _battery_cache["at"] < 60:
        return _battery_cache["data"]
    target = relay().get("ssh")
    try:
        out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target,
                              "export PATH=/opt/homebrew/bin:$PATH; for u in $(idevice_id -l); do echo \"== $u\"; "
                              "ideviceinfo -u $u -q com.apple.mobile.battery 2>/dev/null | grep -E 'BatteryCurrentCapacity|BatteryIsCharging|ExternalConnected'; done"],
                             capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return _battery_cache["data"]
    data, cur = {}, None
    for line in out.splitlines():
        if line.startswith("== "):
            cur = line[3:].strip(); data[cur] = {}
        elif cur and ":" in line:
            k, v = (x.strip() for x in line.split(":", 1))
            if k == "BatteryCurrentCapacity" and v.isdigit():
                data[cur]["level"] = int(v)
            elif k == "BatteryIsCharging":
                data[cur]["charging"] = v == "true"
    _battery_cache.update(at=time.time(), data=data)
    return data


_camera_cache = {"at": 0.0, "blocked": False}


def camera_blocked() -> bool:
    """True when macOS has withdrawn usbcap2's Camera permission on the relay: every capture then
    logs "camera access denied" and no phone has a picture, whatever the hubs/cables do. Only a
    click at the Mac mini (Privacy & Security > Camera) restores it (2026-10-08). Cached 60 s."""
    import time
    if time.time() - _camera_cache["at"] < 60:
        return _camera_cache["blocked"]
    try:
        out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", relay().get("ssh"),
                              "for f in ~/farm/logs/com.veralta.usbcap*.log; do tail -n 1 \"$f\"; echo; done"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return _camera_cache["blocked"]
    _camera_cache.update(at=time.time(), blocked="camera access denied" in out)
    return _camera_cache["blocked"]


def lines_status() -> dict:
    """For the Live page: per phone (keyed by Device record id) whether it is on the USB bus, which
    line, whether its video is live and watched; per line how many live videos it carries."""
    lines, vs = usb_lines(), video_status() or {}
    phones, per_line = {}, {}
    for rec_id, p in load().items():
        l = lines.get(rec_id, {})
        live = bool(vs.get(view_stream(p)))
        b = batteries().get(p.get("udid", ""), {})
        phones[rec_id] = {"on_usb": l.get("on_usb", False), "line": l.get("line"), "live": live, "watched": watched(rec_id),
                          "battery": b.get("level"), "charging": b.get("charging")}
        if live and l.get("line"):
            per_line.setdefault(l["line"], []).append(rec_id)
    return {"phones": phones, "lines": {k: {"live": v, "capacity": LINE_CAPACITY} for k, v in per_line.items()},
            "capacity": LINE_CAPACITY, "camera_blocked": camera_blocked()}


ACTIONS = {"wake": wake, "sleep": screen_off, "respring": respring, "mirror": mirror, "refresh": refresh,
           "watch": watch, "unwatch": unwatch}


def write_tokens(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec_id, phone in load().items():
            # USB phones: CatVNC is forwarded to 127.0.0.1:590N on the Mac mini; adbbot-vnc-tunnel brings
            # those here as 127.0.0.1:1590N, so the /vnc bridge (the only way from https) reaches every phone.
            # Before 2026-10-09 this wrote the LAN ip and only A/B had one -> no touch input on C/D/E via https.
            vnc = phone.get("vnc") or ""
            n = str(phone.get("stream", "")).removeprefix("iphone")
            if relay().get("video") == "usb" and n.isdigit():          # same N as the relay's vnc-fwdN
                f.write(f"{rec_id}: 127.0.0.1:1590{n}\n")
            elif vnc or phone.get("ip"):
                f.write(f"{rec_id}: {vnc or phone['ip'] + ':5900'}\n")


if __name__ == "__main__":
    write_tokens(sys.argv[1] if len(sys.argv) > 1 else "/run/adbbot-vnc-tokens")


# ---- which macOS screen device is which phone (2026-10-08) ----
# macOS 27 gives each iPhone screen a random UUID per USB connection, and the device names it
# reports were stale/swapped at times (window A showed B). Neither is a reliable key, so the
# mapping is MEASURED: all captures run on some assignment, every phone's screen is still, then
# one phone at a time is made to move and whichever capture receives frames is that phone's.

def screen_devices() -> dict:
    """{uuid: name} of the iPhone screens macOS offers right now (relay's ~/farm/cmiolist/cmiolist)."""
    target = relay().get("ssh")
    try:
        out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target,
                              "~/farm/cmiolist/cmiolist"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return {}
    return dict(re.findall(r"^AV uid=(\S+) name=(.*?) model=", out, re.M))


def _capture_in_fps(target: str) -> dict:
    out = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", target,
                          "for f in ~/farm/logs/com.veralta.usbcap*.log; do echo \"$f $(tail -1 $f)\"; done"],
                         capture_output=True, text=True, timeout=30).stdout
    fps = {}
    for line in out.splitlines():
        m = re.search(r"usbcap(\d+)\.log .*? in=([\d.]+)fps", line)
        if m:
            fps[int(m.group(1))] = float(m.group(2))
    return fps


ID_COLOURS = ["ff0000", "00c000", "0000ff", "ffff00", "ff00ff", "00ffff", "ff8000", "8000ff", "ffffff", "000000"]


def _frame_rgb(stream: str) -> tuple | None:
    """Average colour of the middle of one frame of a live stream (RTSP on the relay)."""
    host = relay()["ssh"].split("@")[-1]
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", f"rtsp://{host}:8554/{stream}",
                        "-frames:v", "1", "-vf", "crop=iw/2:ih/4:iw/4:ih*3/8,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=25)
    return tuple(r.stdout[:3]) if len(r.stdout) >= 3 else None


def calibrate_screens(log=print, only: list | None = None, uuids: list | None = None) -> dict:
    """Measure phone <-> screen device and store it as "screen_uuid" per phone. All captures run on
    some assignment, every phone shows its own solid colour (Safari, id.html on the relay), and the
    colour each capture receives tells which phone it is. Unwatched captures are stopped again."""
    import time
    from adb_bot.automation import iphone_farm_add
    from adb_bot.automation.iphone_farm_wda import connect as wda
    raw = _raw()
    devs = screen_devices()
    lines = usb_lines()
    phones = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict) and v.get("stream")
              and lines.get(k, {}).get("on_usb") and (only is None or k in only)}
    if not devs or not phones:
        return {}
    num = {k: int(str(v["stream"]).removeprefix("iphone")) for k, v in phones.items()}
    order = sorted(phones, key=lambda k: num[k])
    uuids = list(uuids) if uuids is not None else list(devs)
    guess = {k: uuids[i] for i, k in enumerate(order) if i < len(uuids)}
    for k, u in guess.items():
        raw[k]["screen_uuid"] = u
    _write_raw(raw)
    target = relay()["ssh"]
    iphone_farm_add.sync_usb_relay(raw, target)
    labels = " ".join(f"com.veralta.usbcap{num[k]}" for k in guess)
    subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", target,
                    # restart, not "start if missing": a capture that is already running keeps filming the
                    # OLD screen id and the measurement then checks the wrong thing (2026-10-09)
                    f"U=$(id -u); for L in {labels}; do launchctl bootout gui/$U/$L 2>/dev/null; sleep 1; "
                    f"launchctl bootstrap gui/$U ~/Library/LaunchAgents/$L.plist; done"], capture_output=True, timeout=90)
    lan = subprocess.run(["ssh", "-n", "-i", SSH_KEY, "-o", "BatchMode=yes", target,
                          "ipconfig getifaddr en1 || ipconfig getifaddr en0"], capture_output=True, text=True, timeout=20).stdout.strip()
    colour = {k: ID_COLOURS[i % len(ID_COLOURS)] for i, k in enumerate(order)}
    for k in order:
        _ssh(k, f"uiopen --url 'http://{lan}:8099/id.html?c={colour[k]}' && echo ok", timeout=15)
    time.sleep(10)
    want = {k: tuple(int(colour[k][i:i + 2], 16) for i in (0, 2, 4)) for k in order}
    measured = {}
    for k_cap, u in guess.items():                      # capture of stream num[k_cap] is device u
        rgb = _frame_rgb(f"iphone{num[k_cap]}")
        if rgb is None:
            log(f"calibrate: no frame on iphone{num[k_cap]}"); continue
        # compare hue, not brightness: Safari's dark mode dims the page (blue arrived as 39,36,97)
        norm = lambda c: [x / (max(c) or 1) for x in c]
        best = min(want, key=lambda k: sum((a - b) ** 2 for a, b in zip(norm(want[k]), norm(rgb))))
        dist = sum((a - b) ** 2 for a, b in zip(norm(want[best]), norm(rgb))) ** 0.5
        log(f"calibrate: device {u[:8]} shows {rgb} -> {best[-4:]} (hue distance {dist:.2f})")
        if dist < 0.6 and max(rgb) > 40 and best not in measured:
            measured[best] = u
    raw = _raw()
    for k, u in measured.items():
        raw[k]["screen_uuid"] = u
    _write_raw(raw)
    iphone_farm_add.sync_usb_relay(raw, target)
    for k in order:                                      # Safari away, back to the home screen
        try:
            if wda_enabled():
                p = wda(k, timeout=20); p.press("home"); p.close()
            else:
                _ssh(k, "killall MobileSafari 2>/dev/null; true", timeout=15)
        except Exception:
            pass
        if not watched(k):
            _capture(k, False)
    log(f"calibrate: {len(measured)}/{len(phones)} phones mapped")
    return measured


def auto_map(log=print) -> dict:
    """Keep "screen_uuid" right without anyone noticing. A phone that (re)connects gets a new random
    screen id; when exactly one phone lost its id and exactly one new id appeared, they are paired
    directly. Otherwise only the phones without a valid id show their colour for a few seconds."""
    raw, devs, lines = _raw(), screen_devices(), usb_lines()
    if not devs:
        return {}
    on = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict) and v.get("stream")
          and lines.get(k, {}).get("on_usb")}
    valid = {k: v["screen_uuid"] for k, v in on.items() if v.get("screen_uuid") in devs}
    used = set(valid.values())
    # two phones claiming the same id: neither is trustworthy
    dup = {u for u in used if list(valid.values()).count(u) > 1}
    valid = {k: u for k, u in valid.items() if u not in dup}
    need = [k for k in on if k not in valid]
    free = [u for u in devs if u not in set(valid.values())]
    if not need or not free:
        return {}
    if len(need) == 1 and len(free) == 1:
        from adb_bot.automation import iphone_farm_add
        raw[need[0]]["screen_uuid"] = free[0]
        _write_raw(raw)
        iphone_farm_add.sync_usb_relay(raw, relay()["ssh"])
        log(f"auto_map: {need[0]} -> {free[0][:8]} (only candidate)")
        return {need[0]: free[0]}
    return calibrate_screens(log=log, only=need, uuids=free)
