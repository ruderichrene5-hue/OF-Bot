"""Faster container open (15.10, Rene: "warum dauert das 1,5 Minuten, der Kurzbefehl braucht von Hand 30 Sekunden").

`iphone_farm_config.open_container_cli` drives the phone from outside: about six separate SSH logins through
the Mac mini (3-10 s each just to connect), fixed sleeps, and the "Open in Shadowrocket?" sheets are answered
only after a probe has already failed, which repeats the whole connect. This version sends ONE script to the
phone (VPN switch, connect, wait for the exit IP, DNS check, locsim) in a single SSH session while WDA answers
the iOS sheets in parallel -- nothing else changes:

  the SAME gates as before, in the SAME order -- exit IP must be this container's proxy and the resolver must
  sit in the US, otherwise Instagram is NOT launched; then rnxspoof identity, then Instagram in its container.

Switch on with `_relay.container_open: "cli-fast"` in /etc/adbbot/iphone_farm.json; "cli" keeps the old route.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from adb_bot.automation import iphone_farm_config as C

CTL = "/var/jb/usr/local/bin/cranectl"

# runs on the phone (login shell zsh); @NAME@ / @HOST@ are filled in below
SCRIPT = r"""
/var/jb/var/mobile/bin/ig-close.sh >/dev/null 2>&1
uiopen 'shadowrocket://disconnect?autoclose=true'; sleep 2
@CTL@ --switch com.liguangming.Shadowrocket 'name:@NAME@ Proxy' || { echo SWITCH_FAILED; exit 3; }
sleep 1; uiopen 'shadowrocket://route/proxy'; sleep 1; uiopen 'shadowrocket://connect?autoclose=true'
ip=""; n=0
while [ $n -lt 50 ]; do
  ip=$(curl -s -m 5 -4 'http://ip-api.com/line/?fields=query' 2>/dev/null)
  [ "$ip" = '@HOST@' ] && break
  n=$((n+1)); [ $n -eq 25 ] && uiopen 'shadowrocket://connect?autoclose=true'
  sleep 1
done
dns=$(curl -s -L -m 12 -4 'http://edns.ip-api.com/json' 2>/dev/null | tr -d '\n ' | grep -o '"geo":"[^"]*' | cut -d'"' -f4)
echo "GATE ip=$ip dns=$dns"
if [ "$ip" = '@HOST@' ] && command -v locsim >/dev/null 2>&1; then
  ll=$(curl -s -m 8 -4 'http://ip-api.com/line/?fields=lat,lon' 2>/dev/null)
  lat=$(echo "$ll" | sed -n 1p); lon=$(echo "$ll" | sed -n 2p)
  [ -n "$lon" ] && locsim start -x $lat -y $lon -h 25 >/dev/null 2>&1 && echo "locsim $lat $lon"
fi
"""


def open_container(rec_id: str, name: str, log=None) -> tuple[bool, str]:
    """Dispatcher for `_relay.container_open`: "cli-fast" -> this module, anything else -> the step-by-step route."""
    if C.relay().get("container_open") == "cli-fast":
        return open_container_fast(rec_id, name, log)
    return C.open_container_cli(rec_id, name, log=log)


def open_container_fast(rec_id: str, name: str, log=None) -> tuple[bool, str]:
    log = log or (lambda *a, **k: None)
    if not re.fullmatch(r"[A-Z]\d{1,2}", name or ""):
        return False, f"bad container name {name!r}"
    from adb_bot.automation.iphone_farm_shortcuts import airtable_hosts
    host = airtable_hosts().get(name)
    if not host:
        return False, f"{name}: no proxy host in Airtable Containers"
    phone = C.load().get(rec_id) or {}
    if not (phone.get("ssh") or phone.get("ip")):
        return False, f"{name}: phone not configured"
    dest = phone.get("ssh") or f"mobile@{phone['ip']}"
    script = SCRIPT.replace("@CTL@", CTL).replace("@NAME@", name).replace("@HOST@", host)

    t0 = time.monotonic()
    proc = subprocess.Popen(
        ["ssh", "-i", C.SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
         *C.lan_ssh_opts(dest), dest, "zsh -s"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    proc.stdin.write(script)
    proc.stdin.close()
    buf: list[str] = []
    reader = threading.Thread(target=lambda: buf.append(proc.stdout.read()), daemon=True)
    reader.start()

    # iOS asks "Open in Shadowrocket?" for every URL opened from outside an app; they queue up behind the
    # script's uiopen calls, so answer them WHILE the script waits for the tunnel (not after, as before)
    p, answered = None, 0
    try:
        from adb_bot.automation.iphone_farm_wda import connect
        p = connect(rec_id, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log(f"{name}: WDA not usable for the Open sheets ({str(exc)[:70]}) -- the IP gate decides")
    end = time.monotonic() + 150
    try:
        while proc.poll() is None and time.monotonic() < end:
            if p is not None:
                try:
                    txt = p.alert_text() or ""
                    if "Shadowrocket" in txt or "Open in" in txt:
                        try:
                            p.alert_accept("Open")
                        except Exception:  # noqa: BLE001
                            p.alert_accept()
                        answered += 1
                        continue
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.7)
    finally:
        if p is not None:
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
    if proc.poll() is None:
        proc.kill()
        return False, f"{name}: container script did not finish in 150 s -- Instagram not opened"
    reader.join(5)
    out = "".join(buf)
    log(f"{name}: {answered} 'Open in Shadowrocket?' sheet(s) answered, script took {time.monotonic() - t0:.0f} s")
    if "SWITCH_FAILED" in out:
        return False, f"{name}: Crane could not switch Shadowrocket to '{name} Proxy'"
    m = re.search(r"GATE ip=(\S*) dns=(.*)", out)
    ip_now, dns = (m.group(1), m.group(2).strip()) if m else ("?", "?")
    if ip_now != host:
        return False, f"{name}: exit IP {ip_now} is not the container's proxy {host} -- Instagram not opened"
    if "unitedstates" not in dns.lower().replace(" ", ""):
        return False, f"{name}: resolver not in the US ({dns!r}) -- Instagram not opened"
    log(f"{name}: VPN up, exit {ip_now}, DNS {dns}")
    loc = re.search(r"locsim .*", out)
    if loc:
        log(f"{name}: {loc.group(0).strip()}")
    # 2026-10-19 Rene: rnxspoof komplett deaktiviert (verursachte HV-Welle). Call auskommentiert
    # bis Spoofer-Mechanismus neu bewertet. Dylib + plists sind von allen Phones entfernt;
    # dieser Call würde nur eine leere plist schreiben (dylib weg -> wirkt eh nicht), aber
    # Rene will 100% sicher dass nichts automatisch spoof-related passiert.
    # from adb_bot.automation import iphone_farm_spoof
    # iphone_farm_spoof.apply(rec_id, name)
    # log(f"{name}: rnxspoof identity applied for {name}")
    ok, msg = C._ssh(rec_id, f"{CTL} --switch com.burbn.instagram 'name:{name} IG' && uiopen --bundleid com.burbn.instagram && echo ok", timeout=60)
    if not ok:
        return False, f"{name}: Instagram launch failed: {msg}"
    return True, f"done (cli-fast) exit {ip_now}"
