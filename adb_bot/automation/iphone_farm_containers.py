"""Make a phone's Crane containers match its proxies in Airtable: one "<X>n IG" + "<X>n Proxy"
container pair per Containers row that HAS a proxy, nothing else.

    .venv/bin/python3 -m adb_bot.automation.iphone_farm_containers plan  iphone4
    .venv/bin/python3 -m adb_bot.automation.iphone_farm_containers apply iphone4 [--template iphone2]

Rene, 2026-10-08: "nur so viele Container, wie wir auch Proxys haben" -- containers without a proxy
must not exist on the phone (nobody may open them), and the Airtable rows go too.

What `apply` does on the phone (all over SSH, nothing tapped):
1. Crane prefs (com.opa334.craneprefs.plist): adds the Instagram + Shadowrocket app entries if
   Crane never saw them (fresh phone), appends missing "<n> IG" / "<n> Proxy" containers, and
   REMOVES containers of this phone's letter whose row has no proxy (only if no account is linked).
2. Each new "<n> Proxy" container gets Shadowrocket's files cloned from a working template phone
   (default iPhone B, container B1): ServerManager patched to the Airtable proxy, the group prefs
   (selected server + Global Routing = Proxy) and the rule DB (FINAL,PROXY + the XXTouch/Havoc
   DIRECT exceptions from 2026-10-08).
3. The container Shortcuts are separate: iphone_farm_shortcuts.sync() rewrites existing ones;
   new phones get theirs via `shortcuts` below (cloned from the template phone's DB).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time

from adb_bot.automation import iphone_farm_config, phone_dashboard
from adb_bot.automation.iphone_farm_shortcuts import SR_GROUP_ID

CRANE_PREFS = "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist"
IG, SR = "com.burbn.instagram", "com.liguangming.Shadowrocket"


def _dest(ref: str) -> tuple[str, dict]:
    from adb_bot.automation.iphone_farm_wda import resolve
    rec_id, p = resolve(ref)
    return (p.get("ssh") or f"mobile@{p['ip']}"), {**p, "rec_id": rec_id}


def ssh(dest: str, cmd: str, data: bytes | None = None, timeout: int = 90) -> bytes:
    args = ["ssh"] + ([] if data is not None else ["-n"]) + [
        "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=10", *iphone_farm_config.lan_ssh_opts(dest), dest, cmd]
    r = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{dest}: {cmd[:60]}...: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout


def put(dest: str, path: str, data: bytes):
    q = shlex.quote(path)
    ssh(dest, f"mkdir -p $(dirname {q}) && cat > {q}.tmp && mv {q}.tmp {q}", data)


def app_dirs(dest: str) -> dict:
    """{"sr_data", "sr_group"}: Shadowrocket's data container and app group on this phone."""
    out = ssh(dest, "for f in /var/mobile/Containers/Data/Application/*/.com.apple.mobile_container_manager.metadata.plist; "
                    f"do grep -q '{SR}' \"$f\" && echo data=$(dirname \"$f\"); done; "
                    f"for f in /var/mobile/Containers/Shared/AppGroup/*/.com.apple.mobile_container_manager.metadata.plist; "
                    f"do grep -q '{SR_GROUP_ID}' \"$f\" && echo group=$(dirname \"$f\"); done; true").decode()
    d = dict(line.split("=", 1) for line in out.split() if "=" in line)
    if "data" not in d or "group" not in d:
        raise RuntimeError(f"{dest}: Shadowrocket not found (open it once on the phone)")
    return {"sr_data": d["data"], "sr_group": d["group"]}


def airtable_rows(letter: str) -> list[dict]:
    """[{container, proxy{host,port,user,password} or {}, accounts, id}] of one phone letter."""
    rows = []
    for ph in phone_dashboard.load():
        for c in ph["containers"]:
            if re.fullmatch(fr"{letter}\d+", c["container"] or ""):
                rows.append(c)
    return sorted(rows, key=lambda c: int(c["container"][1:]))


