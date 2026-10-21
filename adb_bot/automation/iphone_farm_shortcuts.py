"""Keep the physical farm's container Shortcuts (A1..A25 on iPhone 1, B1..B25 on
iPhone 2, ...) in line with Airtable.

Each Shortcut was rebuilt on 2026-10-03 to:

    Close Instagram (SSH to the phone itself, see _close_instagram) -> not "OK": ❌ + stop
    -> Disconnect VPN -> wait 2 s -> Crane: Shadowrocket -> "<name> Proxy"
    -> Shadowrocket Global Routing = Proxy -> Connect VPN -> wait 2 s
    -> up to 15x: IP from api.ipify.org; is it <proxy host>? else wait 1 s
    -> not the proxy host: ❌ banner + stop (Instagram is NOT opened)
    -> Crane: Instagram -> "<name> IG" -> open Instagram -> ✅ banner

Why each piece exists (all found the hard way the same day):
* Switching the Shadowrocket container while the VPN is up dropped the tunnel
  while the old "Shadowrocket is now connected" banner still showed -> Instagram
  opened on the home (Thai) IP. Hence Disconnect first.
* The originals' "VPN Status is Connected" check compared an entity to text
  (shown red / invalid) and never stopped anything. "VPN Status" also pops a
  blocking "Done" panel per call, so it can't be polled; the external-IP check
  is silent and stricter (it also catches a connected VPN on the wrong proxy).
* Global Routing stayed "Config" although each container's prefs file said
  Proxy (file writes aren't effective); the GlobalRoutingIntent sets it for real.
* Instagram kept running in the background when it wasn't swiped away, and a
  Crane container switch does not reliably end it (Rene, 10-03) -> it is killed
  first, and nothing else happens unless the kill is confirmed (10-04).
* Apple's "Get current IP address" returned a 502 HTML page through a working
  proxy -> ipify. SSH / URL outputs must be coerced to text before "is".
* First run of each Shortcut asks twice (SSH on 127.0.0.1, api.ipify.org) --
  answered by tapping Allow; UUIDs are stable so a rebuild does not re-ask.
* iOS asks notification permission once per Shortcut and an ignored prompt stops
  the Shortcut -- so the ✅ banner comes after Instagram is open.

The expected IP is baked into each Shortcut. `sync()` (timer, every 15 min) reads
the proxy host per container from the Reels base's Containers table and rewrites
only the Shortcuts whose baked-in IP differs -- so changing a proxy in Airtable
updates the check without anyone touching the phone. It does NOT change the
proxy inside Shadowrocket -- `sync_shadowrocket()` does that, and runs first.

Editing works on the Shortcuts SQLite DB over SSH: stop the Shortcuts processes,
copy db + WAL, edit ZSHORTCUTACTIONS.ZDATA (+ ZSHORTCUT.ZACTIONCOUNT /
ZMODIFICATIONDATE), checkpoint, copy back, delete the phone's -wal/-shm. A copy
of the original DB is on each phone as Shortcuts.sqlite.bak-20261003.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from adb_bot.automation import iphone_farm_config

log = logging.getLogger("iphone_farm_shortcuts")

DB_DIR = "/var/mobile/Library/Shortcuts"
KILL = "killall Shortcuts ShortcutsUI BackgroundShortcutRunner siriactionsd 2>/dev/null"
CORE_DATA_EPOCH = 978307200
SR = {"BundleIdentifier": "com.liguangming.Shadowrocket", "Name": "Shadowrocket", "TeamIdentifier": "JBJ55K5C9X"}
_NAME_RE = re.compile(r"^[A-Z]\d{1,3}$")


def _u() -> str:
    return str(uuid.uuid4()).upper()


_UUID_NS = uuid.UUID("6b1d3c1e-6a43-4c9e-9a51-0f2a6f1c2b10")


def _stable_uuids(actions: list, name: str, keep: list) -> list:
    """iOS remembers "Allow <shortcut> to run an SSH script on 127.0.0.1 / to
    connect to api.ipify.org" per action UUID. Fresh random UUIDs on every
    rebuild (= every proxy change in Airtable) would ask again, and an unanswered
    prompt stops the shortcut. So the actions we generate get a UUID derived from
    shortcut name + position; the container's own Crane / Open-App actions
    (`keep`) keep theirs."""
    kept = {a["WFWorkflowActionParameters"].get("UUID") for a in keep}
    remap = {}
    for i, a in enumerate(actions):
        u = a["WFWorkflowActionParameters"].get("UUID")
        if u and u not in kept:
            remap[u] = str(uuid.uuid5(_UUID_NS, f"{name}/{i}/{a['WFWorkflowActionIdentifier']}")).upper()

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return remap.get(x, x) if isinstance(x, str) else x
    return walk(actions)


def _app_intent(ident: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": f"com.liguangming.Shadowrocket.{ident}",
            "WFWorkflowActionParameters": {"UUID": _u(), "AppIntentDescriptor": dict(SR, AppIntentIdentifier=ident), **params}}


def _delay(s: float) -> dict:
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.delay", "WFWorkflowActionParameters": {"UUID": _u(), "WFDelayTime": float(s)}}


IP_URL = "https://api.ipify.org"
IP_OUTPUT = "Contents of URL"
SSH_OUTPUT = "Shell Script Result"
COND_IS, COND_CONTAINS = 4, 99


def _get_ip() -> tuple[str, dict]:
    """External IP via ipify. Apple's own "Get current IP address" answered a
    "502 Bad Gateway" page through a working proxy (A2, 2026-10-03)."""
    u = _u()
    return u, {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
               "WFWorkflowActionParameters": {"UUID": u, "WFURL": IP_URL, "WFHTTPMethod": "GET"}}


def _close_instagram() -> tuple[str, dict]:
    """Kill Instagram before the VPN goes down (swiping it away by hand was the
    only safeguard). SSH to the phone itself with the Shortcuts app's own key,
    which ~/.ssh/authorized_keys pins to `restrict,command=ig-close.sh,from=127.0.0.1`
    -- the key can do nothing but end Instagram and answer OK / FAIL."""
    u = _u()
    return u, {"WFWorkflowActionIdentifier": "is.workflow.actions.runsshscript",
               "WFWorkflowActionParameters": {"UUID": u, "WFSSHHost": "127.0.0.1", "WFSSHPort": "22",
                   "WFSSHUser": "mobile", "WFSSHAuthenticationType": "SSH Key", "WFSSHScript": "close"}}


def _if_is(out_uuid: str, value: str, g: str, output: str = IP_OUTPUT, cond: int = COND_IS) -> dict:
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {"GroupingIdentifier": g, "WFCondition": cond, "WFConditionalActionString": value,
                # SSH / URL results are not text by default; uncoerced, "is OK" was false on an "OK" answer
                "WFControlFlowMode": 0, "WFInput": {"Type": "Variable", "Variable": {"Value": {
                    "OutputName": output, "OutputUUID": out_uuid, "Type": "ActionOutput",
                    "Aggrandizements": [{"Type": "WFCoercionVariableAggrandizement",
                                         "CoercionItemClass": "WFStringContentItem"}]},
                    "WFSerializationType": "WFTextTokenAttachment"}}}}


def _flow(g: str, mode: int, **extra) -> dict:
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {"GroupingIdentifier": g, "WFControlFlowMode": mode, **extra}}


def _repeat(g: str, mode: int, n: int = 15) -> dict:
    p = {"GroupingIdentifier": g, "WFControlFlowMode": mode, "UUID": _u()}
    if mode == 0:
        p["WFRepeatCount"] = n
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.repeat.count", "WFWorkflowActionParameters": p}


def _notify(title: str, body: str, ip_uuid: str | None = None, output: str = IP_OUTPUT) -> dict:
    text: dict = {"string": body}
    if ip_uuid:
        text = {"string": body + "￼", "attachmentsByRange": {"{%d, 1}" % len(body): {
            "OutputName": output, "OutputUUID": ip_uuid, "Type": "ActionOutput"}}}
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.notification",
            "WFWorkflowActionParameters": {"UUID": _u(), "WFNotificationActionTitle": title,
                "WFNotificationActionBody": {"Value": text, "WFSerializationType": "WFTextTokenString"},
                "WFNotificationActionSound": False}}


def _exit(msg: str) -> dict:
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.exit",
            "WFWorkflowActionParameters": {"WFOutput": {"Value": {"string": msg}, "WFSerializationType": "WFTextTokenString"}}}


def expected_host(actions: list) -> str | None:
    """The proxy host baked into a rebuilt Shortcut (its 'Current IP Address is …' value)."""
    for a in actions:
        p = a.get("WFWorkflowActionParameters", {})
        src = p.get("WFInput", {}).get("Variable", {}).get("Value", {}).get("OutputName")
        if (a.get("WFWorkflowActionIdentifier") == "is.workflow.actions.conditional" and p.get("WFCondition") == COND_IS
                and src in (IP_OUTPUT, "Current IP Address")):          # not the "Instagram closed: OK" check
            return p.get("WFConditionalActionString")
    return None


def is_current(actions: list) -> bool:
    """Built by this version of `rebuild` (closes Instagram first, checks the IP via ipify)?

    2026-10-19 Rene: G-Shortcuts hatten noch die alten DLW*TunnelIntent App-Intents (von einem
    anderen VPN-App) statt der nativen Shadowrocket StopVPN/StartVPN/GlobalRouting-Intents.
    Plus kein GlobalRoutingIntent → DNS leckte → Human Verification auf jeden neuen Signup.
    Shortcuts mit DLW-Intents werden jetzt als "nicht current" erkannt und rebuilt."""
    ids = [a.get("WFWorkflowActionIdentifier") or "" for a in actions]
    urls = {a.get("WFWorkflowActionParameters", {}).get("WFURL") for a in actions}
    # Any DLW* intent id -> old shortcut (DNSCloak/other VPN app leftovers), must rebuild
    if any("DLW" in i for i in ids):
        return False
    # Must have Shadowrocket's native intents: StopVPN + GlobalRouting + StartVPN
    required = ("StopVPNIntent", "GlobalRoutingIntent", "StartVPNIntent")
    if not all(any(r in i for i in ids) for r in required):
        return False
    # 20.10 BUG FIX: if there's an openapp Shadowrocket action anywhere in the shortcut, it was
    # inserted by an earlier broken rebuild. Force a rebuild to clean it out -- the fleet-wide
    # posting failure today was triggered by this: the SECOND openapp action (originally Instagram)
    # ended up being Shadowrocket after rebuild picked the first openapp in the list.
    # 22.10 (Rene: "am Schluss steht Open App, aber die App ist nicht eingetragen"): the only openapp a container
    # Shortcut may carry is Instagram's. An openapp with an empty/missing/other identifier (Shadowrocket from the
    # 20.10 bug, or a blank one) passed as "current" before and was never rebuilt -> the Shortcut ended in
    # "Open App" without an app. Any such action forces a rebuild; rebuild() constructs a fresh Instagram openapp.
    for a in actions:
        if a.get("WFWorkflowActionIdentifier") == "is.workflow.actions.openapp":
            prm = a.get("WFWorkflowActionParameters", {})
            if prm.get("WFAppIdentifier") != "com.burbn.instagram" \
               or (prm.get("WFSelectedApp") or {}).get("BundleIdentifier") != "com.burbn.instagram":
                return False
    if not any(a.get("WFWorkflowActionIdentifier") == "is.workflow.actions.openapp" for a in actions):
        return False                                        # no openapp at all -> never opens Instagram
    return (bool(ids) and ids[0] == "is.workflow.actions.runsshscript" and "is.workflow.actions.getipaddress" not in ids
            and COUNTRY_URL in urls and DNS_URL in urls)


COUNTRY_URL = "http://ip-api.com/line/?fields=countryCode"   # "US\n" -- the exit IP's country
DNS_URL = "http://edns.ip-api.com/json"                      # which resolver Meta-style GeoDNS would see
MATCH_OUTPUT = "Matches"
COND_HAS_VALUE, COND_NO_VALUE = 100, 101


def _get_url(url: str) -> tuple[str, dict]:
    u = _u()
    return u, {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
               "WFWorkflowActionParameters": {"UUID": u, "WFURL": url, "WFHTTPMethod": "GET"}}


def _token(out_uuid: str, output: str) -> dict:
    return {"Value": {"string": "\ufffc", "attachmentsByRange": {"{0, 1}": {
                "OutputName": output, "OutputUUID": out_uuid, "Type": "ActionOutput"}}},
            "WFSerializationType": "WFTextTokenString"}


def _match(out_uuid: str, pattern: str, output: str = IP_OUTPUT) -> tuple[str, dict]:
    u = _u()
    return u, {"WFWorkflowActionIdentifier": "is.workflow.actions.text.match",
               "WFWorkflowActionParameters": {"UUID": u, "WFMatchTextPattern": pattern,
                   "WFMatchTextCaseSensitive": False, "text": _token(out_uuid, output)}}


def _if_value(out_uuid: str, g: str, has: bool, output: str = MATCH_OUTPUT) -> dict:
    return {"WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {"GroupingIdentifier": g, "WFControlFlowMode": 0,
                "WFCondition": COND_HAS_VALUE if has else COND_NO_VALUE,
                "WFInput": {"Type": "Variable", "Variable": {"Value": {
                    "OutputName": output, "OutputUUID": out_uuid, "Type": "ActionOutput"},
                    "WFSerializationType": "WFTextTokenAttachment"}}}}


def _country_checks(name: str) -> list:
    """Rene 2026-10-04: "alles außer USA muss abgelehnt werden". The ipify check only proves
    the exit IP; that day the IP was the US proxy while DNS went through AIS Bangkok and
    Instagram's sign-up offered Thailand. So: the exit IP's country must be US, and every
    resolver edns.ip-api.com reports (dns + edns client subnet) must be in the United States."""
    out = []
    cu, cget = _get_url(COUNTRY_URL)
    mu, m = _match(cu, r"^\s*US\s*$")
    g = _u()
    out += [cget, m, _if_value(mu, g, has=False),
            _notify(f"❌ {name}: IP nicht in den USA", "Instagram NICHT geöffnet. Land: ", cu),
            _exit(f"{name}: exit IP not in the US -- Instagram not opened."), _flow(g, 2)]
    du, dget = _get_url(DNS_URL)
    # the service pretty-prints ("geo": "...") while Shortcuts' dictionary-to-text is compact; accept both.
    # A response without any "geo" fails the first test, so a broken lookup rejects too.
    for pattern, has in ((r'"geo"\s*:\s*"United States', False), (r'"geo"\s*:\s*"(?!United States)', True)):
        mu, m = _match(du, pattern)
        g = _u()
        out += [m, _if_value(mu, g, has=has),
                _notify(f"❌ {name}: DNS nicht in den USA", "Instagram NICHT geöffnet. DNS: ", du),
                _exit(f"{name}: DNS not in the US -- Instagram not opened."), _flow(g, 2)]
    return out[:6] + [dget] + out[6:]


def rebuild(actions: list, name: str, host: str) -> list:
    """New action list for container Shortcut `name`, keeping its own Crane switches and
    Open-Instagram action (the parts that identify the container)."""
    crane = [a for a in actions if a["WFWorkflowActionIdentifier"] == "com.opa334.CraneApplication.SetActiveCraneContainerIntent"]
    sr_switch = next(a for a in crane if "Shadowrocket" in str(a["WFWorkflowActionParameters"].get("app")))
    ig_switch = next(a for a in crane if "instagram" in str(a["WFWorkflowActionParameters"].get("app")))
    # 20.10 BUG FIX (Rene): must pick openapp Instagram specifically. Earlier rebuilds inserted
    # openapp Shadowrocket and the former `next(...)` picked THAT as open_ig, so the rebuilt
    # shortcut had openapp Shadowrocket at the end instead of openapp Instagram -- fleet-wide
    # "Open App Failed because it couldn't find the app". If the previous rebuild already clobbered
    # Instagram's openapp, construct a fresh one from scratch.
    open_ig_matches = [a for a in actions if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.openapp"
                       and a.get("WFWorkflowActionParameters", {}).get("WFAppIdentifier") == "com.burbn.instagram"]
    # 22.10 (Rene: the editor shows "Open App" with NO app and the run fails): since iOS 15 the Shortcuts app
    # keeps the chosen app in WFSelectedApp {BundleIdentifier, Name}; a bare WFAppIdentifier (all our rebuilt
    # shortcuts, 13/13 on B, 25/25 on H) renders blank. Always write both.
    open_ig = {"WFWorkflowActionIdentifier": "is.workflow.actions.openapp",
               "WFWorkflowActionParameters": {"WFAppIdentifier": "com.burbn.instagram",
                                              "WFSelectedApp": {"BundleIdentifier": "com.burbn.instagram", "Name": "Instagram"}}}
    if open_ig_matches and open_ig_matches[0].get("WFWorkflowActionParameters", {}).get("UUID"):
        open_ig["WFWorkflowActionParameters"]["UUID"] = open_ig_matches[0]["WFWorkflowActionParameters"]["UUID"]
    gc = _u()
    cu, close = _close_instagram()
    out = [close, _if_is(cu, "OK", gc, SSH_OUTPUT), _flow(gc, 1),
           _notify(f"❌ {name}: Prüfung vor dem Wechsel fehlgeschlagen", "VPN NICHT gewechselt. Antwort: ", cu, SSH_OUTPUT),
           _exit(f"{name}: Instagram could not be closed -- VPN left alone."), _flow(gc, 2)]
    out += [_app_intent("StopVPNIntent"), _delay(2), sr_switch,
           _app_intent("GlobalRoutingIntent", globalRouting="Proxy"), _app_intent("StartVPNIntent"), _delay(2)]
    g, gi = _u(), _u()
    iu, ip = _get_ip()
    out += [_repeat(g, 0), ip, _if_is(iu, host, gi), _flow(gi, 1), _delay(1), _flow(gi, 2), _repeat(g, 2)]
    gi = _u()
    iu, ip = _get_ip()
    out += [ip, _if_is(iu, host, gi), _flow(gi, 1),
            _notify(f"❌ {name}: Proxy NICHT aktiv", f"Instagram NICHT geöffnet. Erwartet {host}, ist: ", iu),
            _exit(f"{name}: proxy {host} NOT active -- Instagram not opened."), _flow(gi, 2)]
    out += _country_checks(name)
    # 11.10, explicit instruction (chat): B9 ran this far clean (proxy up, VPN on the right
    # container -- confirmed live) but then never actually opened Instagram, 7/7 live attempts,
    # on a device that otherwise behaves identically to every other container. The only
    # difference between the Crane switch and the open-app call was zero delay between them;
    # a short pause gives Crane a beat to finish switching before openapp fires.
    out += [ig_switch, _delay(2), open_ig, _notify(f"✅ {name}: Proxy aktiv", f"IP {host} · Land US · DNS US")]
    return _stable_uuids(out, name, crane + [open_ig])


def _host(ip: str) -> str:
    """"mobile@<lan ip>", or an ~/.ssh/config alias as-is (phone C: "phone-c", reached over
    USB through the MacBook because its WLAN stays off)."""
    return f"mobile@{ip}" if re.fullmatch(r"[0-9.]+", ip) else ip


def _ssh(ip: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-n", "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes",
                           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30", _host(ip), cmd],
                          capture_output=True, text=True, timeout=timeout)


def _scp(src: str, dst: str) -> bool:
    return subprocess.run(["scp", "-q", "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes",
                           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30", src, dst], capture_output=True, timeout=60).returncode == 0


def _pull(ip: str, workdir: str) -> str:
    db = os.path.join(workdir, "Shortcuts.sqlite")
    if not _scp(f"{_host(ip)}:{DB_DIR}/Shortcuts.sqlite", db):
        raise RuntimeError(f"could not copy the Shortcuts DB from {ip}")
    for ext in ("-wal", "-shm"):
        _scp(f"{_host(ip)}:{DB_DIR}/Shortcuts.sqlite{ext}", db + ext)
    return db


def airtable_hosts() -> dict:
    """Container name -> proxy host, from the Reels base's Containers table."""
    from adb_bot.automation import phone_dashboard
    return {c["container"]: c["proxy"]["host"] for ph in phone_dashboard.load() for c in ph["containers"]
            if c["proxy"].get("host")}


