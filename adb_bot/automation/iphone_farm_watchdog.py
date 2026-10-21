"""Keep the physical iPhone farm's live view up without anyone pressing buttons.

Runs every minute (adbbot-iphone-farm-watchdog.timer). For each phone in
/etc/adbbot/iphone_farm.json:

* AirPlay session died behind a frozen picture (screen went dark) -> reset
  that receiver, so the dashboard shows "Video is off" instead of a still;
* phone reachable (SSH port) but its video stream is off -> start AirPlay
  mirroring again, WITHOUT waking a dark screen (that would keep every phone
  lit all night) -- a dark phone is retried each minute until someone wakes it (`iphone_farm_config.mirror`, which only taps once it has
  seen the Control Center open, and only wakes the screen if it has to);
* phone unreachable for OFFLINE_AFTER runs in a row -> one Telegram alert,
  and one more when it is back;
* mirroring could not be restarted RESTART_TRIES times in a row -> one alert,
  then it keeps retrying at most every RETRY_BACKOFF seconds without
  repeating the alert.

The relay box (MacBook today, the mini PC later) is checked first: if it does
not answer, nothing about the phones can be known, so that is the only alert.

Rene, 2026-10-03: "Selbstheilung ... bricht das Video ab ... das kannst du
machen. Telegram nachher". Telegram stays silent until TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID are set (category "phones"); everything is logged regardless.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time

from adb_bot.automation import iphone_farm_config

STATE_PATH = os.path.expanduser("~/.adb_bot/iphone_farm_watchdog.json")
OFFLINE_AFTER = 9            # runs in a row (~3 min at one run per 20 s) before "offline" is believed
RESTART_TRIES = 3            # failed mirror restarts before alerting
RETRY_BACKOFF = 60           # seconds between restart attempts per phone

log = logging.getLogger("iphone_farm_watchdog")


def _reachable(ip: str, port: int = 22) -> bool:
    try:
        socket.create_connection((ip, port), timeout=4).close()
        return True
    except OSError:
        return False


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def _alert(text: str) -> None:
    log.warning("ALERT %s", text)
    try:
        from adb_bot.clients.telegram import TelegramNotifier
        TelegramNotifier().send(f"📱 <b>iPhone farm</b>\n{text}", logger=log, category="phones")
    except Exception as exc:                          # an alert must never break the loop
        log.warning("telegram failed: %s", exc)


def _names() -> dict:
    """record id -> human name from Airtable, falling back to the stream name."""
    try:
        from adb_bot.automation import phone_dashboard
        return {d["id"]: d["name"] for d in phone_dashboard.load()}
    except Exception:
        return {}


USB_RETRY = 180              # seconds between video/WDA restarts per phone (each restart re-plugs the USB once)


def _relay_ssh(cmd: str, timeout: int = 30) -> str | None:
    import subprocess
    r = iphone_farm_config.relay()
    try:
        out = subprocess.run(["ssh", "-n", "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
                              r["ssh"], cmd], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return out.stdout if out.returncode == 0 else None


def _wda_up(port: int) -> bool:
    from adb_bot.automation.iphone_farm_wda import _relay_host
    return _reachable(_relay_host(), port)


FLAP_WINDOW = 600            # a capture that loses its phone more than FLAP_MAX times in this window is parked
FLAP_MAX = 5
FLAP_PARK = 3600             # ...for this long, then it gets one more chance


def _gate_captures(now: float, state: dict, phones: dict, usb: set, names: dict) -> None:
    """2026-10-12: one capture agent (com.veralta.usbcapN) runs only while its phone is on the USB bus.
    Every (re)start of a capture knocks macOS's iOSScreenCaptureAssistant, and every OTHER phone then
    drops its video for a moment -- so captures of unplugged phones (exit, launchd respawns them every
    few s) and one bad phone (D, 10-12: C alone stable, C+D dropped every ~10 s) kept the whole Mac
    mini without video. Here: absent phone -> agent booted out; phone back -> agent loaded again (at most
    one per run, so they come up one after another); a capture that keeps losing its phone is parked
    for an hour with one alert. `video_parked: true` in a phone's farm config keeps its capture off."""
    out = _relay_ssh('U=$(id -u); for n in 1 2 3 4 5 6 7 8 9; do f=~/Library/LaunchAgents/com.veralta.usbcap$n.plist; '
                     '[ -f $f ] || continue; l=-; launchctl print gui/$U/com.veralta.usbcap$n >/dev/null 2>&1 && l=L; '
                     'd=$(grep -c "phone disconnected" ~/farm/logs/com.veralta.usbcap$n.log 2>/dev/null); echo "$n $l ${d:-0}"; done')
    if out is None:
        return
    agents = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit():
            agents[int(parts[0])] = (parts[1] == "L", int(parts[2]))
    gate = state.setdefault("_capture_gate", {})
    started = False
    # 10-12: plugging D into the hub dropped B and C from usbmuxd while their cables stayed in -- still listed
    # on the USB bus, gone for usbmux/capture. A phone on the bus but missing from usbmux for 2 runs gets a
    # software re-plug (same tool as _unstick), at most once per 10 min.
    bus = set(re.findall(r'"USB Serial Number" = "([0-9a-f]{40})"', _relay_ssh("ioreg -p IOUSB -w0 -l | grep '\"USB Serial Number\"'") or ""))
    for rec_id, phone in phones.items():
        u = phone.get("udid")
        if not u:
            continue
        lost = gate.setdefault("lost_" + u[:8], {"runs": 0, "last": 0})
        if u in bus and u not in usb:
            lost["runs"] += 1
            if lost["runs"] >= 2 and now - lost["last"] > REENUM_EVERY:
                out = _relay_ssh(f"~/farm/usbreenum/usbreenum {u} 2>&1")
                lost.update(runs=0, last=now)
                log.info("%s: on the USB bus but lost by usbmux -> software re-plug: %s",
                         names.get(rec_id) or phone.get("stream"), (out or "").strip()[:100])
        else:
            lost["runs"] = 0
    for rec_id, phone in phones.items():
        n = str(phone.get("stream", "")).removeprefix("iphone")
        if not n.isdigit() or int(n) not in agents or not phone.get("udid"):
            continue
        n = int(n)
        loaded, drops = agents[n]
        name = names.get(rec_id) or phone.get("stream")
        g = gate.setdefault(str(n), {"drops": [], "last_count": drops, "parked_until": 0, "alerted": False})
        new = max(0, drops - g.get("last_count", drops))
        g["last_count"] = drops
        if new and loaded and phone["udid"] in usb:
            g["drops"] = [t for t in g["drops"] if now - t < FLAP_WINDOW] + [now] * new
        g["drops"] = [t for t in g["drops"] if now - t < FLAP_WINDOW]
        # starting a capture switches the phone into QuickTime USB mode = it drops off the bus for a few s
        # and comes back; only ~1 min of absence (3 runs) counts as unplugged (10-12: A was stopped mid-switch)
        if phone["udid"] in usb:
            if g.get("absent_runs", 0) >= 3:             # plugged back in: a fresh start, forget old drops/parking
                g.update(drops=[], parked_until=0, alerted=False)
            g["absent_runs"] = 0
        else:
            g["absent_runs"] = g.get("absent_runs", 0) + 1
            g["drops"] = []                              # drops while it is unplugged are not its fault
        present = g["absent_runs"] < 3
        if phone.get("video_unpark"):                       # manual override in the farm config: lift a parking now
            g.update(parked_until=0, drops=[], alerted=False)
        want = present and not phone.get("video_parked") and now >= g.get("parked_until", 0)
        if want and len(g["drops"]) > FLAP_MAX:
            g.update(parked_until=now + FLAP_PARK, drops=[])
            want = False
            log.info("%s: capture lost its phone %d+ times in %d min -> parked for %d min", name, FLAP_MAX, FLAP_WINDOW // 60, FLAP_PARK // 60)
            if not g["alerted"]:
                _alert(f"<b>{name}</b>: its live video keeps dropping and pulls the other phones' video down with it -- "
                       "parked for an hour (bot and touch still work). If it repeats: re-plug that phone or restart it.")
                g["alerted"] = True
        if loaded and not want:
            _relay_ssh(f"launchctl bootout gui/$(id -u)/com.veralta.usbcap{n} 2>/dev/null; true")
            log.info("%s: capture stopped (%s)", name, "phone not on USB" if not present else "parked")
        elif want and not loaded and not started:
            _relay_ssh(f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.veralta.usbcap{n}.plist 2>/dev/null; true")
            started = True
            log.info("%s: phone on USB -> capture started", name)
        if want and loaded and not g["drops"] and g["alerted"] and now - g.get("parked_until", 0) > 600:
            g["alerted"] = False
        if want and loaded:
            _unstick(now, g, n, phone, name)


STUCK_AFTER = 150            # s of "connected=1, 0x0" (never a first frame) before the screen is poked
REENUM_EVERY = 600           # at most one software re-plug per phone per 10 min


def _unstick(now: float, g: dict, n: int, phone: dict, name: str) -> None:
    """2026-10-12 root cause: a phone's USB screen session can hang -- macOS sees it, QuickTime mode is on,
    but no frame ever comes (after AirPlay mirroring, after re-plug churn). Only re-enumerating the USB
    device fixes it; ~/farm/usbreenum/usbreenum <udid> does that in software (no root, the other phones
    keep their video). A dark/idle screen also never sends a first frame, so the screen is poked with a
    toast first and the phone re-plugged only if still nothing comes."""
    last = (_relay_ssh(f"tail -n 1 ~/farm/logs/com.veralta.usbcap{n}.log") or "").strip()
    if "not found as a USB screen" in last:
        # 12.10: macOS hands a phone a NEW screen id after a re-plug; the capture keeps asking for the old one.
        # The phones carry unique names ("Farm iPhone 0N" = stream N), so look the current id up by name.
        out = _relay_ssh("~/farm/cmiolist/cmiolist 2>&1 | grep '^AV'") or ""
        m = re.search(rf"uid=([0-9A-F-]{{36}}) name=Farm iPhone {n:02d}\b", out)
        if m:
            uid = m.group(1)
            _relay_ssh(f"P=~/Library/LaunchAgents/com.veralta.usbcap{n}.plist; "
                       f"plutil -replace ProgramArguments -json '[\"/Users/reynex03/farm/usbcap2\",\"--udid\",\"{uid}\",\"--fps\",\"30\",\"--udp\",\"510{n}\"]' $P; "
                       f"launchctl bootout gui/$(id -u)/com.veralta.usbcap{n} 2>/dev/null; launchctl bootstrap gui/$(id -u) $P; true")
            log.info("%s: screen id changed after re-plug -> capture now on %s", name, uid)
        elif not out.strip():
            # 13.10: the Mac lists NO iPhone screen at all = macOS's capture service is down, not this phone.
            # Re-plugging every phone every 10 min then fixed nothing and cut each phone's USB link -- WDA died
            # on all of them every ~11 min ("broken pipe"). Leave the phones alone; only a Mac restart helps.
            log.info("%s: Mac shows no iPhone screens at all -> capture service down, no re-plug", name)
        elif now - g.get("last_reenum", 0) > REENUM_EVERY:
            # no screen with its name at all: the phone never entered QuickTime USB mode -> re-plug in software
            out = _relay_ssh(f"~/farm/usbreenum/usbreenum {phone['udid']} 2>&1")
            g["last_reenum"] = now
            log.info("%s: no screen device on the Mac -> software re-plug: %s", name, (out or "").strip()[:100])
        return
    if not re.search(r"\b0x0 in=0\.0fps.*connected=1", last):
        g.pop("stuck_since", None)
        g.pop("poked", None)
        return
    g.setdefault("stuck_since", now)
    if now - g["stuck_since"] < STUCK_AFTER:
        return
    if not g.get("poked"):
        try:
            from adb_bot.automation import iphone_xxtouch
            iphone_xxtouch.for_phone(phone["stream"])._post(
                "/spawn", b'touch.init(0)\npcall(device.unlock_screen)\nsys.toast("video check")\n')
        except Exception as exc:
            log.info("%s: screen poke failed: %s", name, exc)
        g["poked"] = now
        return
    if now - g["poked"] < 30 or now - g.get("last_reenum", 0) < REENUM_EVERY:
        return
    out = _relay_ssh(f"~/farm/usbreenum/usbreenum {phone['udid']} 2>&1")
    g.update(last_reenum=now, stuck_since=now, poked=0)
    log.info("%s: USB video hung (connected, no frame) -> software re-plug: %s", name, (out or "").strip()[:120])


def run_usb(now: float, state: dict, phones: dict) -> dict:
    """USB relay (Mac mini, since 2026-10-06): heal what a VA would otherwise have to click.
    Per phone: not on the USB bus -> alert after a few minutes ("unplugged / off / jailbreak gone");
    WDA (control) down -> restart its agent; live video not ready -> wake the screen + restart the
    capture (iphone_farm_config.usb_video_restart). Restarts are spaced USB_RETRY apart because each
    one re-plugs the phone's USB once; after RESTART_TRIES failures one alert says "re-plug it"."""
    usb = _relay_ssh("/opt/homebrew/bin/idevice_id -l")
    relay = state.setdefault("_relay", {"down_runs": 0, "alerted": False})
    if usb is None:
        relay["down_runs"] += 1
        if relay["down_runs"] >= OFFLINE_AFTER and not relay["alerted"]:
            _alert("The Mac mini the iPhones are cabled to is not answering -- every phone is offline.")
            relay["alerted"] = True
        _save_state(state)
        return state
    if relay["alerted"]:
        _alert("The Mac mini is answering again.")
    relay.update(down_runs=0, alerted=False)
    usb = set(usb.split())
    # macOS withdrew the capture tool's Camera permission (2026-10-08, after the usbcap2 swap + reboot):
    # no phone can have video and restarting captures is pointless. Say it once, and once when it is back.
    cam = state.setdefault("_camera", {"alerted": False})
    camera_ok = not iphone_farm_config.camera_blocked()
    if not camera_ok and not cam["alerted"]:
        _alert("No iPhone has live video: macOS blocked the Camera permission of <b>usbcap2</b> on the Mac mini. "
               "Fix it at the Mac mini: System Settings &gt; Privacy &amp; Security &gt; Camera &gt; turn on usbcap2.")
        cam["alerted"] = True
    elif camera_ok and cam["alerted"]:
        _alert("Camera permission on the Mac mini is back -- iPhone live video works again.")
        cam["alerted"] = False
    # phones that (re)connected get new random screen ids from macOS -> re-pair before healing video
    if now - state.get("_last_map", 0) > 60 and iphone_farm_config.relay().get("auto_calibrate") is True:   # see below, off since 10-09
        state["_last_map"] = now
        try:
            iphone_farm_config.auto_map(log=log.info)
        except Exception as exc:
            log.warning("auto_map failed: %s", exc)
        phones = iphone_farm_config.load()
    # macOS's iPhone-screen service (iOSScreenCaptureAssistant) can vanish: phones on USB, but no
    # screen devices and every capture says "phone disconnected" (2026-10-09, all video gone). Two
    # runs like that -> wake it (asking for the devices starts it) and restart the running captures.
    if usb and camera_ok and iphone_farm_config.relay().get("picture") != "vnc":
        # 2026-10-09: the first version asked cmiolist for devices -- that list is also empty for a while
        # right after captures restart, so it killed every capture again and again (video never came up).
        # Now the symptom itself: every running capture's last log line says the phone is gone, for 3 runs
        # in a row (~1 min), and at most one revive per 10 min.
        scr = state.setdefault("_screens", {"bad_runs": 0, "alerted": False, "last_revive": 0})
        # only RUNNING captures: since 10-12 captures of unplugged phones are booted out and their logs end
        # in "phone disconnected" for good -- reading those would revive (= knock) the service for nothing
        tails = _relay_ssh('U=$(id -u); for n in 1 2 3 4 5 6 7 8 9; do launchctl print gui/$U/com.veralta.usbcap$n >/dev/null 2>&1 '
                           '&& { tail -n 1 ~/farm/logs/com.veralta.usbcap$n.log; echo; }; done') or ""
        lines = [l for l in tails.splitlines() if l.strip()]
        gone = bool(lines) and all(("phone disconnected" in l) or ("not found as a USB screen" in l) for l in lines)
        if not gone:
            if scr["alerted"]:
                _alert("iPhone screens are back on the Mac mini.")
            scr.update(bad_runs=0, alerted=False)
        else:
            scr["bad_runs"] += 1
            if scr["bad_runs"] >= 3 and now - scr.get("last_revive", 0) > 600:
                scr["last_revive"] = now
                # 13.10: NO killall iOSScreenCaptureAssistant any more -- done with all captures at once it left the
                # service in a spawn/exit loop with ZERO screens that only a Mac reboot cured. Alert only.
                log.info("every capture reports its phone gone although phones are on USB -> needs a Mac mini restart")
            if scr["bad_runs"] >= 12 and not scr["alerted"]:
                _alert("No iPhone screen shows up on the Mac mini although the phones are plugged in -- "
                       "the capture service did not recover. Restart the Mac mini.")
                scr["alerted"] = True

    # Re-plugging moves the screen ids with the PORT, not the phone (2026-10-09: after a hub swap every
    # window showed another phone). When the port layout changed and has been stable for one run,
    # measure all screens again -- nobody has to care which cable a phone is in.
    try:
        layout = {k: v["loc"] for k, v in iphone_farm_config.usb_lines().items() if v.get("on_usb")}
    except Exception:
        layout = None
    # 2026-10-09: off by default -- re-measuring restarted every capture, which reshuffled the macOS screen
    # ids again and stopped unwatched captures (video never settled). Phones now get "view_stream" measured
    # without restarting anything; re-enable only with _relay.auto_calibrate: true.
    if layout and iphone_farm_config.relay().get("picture") != "vnc" and iphone_farm_config.relay().get("auto_calibrate") is True:
        if layout != state.get("_layout_seen"):
            state["_layout_seen"], state["_layout_since"] = layout, now   # changed: wait until it is stable
        elif layout != state.get("_layout_calibrated") and camera_ok and now - state.get("_layout_since", now) >= 90:
            log.info("USB layout changed -> measuring which screen is which phone")
            try:
                iphone_farm_config.calibrate_screens(log=log.info)
                state["_layout_calibrated"] = layout
                phones = iphone_farm_config.load()
            except Exception as exc:
                log.warning("calibrate after re-plug failed: %s", exc)
    status = iphone_farm_config.video_status() or {}
    names = _names()
    uid = None
    _gate_captures(now, state, phones, usb, names)
    for rec_id, phone in phones.items():
        if not phone.get("udid"):
            continue
        name = names.get(rec_id) or phone.get("stream") or rec_id
        n = int(str(phone.get("stream", "0")).replace("iphone", "") or 0)
        st = state.setdefault(rec_id, {"down_runs": 0, "offline_alerted": False, "fails": 0, "fail_alerted": False, "last_try": 0})
        if phone["udid"] not in usb:
            st["down_runs"] += 1
            if st["down_runs"] >= OFFLINE_AFTER and not st["offline_alerted"]:
                _alert(f"<b>{name}</b> is not on the USB hub any more (~{st['down_runs']} min). Unplugged, off, out of power, "
                       "or the cable/hub port is loose.")
                st["offline_alerted"] = True
            continue
        if st["offline_alerted"]:
            _alert(f"<b>{name}</b> is back on the USB hub.")
        st.update(down_runs=0, offline_alerted=False)
        # The phones lie flat in a rack: a rotated screen breaks the portrait-only live view (Rene 10-08 set
        # the rotation lock on every phone by hand). Belt and braces: turn anything not portrait back.
        wda_on = iphone_farm_config.wda_enabled()
        if wda_on and phone.get("wda_port") and now - st.get("orient_check", 0) > 300 and _wda_up(phone["wda_port"]):
            st["orient_check"] = now
            try:
                from adb_bot.automation.iphone_farm_wda import connect
                with connect(phone.get("stream") or rec_id) as wp:
                    o = wp._req("GET", wp._s("/orientation"))
                    if str(o).upper() != "PORTRAIT":
                        wp._req("POST", wp._s("/orientation"), {"orientation": "PORTRAIT"})
                        log.info("%s: screen was %s -> back to portrait", name, o)
            except Exception as exc:
                log.info("%s: orientation check failed: %s", name, exc)
        if now - st.get("last_try", 0) < (USB_RETRY * 5 if st["fail_alerted"] else USB_RETRY):
            continue                       # after the "re-plug it" alert, poke it only every 15 min
        acted = []
        if wda_on and phone.get("wda_port") and not _wda_up(phone["wda_port"]):
            if uid is None:
                uid = (_relay_ssh("id -u") or "").strip()
            _relay_ssh(f"launchctl kickstart -k gui/{uid}/com.veralta.wda{n}")
            acted.append("WDA restarted")
        always_on = iphone_farm_config.relay().get("video_always_on") is True
        if phone.get("stream") and not always_on and not iphone_farm_config.watched(rec_id):
            # nobody has this phone open on the Live page: its capture must not take USB bandwidth
            if not st.get("capture_stopped"):
                iphone_farm_config._capture(rec_id, False)
                st["capture_stopped"] = True
                log.info("%s: not watched -> live video stopped", name)
        elif (phone.get("stream") and not status.get(phone["stream"]) and camera_ok
              and iphone_farm_config.watched(rec_id) and iphone_farm_config.relay().get("video_heal", True) is not False):
            # only when someone is looking at it: an idle phone sends no frames, a restart doesn't change
            # that and only knocks its USB link (always-on captures were restarted every 3 min, 09.10)
            st["capture_stopped"] = False
            ok, msg = iphone_farm_config.usb_video_restart(rec_id)
            acted.append(f"video restarted ({msg})")
        else:
            st["capture_stopped"] = False
        if not acted:
            if st["fail_alerted"]:
                _alert(f"<b>{name}</b>: control and live video are back.")
            st.update(fails=0, fail_alerted=False)
            continue
        st["last_try"] = now
        st["fails"] += 1
        log.info("%s: %s (try %d)", name, ", ".join(acted), st["fails"])
        if st["fails"] >= RESTART_TRIES and not st["fail_alerted"]:
            _alert(f"<b>{name}</b>: {', '.join(acted)} {st['fails']}x without success. "
                   "Unplug the phone's cable for 3 seconds and plug it back in.")
            st["fail_alerted"] = True
    _save_state(state)
    return state


WLAN_RETRY = 120             # seconds between WDA agent restarts per phone (WDA needs ~20 s to come up)
LICENCE_EVERY = 3600         # seconds between XXTouch licence probes per phone


def _xx_licence(ref: str) -> int | None:
    """0 = XXTouch runs scripts, 403 = Havoc licence invalid, None = phone/tunnel not reachable."""
    try:
        from adb_bot.automation import iphone_xxtouch
        t = iphone_xxtouch.Touch(ref)
        res = t._post("/spawn", b"sys.msleep(1)\n")
        return res.get("code")
    except Exception as exc:                                # noqa: BLE001
        log.info("%s: licence probe skipped (%s)", ref, str(exc)[:80])
        return None


def run_wlan(now: float, state: dict, phones: dict) -> dict:
    """22.10 (Rene: alles ueber WLAN, auch das Video): no USB bus, no captures. Every phone is reached by its
    Bonjour name (phone["host"], Farm-iPhone-0N.local) through adbbot-vnc-tunnel: WDA 810N, WDA-MJPEG 910N,
    CatVNC 1590N. Per phone: name not resolving on the Mac mini -> "off the WLAN" alert after OFFLINE_AFTER runs
    (asleep, off, out of power -- or jailbreak gone when only lockdown answers); WDA down -> kickstart its
    com.veralta.wdaN agent, spaced WLAN_RETRY apart, alert after RESTART_TRIES failures; one alert when back."""
    names = _names()
    relay = state.setdefault("_relay", {"down_runs": 0, "alerted": False})
    hosts = {k: p.get("host") for k, p in phones.items() if p.get("host")}
    # one ssh round-trip for all names: "<name> <ip-or-empty>" per line
    # mDNS lookups of names that are NOT on the WLAN take ~5 s each -> run them in parallel, generous timeout
    # a phone on the USB hub also answers with its 169.254.x USB address -- prefer the WLAN one, accept either (22.10:
    # B and G resolved to the USB address only for a moment and must not read as "off the WLAN")
    cmd = " ".join(f"(echo \"{h} $(dscacheutil -q host -a name {h} 2>/dev/null | sed -n 's/^ip_address: //p' | sort | grep -v 169.254 | head -1) "
                   f"$(dscacheutil -q host -a name {h} 2>/dev/null | sed -n 's/^ip_address: //p' | grep 169.254 | head -1)\") &"
                   for h in sorted(set(hosts.values()))) + " wait"
    out = _relay_ssh(cmd, timeout=90)
    if out is None:
        relay["down_runs"] += 1
        if relay["down_runs"] >= OFFLINE_AFTER and not relay["alerted"]:
            _alert("The Mac mini (farm relay) is not answering -- every phone is offline in the dashboard.")
            relay["alerted"] = True
        _save_state(state)
        return state
    if relay["alerted"]:
        _alert("The Mac mini is answering again.")
    relay.update(down_runs=0, alerted=False)
    resolved = {}
    for line in out.splitlines():
        parts = line.split()
        if parts:
            resolved[parts[0]] = parts[1] if len(parts) > 1 else ""
    uid = None
    for rec_id, phone in phones.items():
        if iphone_farm_config.is_off(rec_id):
            continue
        name = names.get(rec_id) or phone.get("name") or phone.get("stream", rec_id)
        host = hosts.get(rec_id)
        n = str(phone.get("stream", "")).removeprefix("iphone")
        st = state.setdefault(rec_id, {"down_runs": 0, "offline_alerted": False, "fails": 0, "fail_alerted": False, "last_try": 0})
        if not host or not resolved.get(host):
            st["down_runs"] += 1
            if st["down_runs"] >= OFFLINE_AFTER and not st["offline_alerted"]:
                _alert(f"<b>{name}</b> is not on the WLAN any more (~{st['down_runs']*20//60} min, name {host or '?'} does not "
                       f"resolve on the Mac mini). Asleep, off, out of power -- or rebooted (jailbreak gone)?")
                st["offline_alerted"] = True
            continue
        if st["offline_alerted"]:
            _alert(f"<b>{name}</b> is back on the WLAN ({resolved[host]}).")
        st.update(down_runs=0, offline_alerted=False)
        if not (iphone_farm_config.wda_enabled() and phone.get("wda_port") and n.isdigit()):
            continue
        # 22.10 (Rene: Havoc "unregistered" on B/C/D, XXTouch 403 on A/B/C the same evening): probe the XXTouch licence
        # once an hour per phone and say which phone needs the Havoc login in Sileo -- before a posting run finds out.
        if now - st.get("lic_check", 0) > LICENCE_EVERY:
            st["lic_check"] = now
            code = _xx_licence(phone.get("ssh") or rec_id)
            if code == 403 and not st.get("lic_alerted"):
                _alert(f"<b>{name}</b>: XXTouch says 'requires a valid license' (403). Havoc login in Sileo needed on this "
                       f"phone -- posting and signup touches fail until then.")
                st["lic_alerted"] = True
            elif code == 0 and st.get("lic_alerted"):
                _alert(f"<b>{name}</b>: XXTouch licence is valid again.")
                st["lic_alerted"] = False
        if _wda_up(phone["wda_port"]):
            if st["fail_alerted"]:
                _alert(f"<b>{name}</b>: WDA (control + live picture) is back.")
            st.update(fails=0, fail_alerted=False)
            continue
        if now - st.get("last_try", 0) < (WLAN_RETRY * 5 if st["fail_alerted"] else WLAN_RETRY):
            continue
        st["last_try"] = now
        if uid is None:
            uid = (_relay_ssh("id -u") or "501").strip()
        _relay_ssh(f"launchctl kickstart -k gui/{uid}/com.veralta.wda{n}")
        st["fails"] += 1
        log.info("%s: WDA down -> kickstart com.veralta.wda%s (try %d)", name, n, st["fails"])
        if st["fails"] >= RESTART_TRIES and not st["fail_alerted"]:
            _alert(f"<b>{name}</b>: WDA restarted {st['fails']}x without success -- no control and no live picture. "
                   f"Phone on the WLAN ({resolved[host]}) but WDA will not start: locked screen? DDI? XCTest runner?")
            st["fail_alerted"] = True
    _save_state(state)
    return state


def run(now: float | None = None) -> dict:
    now = now or time.time()
    state = _load_state()
    phones = iphone_farm_config.load()
    if iphone_farm_config.relay().get("video") == "wda":
        return run_wlan(now, state, phones)
    if iphone_farm_config.relay().get("video") == "usb":
        return run_usb(now, state, phones)
    status = iphone_farm_config.video_status()

    relay = state.setdefault("_relay", {"down_runs": 0, "alerted": False})
    if status is None:
        relay["down_runs"] += 1
        if relay["down_runs"] >= OFFLINE_AFTER and not relay["alerted"]:
            _alert("The box the iPhones are cabled to (MacBook) is not answering. "
                   "All phones are offline in the dashboard. Is it asleep, unplugged or off Tailscale?")
            relay["alerted"] = True
        _save_state(state)
        log.info("relay down (%d runs)", relay["down_runs"])
        return state
    if relay["alerted"]:
        _alert("The MacBook is answering again.")
    relay.update(down_runs=0, alerted=False)

    # a session that died while UxPlay keeps repeating its last frame looks "ready": reset those first
    dead = iphone_farm_config.dead_mirror_sessions() or set()
    off_streams = {p.get("stream") for k, p in phones.items() if iphone_farm_config.is_off(k)}
    for stream in dead - off_streams:
        if status.get(stream):
            ok = iphone_farm_config.reset_receiver(stream)
            log.info("%s: AirPlay session died (frozen picture) -> receiver reset: %s", stream, ok)
            status[stream] = False

    names = None
    for rec_id, phone in phones.items():
        st = state.setdefault(rec_id, {"down_runs": 0, "offline_alerted": False,
                                       "fails": 0, "fail_alerted": False, "last_try": 0})
        label = None

        def name():
            nonlocal names, label
            if label is None:
                names = names if names is not None else _names()
                label = names.get(rec_id) or phone.get("stream") or rec_id
            return label

        if not _reachable(phone["ip"]):
            st["down_runs"] += 1
            if st["down_runs"] >= OFFLINE_AFTER and not st["offline_alerted"]:
                _alert(f"<b>{name()}</b> is offline (no network for ~{st["down_runs"] // 3} min). "
                       "Off, frozen, out of battery or off Wi-Fi.")
                st["offline_alerted"] = True
            log.info("%s unreachable (%d runs)", rec_id, st["down_runs"])
            continue
        if st["offline_alerted"]:
            _alert(f"<b>{name()}</b> is back online.")
        st.update(down_runs=0, offline_alerted=False)

        stream = phone.get("stream")
        if not stream or status.get(stream, False):
            if st["fail_alerted"]:
                _alert(f"<b>{name()}</b>: live video is back.")
            st.update(fails=0, fail_alerted=False)
            continue
        if phone.get("auto_video") is False or iphone_farm_config.is_off(rec_id):
            continue                                       # switched off on purpose (⏾)
        if now - st.get("last_try", 0) < RETRY_BACKOFF:
            continue
        st["last_try"] = now
        ok, msg = iphone_farm_config.mirror(rec_id, allow_wake=False)
        log.info("%s video off -> restart mirroring: %s (%s)", rec_id, ok, msg)
        if ok:
            st["fails"] = 0
            continue
        if msg == "screen off":
            # a dark phone is normal (auto-lock); video comes back once someone wakes it
            st["last_try"] = now - RETRY_BACKOFF + 60           # look again next minute
            continue
        st["fails"] += 1
        if st["fails"] >= RESTART_TRIES and not st["fail_alerted"]:
            _alert(f"<b>{name()}</b>: live video stopped and could not be restarted "
                   f"({st['fails']} tries). Last error: {msg}")
            st["fail_alerted"] = True

    _save_state(state)
    return state


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