def template(ref: str, name: str) -> dict:
    """Shadowrocket files of a working "<name> Proxy" container on the template phone."""
    dest, _ = _dest(ref)
    dirs = app_dirs(dest)
    crane = plistlib.loads(ssh(dest, f"cat {CRANE_PREFS}"))
    uuid = next(c["identifier"] for c in crane[f"appSettings_{SR}"]["Containers"] if c["name"].strip() == f"{name} Proxy")
    g = f"{dirs['sr_group']}/Library/___Crane_Containers/{uuid}"
    a = f"{dirs['sr_data']}/Library/___Crane_Containers/{uuid}"
    t = {"server_manager": ssh(dest, f"cat {g}/ServerManager"),
         "group_prefs": plistlib.loads(ssh(dest, f"cat {g}/Library/Preferences/{SR_GROUP_ID}.plist")),
         "rules_db": ssh(dest, f"cat {a}/Documents/Databases/default.db")}
    with tempfile.NamedTemporaryFile(suffix=".db") as f:            # make sure the template has the farm rules
        f.write(t["rules_db"]); f.flush()
        con = sqlite3.connect(f.name)
        rules = {(r[0], r[1], r[2]) for r in con.execute("select name, value, option from config where section='rule'")}
        con.close()
    if ("FINAL", "", "PROXY") not in rules or ("DOMAIN-SUFFIX", "82flex.com", "DIRECT") not in rules:
        raise RuntimeError(f"template {ref} {name}: rule DB lacks FINAL,PROXY or the XXTouch DIRECT rule")
    return t


def patch_server_manager(template: bytes, host: str, port: str, user: str, password: str) -> tuple[bytes, str]:
    """Clone a ServerManager (NSKeyedArchiver) with one HTTP server swapped to host/port/login.
    Finds the server entry through its own key map (host/port/user/password/uuid -> object index)
    instead of fixed positions -- the B1 template has a "Subscribe" entry in front, which shifts
    every index compared with what iphone_farm_shortcuts._patched_server_manager expects."""
    import io, uuid as uuidlib
    d = plistlib.load(io.BytesIO(template), fmt=plistlib.FMT_BINARY)
    objs = d["$objects"]
    entry = next(o for o in objs if isinstance(o, dict) and "host" in o and "port" in o and "uuid" in o
                 and re.fullmatch(r"[0-9.]+", str(objs[o["host"].data])))
    idx = {k: entry[k].data for k in ("host", "port", "user", "password", "uuid")}
    objs[idx["host"]], objs[idx["port"]] = host, str(port)
    objs[idx["user"]], objs[idx["password"]] = user, password
    new = str(uuidlib.uuid4()).upper()
    objs[idx["uuid"]]["NS.string"] = new
    out = io.BytesIO()
    plistlib.dump(d, out, fmt=plistlib.FMT_BINARY)
    return out.getvalue(), new


def plan(ref: str) -> dict:
    dest, p = _dest(ref)
    letter = None
    rows_all = phone_dashboard.load()
    for ph in rows_all:
        if ph["id"] == p["rec_id"]:
            names = [c["container"] for c in ph["containers"] if c["container"]]
            letter = names[0][0] if names else None
    if not letter:
        raise RuntimeError(f"{ref}: no containers in Airtable")
    rows = airtable_rows(letter)
    want = [c["container"] for c in rows if c["proxy"].get("host")]
    drop = [c["container"] for c in rows if not c["proxy"].get("host")]
    linked = [c["container"] for c in rows if not c["proxy"].get("host") and c["accounts"]]
    crane = plistlib.loads(ssh(dest, f"cat {CRANE_PREFS}"))
    have = {app: {c["name"].strip(): c["identifier"] for c in crane.get(f"appSettings_{app}", {}).get("Containers", [])}
            for app in (IG, SR)}
    no_files = []
    if have[SR]:
        grp = app_dirs(dest)["sr_group"]
        present = set(ssh(dest, f"cd {grp}/Library/___Crane_Containers 2>/dev/null && ls */ServerManager 2>/dev/null; true").decode().split())
        no_files = [n for n in want if f"{n} Proxy" in have[SR] and f"{have[SR][f'{n} Proxy']}/ServerManager" not in present]
    return {"dest": dest, "letter": letter, "rows": rows, "want": want, "drop": drop, "blocked": linked, "crane": crane,
            "missing": [n for n in want if f"{n} IG" not in have[IG] or f"{n} Proxy" not in have[SR] or n in no_files],
            "remove": [n for n in drop if n not in linked and (f"{n} IG" in have[IG] or f"{n} Proxy" in have[SR])]}