def sync(dry_run: bool = False) -> dict:
    """Rewrite every container Shortcut whose baked-in proxy IP differs from Airtable.
    Returns {phone ip: [changed shortcut names]}. Phones with nothing to change are not touched."""
    hosts = airtable_hosts()
    changed: dict = {}
    for rec_id, phone in iphone_farm_config.load().items():
        ip = phone.get("ssh") or phone["ip"]
        # 13.10: a phone that is off / rebooted (no sshd) must not stall the whole run (A took the
        # service down on 11.10 -- the timer stayed off, G/H/I were never hardened)
        try:
            if _ssh(ip, "true", timeout=20).returncode != 0:
                log.warning("%s: unreachable, skipped", ip)
                continue
        except Exception:                                   # noqa: BLE001
            log.warning("%s: unreachable (timeout), skipped", ip)
            continue
        with tempfile.TemporaryDirectory() as wd:
            try:
                db = _pull(ip, wd)
            except Exception as exc:
                log.warning("%s: %s", ip, exc)
                continue
            con = sqlite3.connect(db)
            todo = []
            for pk, name in con.execute("select Z_PK, ZNAME from ZSHORTCUT"):
                if not name or not _NAME_RE.match(name) or name not in hosts:
                    continue
                row = con.execute("select ZDATA from ZSHORTCUTACTIONS where ZSHORTCUT=?", (pk,)).fetchone()
                if not row or not row[0]:
                    continue
                actions = plistlib.loads(row[0])
                if expected_host(actions) != hosts[name] or not is_current(actions):
                    todo.append((pk, name, actions))
            if not todo:
                con.close()
                continue
            changed[ip] = [n for _, n, _ in todo]
            log.info("%s: rebuilding (proxy changed in Airtable or older layout) %s", ip, ", ".join(changed[ip]))
            if dry_run:
                con.close()
                continue
            # re-pull with the Shortcuts processes stopped, so nothing writes between our copy and the push
            con.close()
            _ssh(ip, f"{KILL}; sleep 1")
            db = _pull(ip, wd)
            con = sqlite3.connect(db)
            for pk, name, _ in todo:
                actions = plistlib.loads(con.execute("select ZDATA from ZSHORTCUTACTIONS where ZSHORTCUT=?", (pk,)).fetchone()[0])
                new = rebuild(actions, name, hosts[name])
                con.execute("update ZSHORTCUTACTIONS set ZDATA=? where ZSHORTCUT=?", (plistlib.dumps(new, fmt=plistlib.FMT_BINARY), pk))
                con.execute("update ZSHORTCUT set ZACTIONCOUNT=?, ZMODIFICATIONDATE=? where Z_PK=?",
                            (len(new), time.time() - CORE_DATA_EPOCH, pk))
            con.commit()
            con.execute("pragma wal_checkpoint(TRUNCATE)")
            ok = con.execute("pragma integrity_check").fetchone() == ("ok",)
            con.close()
            if not ok:
                log.error("%s: patched DB failed its integrity check -- not pushed", ip)
                continue
            _ssh(ip, f"{KILL}; rm -f {DB_DIR}/Shortcuts.sqlite-wal {DB_DIR}/Shortcuts.sqlite-shm")
            if not _scp(db, f"{_host(ip)}:{DB_DIR}/Shortcuts.sqlite"):
                log.error("%s: could not push the patched DB", ip)
    return changed


# --------------------------------------------------------------------------- Shadowrocket itself

SR_GROUP_ID = "group.com.liguangming.Shadowrocket"
CRANE_PREFS = "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist"


def _sr_group_dir(ip: str) -> str | None:
    r = _ssh(ip, "for d in /var/mobile/Containers/Shared/AppGroup/*/; do "
                 f"grep -q -a '{SR_GROUP_ID}' \"$d/.com.apple.mobile_container_manager.metadata.plist\" 2>/dev/null "
                 "&& echo $d && break; done")
    d = r.stdout.strip().rstrip("/")
    return d or None


def _patched_server_manager(template: bytes, host: str, port: str, user: str, password: str) -> tuple[bytes, str] | None:
    """Clone the container's own ServerManager with a new host/port (and login, if given).
    iphone_farm_proxies.build_server_manager hard-codes the object positions of an HTTP
    server entry; only use them after checking they really hold host/port here, else None."""
    import io
    d = plistlib.load(io.BytesIO(template), fmt=plistlib.FMT_BINARY)
    objs = d.get("$objects", [])
    try:
        ok = (re.fullmatch(r"[0-9A-Za-z.\-]+", str(objs[26])) and str(objs[24]).isdigit()
              and isinstance(objs[30], dict) and "NS.string" in objs[30])
    except (IndexError, TypeError):
        ok = False
    if not ok:
        return None
    objs[24], objs[26] = str(port), host
    if user:
        objs[29] = user
    if password:
        objs[28] = password
    new_uuid = _u()
    objs[30]["NS.string"] = new_uuid
    out = io.BytesIO()
    plistlib.dump(d, out, fmt=plistlib.FMT_BINARY)
    return out.getvalue(), new_uuid