def apply(ref: str, template_ref: str = "iphone2", template_name: str = "B1", log=print) -> dict:
    pl = plan(ref)
    dest, crane = pl["dest"], pl["crane"]
    if pl["blocked"]:
        log(f"not removing {pl['blocked']}: an account is linked although the row has no proxy -- fix Airtable first")
    tpl = template(template_ref, template_name) if pl["missing"] else None
    dirs = app_dirs(dest)
    proxies = {c["container"]: c["proxy"] for c in pl["rows"]}
    # 1. crane prefs
    for app in (IG, SR):
        s = crane.setdefault(f"appSettings_{app}", {"Containers": [{"identifier": "DEFAULT", "name": ""}], "activeContainer": "DEFAULT"})
        s["Containers"] = [c for c in s["Containers"] if c["name"].strip()[:-len(" IG" if app == IG else " Proxy")] not in pl["remove"]]
        names = {c["name"].strip() for c in s["Containers"]}
        for n in pl["missing"]:
            label = f"{n} IG" if app == IG else f"{n} Proxy"
            if label not in names:
                s["Containers"].append({"identifier": str(__import__("uuid").uuid4()).upper(), "name": label})
    crane.setdefault("launchApplicationOnContainerSelectionEnabled", False)
    if pl["missing"]:          # new Shadowrocket containers: the app must not write its cached state back
        ssh(dest, "killall Shadowrocket 2>/dev/null; true")    # (Instagram is left alone -- a VA may be using it)
    ssh(dest, f"cp -p {CRANE_PREFS} {CRANE_PREFS}.bak-{time.strftime('%Y%m%d%H%M')} 2>/dev/null; true")
    put(dest, CRANE_PREFS, plistlib.dumps(crane, fmt=plistlib.FMT_XML))
    # 2. Shadowrocket files for the new proxy containers
    sr = {c["name"].strip(): c["identifier"] for c in crane[f"appSettings_{SR}"]["Containers"]}
    done = []
    for n in pl["missing"]:
        px, uuid = proxies[n], sr[f"{n} Proxy"]
        sm, server_uuid = patch_server_manager(tpl["server_manager"], px["host"], str(px["port"]), px.get("user", ""), px.get("password", ""))
        prefs = dict(tpl["group_prefs"])
        key = SR_GROUP_ID.replace("Shadowrocket", "")
        prefs[f"{key}SelectedServerName"] = f"{px['host']}:{px['port']}"
        prefs[f"{key}SelectedServerUUID"] = server_uuid
        prefs[f"{key}GlobalRoutingMethod"] = "Proxy"
        g = f"{dirs['sr_group']}/Library/___Crane_Containers/{uuid}"
        a = f"{dirs['sr_data']}/Library/___Crane_Containers/{uuid}"
        put(dest, f"{g}/ServerManager", sm)
        put(dest, f"{g}/Library/Preferences/{SR_GROUP_ID}.plist", plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY))
        put(dest, f"{a}/Documents/Databases/default.db", tpl["rules_db"])
        done.append(n)
    ssh(dest, "killall cfprefsd 2>/dev/null; true")
    log(f"{ref}: created {done or '-'}; removed {pl['remove'] or '-'}")
    return {"created": done, "removed": pl["remove"], "blocked": pl["blocked"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["plan", "apply", "shortcuts", "home"])
    ap.add_argument("phone")
    ap.add_argument("--template", default="iphone2")
    ap.add_argument("--template-container", default="B1")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        pl = plan(a.phone)
        print(f"letter {pl['letter']}: want {pl['want']}\n  missing on phone: {pl['missing']}\n  remove from phone: {pl['remove']}\n"
              f"  no proxy (Airtable rows to drop): {pl['drop']}\n  blocked (account linked, no proxy): {pl['blocked']}")
        return 0
    if a.cmd == "home":
        home_shortcuts(a.phone)
        return 0
    if a.cmd == "shortcuts":
        sync_shortcuts(a.phone)
        return 0
    apply(a.phone, a.template, a.template_container)
    return 0



DIRECT_DOMAINS = ("82flex.com", "xxtou.ch", "xxtouch.net", "xxtouch.app", "havoc.app")


def set_direct_rules(ref: str, domains=DIRECT_DOMAINS, log=print) -> list:
    """08.10 (Rene: "XXTouch musst du umleiten, ohne VPN durchlassen"): rewrite the rule section
    of every "<n> Proxy" container on the phone so the licence/update hosts go DIRECT and
    everything else stays on the proxy -- with FINAL,PROXY as the LAST rule. The earlier template
    had FINAL first (rowid 666) and the DIRECT rules after it, i.e. possibly never reached."""
    dest, _ = _dest(ref)
    dirs = app_dirs(dest)
    crane = plistlib.loads(ssh(dest, f"cat {CRANE_PREFS}"))
    base = f"{dirs['sr_data']}/Library/___Crane_Containers"
    done = []
    for c in crane.get(f"appSettings_{SR}", {}).get("Containers", []):
        if not c["name"].strip().endswith(" Proxy"):
            continue
        path = f"{base}/{c['identifier']}/Documents/Databases/default.db"
        try:
            raw = ssh(dest, f"cat {path}")
        except Exception:  # noqa: BLE001 -- never opened: ensure_rules() writes the template first
            continue
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(raw)
            tmp = f.name
        con = sqlite3.connect(tmp)
        con.execute("delete from config where section='rule'")
        now = int(time.time())
        rows = [("COMMENT", "# farm: XXTouch/Havoc licence + update hosts go DIRECT, Instagram stays on the proxy (2026-10-08)", "0", "0")]
        rows += [("DOMAIN-SUFFIX", d, "DIRECT", "") for d in domains]
        rows += [("COMMENT", "# farm: everything else via the proxy, DNS remote", "0", "0"), ("FINAL", "", "PROXY", "")]
        for i, (name, value, option, ext) in enumerate(rows):
            con.execute("insert into config(section,name,value,option,ext,remarks,created) values('rule',?,?,?,?,'',?)", (name, value, option, ext, now + i))
        con.commit()
        con.close()
        with open(tmp, "rb") as f:
            put(dest, path, f.read())
        os.unlink(tmp)
        done.append(c["name"].strip())
    log(f"{ref}: DIRECT rules {list(domains)} written to {done or '-'} (FINAL last)")
    return done


LICENCE_SKIP_HOSTS = ("82flex.com", "*.82flex.com", "xxtou.ch", "*.xxtou.ch", "xxtouch.net", "*.xxtouch.net",
                      "xxtouch.app", "*.xxtouch.app", "havoc.app", "*.havoc.app")
LICENCE_EXCLUDED_ROUTES = ("192.255.152.46/32",)          # drm.82flex.com (fixed IP; the rest sit on Cloudflare/AWS)


def set_licence_bypass(ref: str, log=print) -> list:
    """22.10 (Rene: "koennen wir die Lizenzpruefung anders umleiten, nicht durch den Proxy?"): Global Routing is
    "Proxy" on every container (rules are ignored there), so the DIRECT rules of set_direct_rules() never applied
    and XXTouch/CatVNC licence checks went through the US proxy. Shadowrocket honours two general settings in
    every mode: `skip-proxy` (hosts that bypass the proxy) and `tun-excluded-routes` (IP ranges kept out of the
    tunnel at system level). Add the licence hosts to both in every "<n> Proxy" container. Takes effect on the
    next VPN (re)connect."""
    dest, _ = _dest(ref)
    dirs = app_dirs(dest)
    crane = plistlib.loads(ssh(dest, f"cat {CRANE_PREFS}"))
    base = f"{dirs['sr_data']}/Library/___Crane_Containers"
    done = []
    for c in crane.get(f"appSettings_{SR}", {}).get("Containers", []):
        if not c["name"].strip().endswith(" Proxy"):
            continue
        path = f"{base}/{c['identifier']}/Documents/Databases/default.db"
        try:
            raw = ssh(dest, f"cat {path}")
        except Exception:  # noqa: BLE001
            continue
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(raw)
            tmp = f.name
        con = sqlite3.connect(tmp)
        changed = False
        try:
            con.execute("select 1 from config limit 1")
        except sqlite3.OperationalError as exc:               # 23.10 (C live): one container had no `config` table
            log(f"{ref}: {c['name']}: Shadowrocket DB has no config table ({exc}) -- skipped, needs a rebuild")
            con.close()
            os.unlink(tmp)
            continue
        for key, extra in (("skip-proxy", LICENCE_SKIP_HOSTS), ("tun-excluded-routes", LICENCE_EXCLUDED_ROUTES)):
            row = con.execute("select rowid, value from config where section='general' and name=?", (key,)).fetchone()
            cur = [x.strip() for x in (row[1] if row else "").split(",") if x.strip()]
            want = cur + [x for x in extra if x not in cur]
            if want != cur:
                val = ", ".join(want)
                if row:
                    con.execute("update config set value=? where rowid=?", (val, row[0]))
                else:
                    con.execute("insert into config(section,name,value,option,ext,remarks,created) values('general',?,?,'','','',?)", (key, val, int(time.time())))
                changed = True
        con.commit()
        con.close()
        if changed:
            with open(tmp, "rb") as f:
                put(dest, path, f.read())
            done.append(c["name"].strip())
        os.unlink(tmp)
    log(f"{ref}: licence bypass (skip-proxy + tun-excluded-routes) written to {done or '- (already there)'}")
    return done


# 2026-10-12, Rene: Shadowrocket Global Routing is ALWAYS "Proxy" -- never anything else. The old
# set_routing_mode() (which switched containers to "Config" on 09.10 and 11.10 without his OK and leaked DNS
# over the Thai line) is deleted on purpose; every container Shortcut sets Proxy itself before connecting.


def ensure_rules(ref: str, template_ref: str = "iphone2", template_name: str = "B1", log=print) -> list:
    """Every "<n> Proxy" container gets the farm rule DB (FINAL,PROXY + XXTouch/Havoc DIRECT) if it
    has none yet. A container Shadowrocket never opened has no data dir, and on first use it would
    start from the stock config (~350 domains DIRECT over the home line, see secure_rules)."""
    dest, _ = _dest(ref)
    dirs = app_dirs(dest)
    crane = plistlib.loads(ssh(dest, f"cat {CRANE_PREFS}"))
    base = f"{dirs['sr_data']}/Library/___Crane_Containers"
    present = set(ssh(dest, f"cd {base} 2>/dev/null && ls */Documents/Databases/default.db 2>/dev/null; true").decode().split())
    todo = [c for c in crane.get(f"appSettings_{SR}", {}).get("Containers", [])
            if c["name"].strip().endswith(" Proxy") and f"{c['identifier']}/Documents/Databases/default.db" not in present]
    if todo:
        db = template(template_ref, template_name)["rules_db"]
        for c in todo:
            put(dest, f"{base}/{c['identifier']}/Documents/Databases/default.db", db)
    log(f"{ref}: rule DB written for {[c['name'].strip() for c in todo] or '-'}")
    return [c["name"].strip() for c in todo]


# --------------------------------------------------------------------------- container Shortcuts

SC_DB = "/var/mobile/Library/Shortcuts/Shortcuts.sqlite"
SC_KILL = "killall Shortcuts ShortcutsUI BackgroundShortcutRunner siriactionsd 2>/dev/null; true"
_FARM = re.compile(r"^[A-Z]\d{1,3}$")


def _pull_db(dest: str, path: str) -> sqlite3.Connection:
    for suf in ("", "-wal", "-shm"):
        data = ssh(dest, f"cat {SC_DB}{suf} 2>/dev/null; true")
        with open(path + suf, "wb") as f:
            f.write(data)
    con = sqlite3.connect(path)
    # Core Data's derived-attribute triggers call functions only the app registers; no-ops here are
    # fine because those derived counts are recomputed by Shortcuts itself
    for fn in set(re.findall(r"NSCoreData\w+", " ".join(r[0] or "" for r in con.execute("select sql from sqlite_master where type='trigger'")))):
        con.create_function(fn, -1, lambda *a: None)
    con.execute("pragma wal_checkpoint(TRUNCATE)")
    return con


def _delete_shortcut(con, pk: int):
    for sql in ("delete from ZSHORTCUTACTIONS where ZSHORTCUT=?", "delete from ZSHORTCUTICON where ZWORKFLOW=?",
                "delete from Z_4SHORTCUTS where Z_5SHORTCUTS=?", "delete from ZACCESSRESOURCEPERMISSION where ZSHORTCUT=?",
                "delete from ZSHORTCUT where Z_PK=?"):
        con.execute(sql, (pk,))


def _add_shortcut(con, src_pk: int, name: str, host: str, sr: tuple, ig: tuple):
    """Clone shortcut src_pk as `name`: Crane switches pointed at this phone's "<name> Proxy" / "<name> IG"
    containers (sr/ig = (uuid, label)), then rebuilt with the proxy check for `host`."""
    import uuid as uuidlib
    from adb_bot.automation.iphone_farm_shortcuts import rebuild, CORE_DATA_EPOCH
    cols = [r[1] for r in con.execute("pragma table_info('ZSHORTCUT')")]
    row = dict(zip(cols, con.execute("select * from ZSHORTCUT where Z_PK=?", (src_pk,)).fetchone()))
    actions = plistlib.loads(con.execute("select ZDATA from ZSHORTCUTACTIONS where ZSHORTCUT=?", (src_pk,)).fetchone()[0])
    for a in actions:
        if a["WFWorkflowActionIdentifier"] == "com.opa334.CraneApplication.SetActiveCraneContainerIntent":
            uid, label = sr if "Shadowrocket" in str(a["WFWorkflowActionParameters"].get("app")) else ig
            a["WFWorkflowActionParameters"]["container"] = {"displayString": label, "identifier": uid}
    new = rebuild(actions, name, host)
    nxt = lambda t: (con.execute(f"select coalesce(max(Z_PK),0)+1 from {t}").fetchone()[0])
    pk_s, pk_a, pk_i = nxt("ZSHORTCUT"), nxt("ZSHORTCUTACTIONS"), nxt("ZSHORTCUTICON")
    now = time.time() - CORE_DATA_EPOCH
    row.update(Z_PK=pk_s, ZNAME=name, ZWORKFLOWID=str(uuidlib.uuid4()).upper(), ZACTIONS=pk_a, ZICON=pk_i,
               ZACTIONCOUNT=len(new), ZCREATIONDATE=now, ZMODIFICATIONDATE=now, ZRUNEVENTSCOUNT=0, ZLASTRUNEVENTDATE=None)
    con.execute(f"insert into ZSHORTCUT ({','.join(cols)}) values ({','.join('?' * len(cols))})", [row[c] for c in cols])
    con.execute("insert into ZSHORTCUTACTIONS (Z_PK, Z_ENT, Z_OPT, ZSHORTCUT, ZDATA) select ?, Z_ENT, 1, ?, ? from ZSHORTCUTACTIONS where ZSHORTCUT=?",
                (pk_a, pk_s, plistlib.dumps(new, fmt=plistlib.FMT_BINARY), src_pk))
    icols = [r[1] for r in con.execute("pragma table_info('ZSHORTCUTICON')")]
    irow = dict(zip(icols, con.execute("select * from ZSHORTCUTICON where Z_PK=?", (row["ZICON"] if False else
                    con.execute("select ZICON from ZSHORTCUT where Z_PK=?", (src_pk,)).fetchone()[0],)).fetchone()))
    irow.update(Z_PK=pk_i, ZWORKFLOW=pk_s)
    con.execute(f"insert into ZSHORTCUTICON ({','.join(icols)}) values ({','.join('?' * len(icols))})", [irow[c] for c in icols])
    root = con.execute("select Z_PK from ZCOLLECTION where ZIDENTIFIER='Root'").fetchone()[0]
    order = con.execute("select coalesce(max(Z_FOK_5SHORTCUTS),0)+1024 from Z_4SHORTCUTS where Z_4PARENTS1=?", (root,)).fetchone()[0]
    con.execute("insert into Z_4SHORTCUTS (Z_4PARENTS1, Z_5SHORTCUTS, Z_FOK_5SHORTCUTS) values (?,?,?)", (root, pk_s, order))
    for ent, t in (("Shortcut", "ZSHORTCUT"), ("ShortcutActions", "ZSHORTCUTACTIONS"), ("ShortcutIcon", "ZSHORTCUTICON")):
        con.execute("update Z_PRIMARYKEY set Z_MAX=(select max(Z_PK) from " + t + ") where Z_NAME=?", (ent,))


def sync_shortcuts(ref: str, template_ref: str = "iphone3", log=print) -> dict:
    """Container Shortcuts on `ref` == its Crane containers with a proxy: removes farm Shortcuts
    whose container is gone, adds missing ones (cloned from the template phone's first farm
    Shortcut). A phone whose Shortcuts DB was never set up (no Root collection) gets the template
    phone's whole DB as a starting point, minus the template's own farm Shortcuts."""
    pl = plan(ref)
    dest = pl["dest"]
    proxies = {c["container"]: c["proxy"]["host"] for c in pl["rows"] if c["proxy"].get("host")}
    crane = plistlib.loads(ssh(dest, f"cat {CRANE_PREFS}"))
    have = {app: {c["name"].strip(): c["identifier"] for c in crane.get(f"appSettings_{app}", {}).get("Containers", [])} for app in (IG, SR)}
    with tempfile.TemporaryDirectory() as wd:
        con = _pull_db(dest, f"{wd}/t.sqlite")
        try:
            fresh = not con.execute("select count(*) from ZCOLLECTION where ZIDENTIFIER='Root'").fetchone()[0]
            farm = {n: pk for pk, n in con.execute("select Z_PK, ZNAME from ZSHORTCUT where ZTOMBSTONED=0") if n and _FARM.match(n)}
        except sqlite3.OperationalError:
            # 12.10: Shortcuts.app was never opened at all on I -- the pulled file has no
            # schema yet (not even ZCOLLECTION), unlike G's case (app opened once, Root
            # collection exists, just no farm shortcuts of its own). Same remedy either way.
            fresh, farm = True, {}
        if fresh or not farm:
            # 12.10: a brand-new phone already has a Root collection (Apple's own stock
            # Shortcuts -- "Shazam-Kurzbefehl" etc.) the moment the Shortcuts app is ever
            # opened, so `fresh` alone missed this case and raised "no farm Shortcut to
            # clone from" on G/H/I. No farm shortcut of its own -> always borrow the
            # template's, same as the true-fresh path.
            con.close()
            tdest, _ = _dest(template_ref)
            con = _pull_db(tdest, f"{wd}/t.sqlite")
            farm = {n: pk for pk, n in con.execute("select Z_PK, ZNAME from ZSHORTCUT where ZTOMBSTONED=0") if n and _FARM.match(n)}
            fresh = True
        src = farm[sorted(farm, key=lambda n: (n[0], int(n[1:])))[0]] if farm else None
        removed = [n for n in farm if (fresh and not n.startswith(pl["letter"])) or (n.startswith(pl["letter"]) and n not in proxies)]
        added = [n for n in sorted(proxies, key=lambda n: int(n[1:])) if n not in farm or n in removed]
        if src is None:
            raise RuntimeError("no farm Shortcut to clone from")
        for n in added:
            _add_shortcut(con, src, n, proxies[n], (have[SR][f"{n} Proxy"], f"{n} Proxy"), (have[IG][f"{n} IG"], f"{n} IG"))
        for n in removed:
            _delete_shortcut(con, farm[n])
        con.commit()
        ok = con.execute("pragma integrity_check").fetchone() == ("ok",)
        con.execute("pragma wal_checkpoint(TRUNCATE)")
        con.close()
        if not ok:
            raise RuntimeError("patched Shortcuts DB failed its integrity check -- not pushed")
        if not added and not removed:
            log(f"{ref}: Shortcuts already match"); return {"added": [], "removed": []}
        ssh(dest, f"{SC_KILL}; cp -p {SC_DB} {SC_DB}.bak-{time.strftime('%Y%m%d%H%M')}; sleep 1")
        ssh(dest, f"{SC_KILL}; rm -f {SC_DB}-wal {SC_DB}-shm")
        put(dest, SC_DB, open(f"{wd}/t.sqlite", "rb").read())
        # a Shortcuts daemon that was still up can write its old pages back (A14-A25 came back on 10-08):
        # stop everything once more and re-read the DB to make sure the change stuck
        ssh(dest, f"{SC_KILL}; sleep 2")
    log(f"{ref}: Shortcuts added {[n for n in added if n not in removed] or '-'}; removed {[n for n in removed if n not in added] or '-'}")
    return {"added": added, "removed": removed}



# --------------------------------------------------------------------------- home-screen folder "IG shortcuts"

ICONSTATE = "/var/mobile/Library/SpringBoard/IconState.plist"
WEBCLIPS = "/var/mobile/Library/WebClips"
FOLDER_NAME = "IG shortcuts"


def _farm_shortcuts(dest: str, wd: str) -> dict:
    con = _pull_db(dest, f"{wd}/h.sqlite")
    out = {n: wid for n, wid in con.execute("select ZNAME, ZWORKFLOWID from ZSHORTCUT where ZTOMBSTONED=0") if n and _FARM.match(n)}
    con.close()
    return out


def home_shortcuts(ref: str, template_ref: str = "iphone1", log=print) -> dict:
    """One home-screen icon per container Shortcut, all in a folder "IG shortcuts" (9 per page,
    sorted A1, A2, ...), like Rene's hand-made one on A/B: tap D4 -> Shortcut D4 runs. Icons of
    Shortcuts that no longer exist are removed. Ends with a respring (SpringBoard re-reads its
    icon layout only at start; the VPN drops -- run a container Shortcut afterwards)."""
    import io, tarfile, uuid as uuidlib
    dest, _ = _dest(ref)
    tdest, _ = _dest(template_ref)
    with tempfile.TemporaryDirectory() as wd:
        shortcuts = _farm_shortcuts(dest, wd)
        # template web clip (icon files + Info.plist layout) from the template phone
        tclips = ssh(tdest, f"ls {WEBCLIPS}").decode().split()
        tdir = next(d for d in tclips if d.endswith(".webclip"))
        ttar = tarfile.open(fileobj=io.BytesIO(ssh(tdest, f"cd {WEBCLIPS} && tar cf - {tdir}")))
        tfiles = {os.path.basename(m.name): ttar.extractfile(m).read() for m in ttar.getmembers() if m.isfile()}
        tinfo = plistlib.loads(tfiles["Info.plist"])
        # existing clips on the target
        have = {}
        listing = [d for d in ssh(dest, f"ls {WEBCLIPS} 2>/dev/null; true").decode().split() if d.endswith(".webclip")]
        for d in listing:
            info = plistlib.loads(ssh(dest, f"cat {WEBCLIPS}/{d}/Info.plist"))
            if info.get("ApplicationBundleIdentifier") == "com.apple.shortcuts" and _FARM.match(str(info.get("Title", ""))):
                have[info["Title"]] = (d, info.get("ShortcutIdentifier"))
        stale = [t for t, (d, sid) in have.items() if shortcuts.get(t) != sid]
        for t in stale:
            ssh(dest, f"rm -rf {WEBCLIPS}/{have[t][0]}")
            have.pop(t)
        for name, wid in shortcuts.items():
            if name in have:
                continue
            d = uuidlib.uuid4().hex.upper() + ".webclip"
            info = dict(tinfo, Title=name, ShortcutIdentifier=wid,
                        URL=f"shortcuts://x-callback-url/run-shortcut?name={name}&id={wid}&source=homescreen")
            for fname, data in tfiles.items():
                put(dest, f"{WEBCLIPS}/{d}/{fname}", plistlib.dumps(info, fmt=plistlib.FMT_BINARY) if fname == "Info.plist" else data)
            have[name] = (d, wid)
        # icon layout: one folder with all farm clips, 9 per page
        ids = [have[n][0][:-len(".webclip")] for n in sorted(have, key=lambda n: (n[0], int(n[1:])))]
        state = plistlib.loads(ssh(dest, f"cat {ICONSTATE}"))
        tstate = plistlib.loads(ssh(tdest, f"cat {ICONSTATE}"))
        def find(x):
            if isinstance(x, dict):
                if x.get("listType") == "folder" and x.get("displayName") == FOLDER_NAME:
                    return x
                for v in x.values():
                    r = find(v)
                    if r: return r
            elif isinstance(x, list):
                for v in x:
                    r = find(v)
                    if r: return r
        all_clip_ids = {d[:-len(".webclip")] for d in listing} | set(ids)
        def strip(lst):                      # farm clips must only live inside the folder
            return [i for i in lst if not (isinstance(i, str) and i in all_clip_ids)]
        state["iconLists"] = [strip(page) for page in state["iconLists"]]
        folder = find(state)
        if folder is None:
            folder = {k: v for k, v in find(tstate).items()}
            folder["uniqueIdentifier"] = str(uuidlib.uuid4()).upper()
            folder["listUniqueIdentifiers"] = []
            state["iconLists"][0].append(folder)
        pages = [ids[i:i + 9] for i in range(0, len(ids), 9)] or [[]]
        folder["iconLists"] = pages
        folder["listUniqueIdentifiers"] = [str(uuidlib.uuid4()).upper() for _ in pages]
        ssh(dest, f"cp -p {ICONSTATE} {ICONSTATE}.bak-{time.strftime('%Y%m%d%H%M')}")
        put(dest, ICONSTATE, plistlib.dumps(state, fmt=plistlib.FMT_BINARY))
        ssh(dest, "echo 123 | sudo -S killall -9 SpringBoard >/dev/null 2>&1; true")
    log(f"{ref}: folder '{FOLDER_NAME}' = {sorted(have, key=lambda n: int(n[1:]))}; removed {stale or '-'}")
    return {"icons": sorted(have), "removed": stale}


if __name__ == "__main__":
    sys.exit(main())