def airtable_proxies() -> dict:
    """Container name -> {host, port, user, password} from the Containers table."""
    from adb_bot.automation import phone_dashboard
    return {c["container"]: c["proxy"] for ph in phone_dashboard.load() for c in ph["containers"]
            if c["proxy"].get("host") and c["proxy"].get("port")}


def sync_shadowrocket(dry_run: bool = False) -> dict:
    """Write Airtable's proxy into each "<name> Proxy" Shadowrocket container whose selected
    server differs. Keeps the container's other prefs (Global Routing etc.); stops Shadowrocket
    and cfprefsd first so the app re-reads the files instead of writing its cached copy back.
    The running tunnel keeps the old server until the next Shortcut run reconnects it."""
    wanted = airtable_proxies()
    changed: dict = {}
    for rec_id, phone in iphone_farm_config.load().items():
        ip = phone.get("ssh") or phone["ip"]
        group = _sr_group_dir(ip)
        if not group:
            log.warning("%s: Shadowrocket app group not found", ip)
            continue
        with tempfile.TemporaryDirectory() as wd:
            crane = os.path.join(wd, "crane.plist")
            if not _scp(f"{_host(ip)}:{CRANE_PREFS}", crane):
                continue
            containers = plistlib.load(open(crane, "rb"))["appSettings_com.liguangming.Shadowrocket"]["Containers"]
            todo = []
            for c in containers:
                name = c["name"].strip()
                if not name.endswith(" Proxy"):
                    continue
                short = name[:-len(" Proxy")]
                if short not in wanted:
                    continue
                want = wanted[short]
                base = f"{group}/Library/___Crane_Containers/{c['identifier']}"
                prefs_local = os.path.join(wd, f"{short}.plist")
                if not _scp(f"{_host(ip)}:{base}/Library/Preferences/{SR_GROUP_ID}.plist", prefs_local):
                    log.warning("%s %s: no Shadowrocket prefs in its container yet -- skipped", ip, short)
                    continue
                prefs = plistlib.load(open(prefs_local, "rb"))
                if prefs.get(f"{SR_GROUP_ID.replace('Shadowrocket', '')}SelectedServerName") == f"{want['host']}:{want['port']}":
                    continue
                todo.append((short, base, prefs, prefs_local, want))
            if not todo:
                continue
            changed[ip] = [t[0] for t in todo]
            log.info("%s: Shadowrocket proxy differs from Airtable for %s", ip, ", ".join(changed[ip]))
            if dry_run:
                continue
            _ssh(ip, "killall Shadowrocket cfprefsd 2>/dev/null; sleep 1")
            for short, base, prefs, prefs_local, want in todo:
                sm_local = os.path.join(wd, f"{short}_ServerManager")
                if not _scp(f"{_host(ip)}:{base}/ServerManager", sm_local):
                    log.warning("%s %s: no ServerManager to clone -- skipped", ip, short)
                    continue
                res = _patched_server_manager(open(sm_local, "rb").read(), want["host"], want["port"],
                                              want.get("user", ""), want.get("password", ""))
                if res is None:
                    log.warning("%s %s: ServerManager layout not recognised -- skipped, fix by hand", ip, short)
                    continue
                sm_bytes, server_uuid = res
                open(sm_local, "wb").write(sm_bytes)
                key = SR_GROUP_ID.replace("Shadowrocket", "")
                prefs[f"{key}SelectedServerName"] = f"{want['host']}:{want['port']}"
                prefs[f"{key}SelectedServerUUID"] = server_uuid
                prefs[f"{key}GlobalRoutingMethod"] = "Proxy"
                plistlib.dump(prefs, open(prefs_local, "wb"), fmt=plistlib.FMT_BINARY)
                if _scp(sm_local, f"{_host(ip)}:{base}/ServerManager") and \
                        _scp(prefs_local, f"{_host(ip)}:{base}/Library/Preferences/{SR_GROUP_ID}.plist"):
                    log.info("%s %s: Shadowrocket now -> %s:%s", ip, short, want["host"], want["port"])
            _ssh(ip, "killall cfprefsd 2>/dev/null")
    return changed


# --------------------------------------------------------------------------- Shadowrocket rule lists

SAFE_RULE_COMMENT = "# farm: everything via the proxy, DNS remote (2026-10-04)"


def _sr_data_dir(ip: str) -> str | None:
    r = _ssh(ip, "ls -d /var/mobile/Containers/Data/Application/*/Library/Preferences/com.liguangming.Shadowrocket.plist")
    path = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None
    return path.rsplit("/Library/", 1)[0] if path else None


# XXTouch Elite checks its licence at drm.82flex.com (+ Havoc); through the US proxy it answers
# "Connection to server" / no licence (2026-10-08). These three -- and only these -- go DIRECT.
DIRECT_OK = ("82flex.com", "xxtou.ch", "havoc.app")
DIRECT_COMMENT = "# farm: XXTouch/Havoc licence checks go direct, Instagram stays on the proxy (2026-10-08)"
# iOS updates wipe the palera1n jailbreak and every tweak (Crane, AppSync, SSH ...): iPhone A, 2026-10-09.
# Automatic updates are switched off on each phone; this blocks the update servers as well.
UPDATE_BLOCK = ("mesu.apple.com", "gdmf.apple.com", "appldnld.apple.com", "updates.cdn-apple.com", "updates-http.cdn-apple.com")
UPDATE_COMMENT = "# farm: no iOS updates -- an update removes the jailbreak (2026-10-09)"


def _make_safe(db_path: str) -> int:
    """Rules = FINAL,PROXY + DOMAIN-SUFFIX DIRECT for DIRECT_OK + DOMAIN-SUFFIX REJECT for UPDATE_BLOCK,
    nothing else. Returns how many rules were changed (0 = already safe)."""
    con = sqlite3.connect(db_path)
    rows = list(con.execute("select name, value, option from config where section='rule'"))
    wanted = {(d, "DIRECT") for d in DIRECT_OK} | {(d, "REJECT") for d in UPDATE_BLOCK}
    have = {(r[1], r[2]) for r in rows if r[0] == "DOMAIN-SUFFIX"}
    extra = [r for r in rows if r[0] not in ("FINAL", "COMMENT") and not (r[0] == "DOMAIN-SUFFIX" and (r[1], r[2]) in wanted)]
    has_final = any(r[0] == "FINAL" and r[2] == "PROXY" for r in rows)
    missing = [w for w in sorted(wanted) if w not in have]
    if not extra and has_final and not missing:
        con.close()
        return 0
    con.execute("delete from config where section='rule'")
    now = int(__import__("time").time())
    rule = "insert into config(section,name,value,option,ext,remarks,created) values('rule',?,?,?,?,'',?)"
    con.execute(rule, ("FINAL", "", "PROXY", "", now - 100))
    con.execute(rule, ("COMMENT", SAFE_RULE_COMMENT, "0", "0", now))
    con.execute(rule, ("COMMENT", DIRECT_COMMENT, "0", "0", now + 10))
    for i, d in enumerate(DIRECT_OK):
        con.execute(rule, ("DOMAIN-SUFFIX", d, "DIRECT", "", now + 5 - i))
    con.execute(rule, ("COMMENT", UPDATE_COMMENT, "0", "0", now + 20))
    for i, d in enumerate(UPDATE_BLOCK):
        con.execute(rule, ("DOMAIN-SUFFIX", d, "REJECT", "", now + 15 - i))
    con.commit()
    assert con.execute("pragma integrity_check").fetchone() == ("ok",)
    con.execute("vacuum")
    con.close()
    return len(extra) + len(missing) + (0 if has_final else 1)


def secure_rules(dry_run: bool = False) -> dict:
    """Shadowrocket's stock config ("Config" routing) sends ~350 domains (apple.com, icloud.com,
    crashlytics.com, ...) DIRECT over the home line and resolves everything else through the
    system (Thai ISP) DNS for its GEOIP,CN rule -- on 2026-10-04 Instagram's sign-up showed
    Thailand/China although the exit IP was the US proxy. And the Global Routing switch can't be
    pinned (files say Proxy, the app under Crane still showed Config). So every container's rule
    list is cut to FINAL,PROXY: "Config" then routes exactly like "Proxy", DNS included.
    Backs up default.db (.bak-20261004) and moves the compiled rule cache aside so Shadowrocket
    rebuilds it. Returns {phone ip: [container ids changed]}."""
    changed: dict = {}
    for rec_id, phone in iphone_farm_config.load().items():
        ip = phone.get("ssh") or phone["ip"]
        # 13.10: a phone that is off / rebooted (no sshd) must not stall the whole run (A took the
        # service down on 11.10 -- the timer stayed off, G/H/I were never hardened)
        try:
            if _ssh(ip, "true", timeout=20).returncode != 0:
                log.warning("%s: unreachable, skipped", ip)
                continue
        except Exception:                                   # noqa: BLE001
            log.warning("%s: unreachable (timeout), skipped", ip)
            continue
        data, group = _sr_data_dir(ip), _sr_group_dir(ip)
        if not data or not group:
            log.warning("%s: Shadowrocket dirs not found", ip)
            continue
        r = _ssh(ip, f"ls '{data}/Library/___Crane_Containers'")
        ids = ["."] + [x for x in r.stdout.split() if x]
        todo = []
        with tempfile.TemporaryDirectory() as wd:
            for cid in ids:
                sub = "" if cid == "." else f"/Library/___Crane_Containers/{cid}"
                remote = f"{data}{sub}/Documents/Databases/default.db"
                local = os.path.join(wd, f"{cid.strip('.') or 'root'}.db")
                if not _scp(f"{_host(ip)}:{remote}", local):
                    continue                                   # container never opened Shadowrocket
                probe = os.path.join(wd, "probe.db")
                shutil.copy(local, probe)
                if _make_safe(probe):
                    todo.append((cid, remote, local, f"{group}{sub}"))
            changed[ip] = [c for c, *_ in todo]
            log.info("%s: %d of %d Shadowrocket configs unsafe", ip, len(todo), len(ids))
            if dry_run or not todo:
                continue
            _ssh(ip, "killall Shadowrocket 2>/dev/null; sleep 1")
            for cid, remote, local, gdir in todo:
                _ssh(ip, f"[ -e '{remote}.bak-20261004' ] || cp -p '{remote}' '{remote}.bak-20261004'")
                _scp(f"{_host(ip)}:{remote}", local)           # fresh copy with Shadowrocket stopped
                _make_safe(local)
                if not _scp(local, f"{_host(ip)}:{remote}.new"):
                    log.error("%s %s: upload failed", ip, cid)
                    continue
                _ssh(ip, f"mv '{remote}.new' '{remote}'; cd '{gdir}' 2>/dev/null && for f in default.db.rule rule.db rule.db-shm rule.db-wal;"
                         f" do [ -e $f ] && mv -f $f $f.bak-20261004; done; true")
    return changed


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry = "--dry-run" in sys.argv
    # Shadowrocket first: the Shortcut's IP check should only change once the proxy it checks for is in place
    print("shadowrocket:", sync_shadowrocket(dry_run=dry))
    # a container whose Shadowrocket was opened for the first time starts on the stock rule list
    print("rule lists:", secure_rules(dry_run=dry))
    print("shortcuts:", sync(dry_run=dry))
