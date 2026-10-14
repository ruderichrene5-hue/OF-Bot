"""The tweak stack a farm iPhone needs, as one repeatable tool: check what is on a
phone, install the parts that can be installed over SSH, and verify that
Instagram runs under Shadow.

    .venv/bin/python -m adb_bot.automation.iphone_farm_tweaks check iphone9
    .venv/bin/python -m adb_bot.automation.iphone_farm_tweaks install iphone9   # asks the sudo password
    .venv/bin/python -m adb_bot.automation.iphone_farm_tweaks verify iphone9
    .venv/bin/python -m adb_bot.automation.iphone_farm_tweaks check --all

What `install` does (idempotent, root via sudo, password from --password /
$IPHONE_SUDO_PASSWORD / prompt -- it is fed on stdin, never on a command line):
1. apt sources: jjolano (Shadow; unsigned -> Trusted) and udevs (LocSim);
2. apt-get install curl locsim;
3. Shadow 3.7.6 with the dependencies apt cannot resolve on rootless palera1n
   (the jjolano index points at GitHub with URLs apt mangles): RootBridge 1.0.2,
   HookKit 2.5.1 (3.x lacks the substrate module Shadow 3.7.6 needs) and Shadow
   itself, as debs from GitHub, cached on the server and pushed over SSH;
4. `shdw -g` (Shadow's ruleset of installed-package paths must be regenerated
   after every dpkg change, or new tweaks' files stay visible to Instagram);
5. Shadow switched on for Instagram with the hook set proven on F (14.10):
   essential + recommended hooks, none of the "dangerous" ones. Global/always-on
   stays off -- Shadow must only touch Instagram, never Shadowrocket or XXTouch.

What it does NOT do (by hand, once per phone): the jailbreak, Crane, XXTouch
Elite, CatVNC, ChargeLimiter (paid / licence / passwords), the App Store apps,
region + time zone (Settings; reported by `check`, 14.10: a phone on a US proxy
showed Asia/Bangkok), and device-identity spoofing per container (see the
13.10 research: IDFV / Meta device key are shared by all Crane containers).

`check` is read-only and safe on live phones. `install` reboots nothing, but a
SpringBoard crash after a tweak install has cost F its jailbreak once (10-12):
run it on a phone with no accounts first, and keep `verify` in the same sitting.
"""

from __future__ import annotations

import argparse
import getpass
import os
import plistlib
import shlex
import subprocess
import sys
import urllib.request

from adb_bot.automation import iphone_farm_config

SSH_KEY = iphone_farm_config.SSH_KEY
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
            "-o", "ServerAliveInterval=10"]
PATH = "export PATH=/var/jb/usr/bin:/var/jb/usr/local/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:$PATH; "

DEB_CACHE = os.environ.get("IPHONE_DEB_CACHE", "/var/lib/adbbot/iphone_debs")
# order matters: Shadow depends on both frameworks
DEBS = [
    ("me.jjolano.fmwk.rootbridge", "1.0.2",
     "https://github.com/jjolano/RootBridge/releases/download/v1.0.2/me.jjolano.fmwk.rootbridge_1.0.2_iphoneos-arm64-rootless.deb"),
    ("me.jjolano.fmwk.hookkit", "2.5.1-1",
     "https://github.com/jjolano/HookKit/releases/download/v2.5.1/me.jjolano.fmwk.hookkit_2.5.1-1_iphoneos-arm64.deb"),
    ("me.jjolano.shadow", "3.7.6",
     "https://github.com/jjolano/shadow/releases/download/v3.7.6/me.jjolano.shadow_3.7.6_iphoneos-arm64.deb"),
]
APT_SOURCES = {
    "jjolano": "Types: deb\nURIs: https://ios.jjolano.me/\nSuites: ./\nComponents:\nTrusted: yes\n",
    "udevs": "Types: deb\nURIs: https://udevsharold.github.io/repo/\nSuites: ./\nComponents:\n",
}
APT_PKGS = ["curl", "locsim"]

# (dpkg package, shown as, how it gets there)
PACKAGES = [
    ("openssh-server", "OpenSSH", "hand: palera1n bootstrap"),
    ("com.opa334.crane", "Crane", "hand: Havoc (paid)"),
    ("ch.xxtou.xxtouch", "XXTouch Elite", "hand: Havoc (licence)"),
    ("com.catvnc.server", "CatVNC", "hand"),
    ("chaoge.chargelimiter", "ChargeLimiter", "hand: + aldente.conf 80/40"),
    ("ai.akemi.appsyncunified", "AppSync Unified", "iphone_farm_wda prepare-phone"),
    ("curl", "curl", "install: apt"),
    ("locsim", "LocSim", "install: apt (udevs)"),
    ("me.jjolano.fmwk.rootbridge", "RootBridge", "install: deb"),
    ("me.jjolano.fmwk.hookkit", "HookKit 2.5.x", "install: deb"),
    ("me.jjolano.shadow", "Shadow 3.7.6", "install: deb"),
]
APPS = {"Instagram": "Instagram.app", "Shadowrocket": "Shadowrocket.app"}

IG = "com.burbn.instagram"
SHADOW_PREFS = "/var/mobile/Library/Preferences/me.jjolano.shadow.plist"   # libSandy path, same on rootless
# the profile Instagram runs under on F since 14.10 (Shadow's essential + recommended hooks)
SHADOW_IG_PROFILE = {
    "App_Enabled": True, "HK_Library": "substrate",
    "Hook_Filesystem": True, "Hook_DynamicLibraries": True, "Hook_URLScheme": True, "Hook_EnvVars": True,
    "Hook_DeviceCheck": True, "Hook_Foundation": True, "Hook_FakeMac": False,
    "Hook_MachBootstrap": False, "Hook_SymLookup": False, "Hook_ObjCRuntime": False, "Hook_AntiDebugging": False,
    "Hook_Syscall": False, "Hook_HideApps": True,
    "Hook_Sandbox": False, "Hook_LowLevelC": False, "Hook_Memory": False, "Hook_TweakClasses": True,
    "Hook_DynamicLibrariesExtra": False,
}


# ---------------------------------------------------------------- ssh

def _resolve(ref: str) -> tuple[str, dict, str]:
    from adb_bot.automation.iphone_farm_wda import resolve
    rec_id, p = resolve(ref)
    return rec_id, p, (p.get("ssh") or f"mobile@{p['ip']}")


def _ssh(dest: str, cmd: str, data: bytes | None = None, timeout: int = 120, check: bool = True) -> bytes:
    args = ["ssh"] + ([] if data is not None else ["-n"]) + ["-i", SSH_KEY, *SSH_OPTS,
            *iphone_farm_config.lan_ssh_opts(dest), dest, PATH + cmd]
    r = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{dest}: {cmd[:70]}...: rc {r.returncode} {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout


def _sudo_script(dest: str, body: str, password: str, timeout: int = 600) -> str:
    """Run `body` with S() = sudo. The password is the first stdin line (never in argv / ps)."""
    cmd = ("read -r PW; S() { printf '%s\\n' \"$PW\" | sudo -S -p '' \"$@\"; }; S true </dev/null || exit 3; " + body)
    args = ["ssh", "-i", SSH_KEY, *SSH_OPTS, *iphone_farm_config.lan_ssh_opts(dest), dest, PATH + cmd]
    r = subprocess.run(args, input=(password + "\n").encode(), capture_output=True, timeout=timeout)
    if r.returncode == 3:
        raise SystemExit("sudo password rejected (the `mobile` password set after the jailbreak)")
    out = (r.stdout + r.stderr).decode(errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"rc {r.returncode}: {out.strip()[-400:]}")
    return out


# ---------------------------------------------------------------- check

def _dpkg_versions(dest: str) -> dict[str, str]:
    out = _ssh(dest, "dpkg-query -W -f '${Package} ${Version} ${Status}\\n' 2>/dev/null", check=False).decode()
    vers = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-1] == "installed":
            vers[parts[0]] = parts[1]
    return vers


def _shadow_prefs(dest: str) -> dict:
    raw = _ssh(dest, f"cat {SHADOW_PREFS} 2>/dev/null", check=False)
    try:
        return plistlib.loads(raw) if raw else {}
    except Exception:
        return {}


def _global_prefs(dest: str) -> dict:
    raw = _ssh(dest, "cat /var/mobile/Library/Preferences/.GlobalPreferences.plist 2>/dev/null", check=False)
    try:
        return plistlib.loads(raw) if raw else {}
    except Exception:
        return {}


def check(ref: str) -> tuple[bool, list[str]]:
    """Returns (everything the tool can fix is in place, report lines). Read-only."""
    rec_id, p, dest = _resolve(ref)
    name = p.get("stream") or ref
    lines, ok = [], True
    vers = _dpkg_versions(dest)
    if not vers:
        return False, [f"{name}: no dpkg answer over SSH ({dest}) -- phone off, not jailbroken or key missing"]

    for pkg, label, how in PACKAGES:
        v = vers.get(pkg)
        good = bool(v)
        if pkg == "me.jjolano.fmwk.hookkit" and v and not v.startswith("2.5"):
            good = False
            v = f"{v} (Shadow 3.7.6 needs 2.5.x)"
        if not good and how.startswith("install"):
            ok = False
        lines.append(f"  {'ok ' if good else '-- '} {label:16} {v or 'missing'}{'' if good else '   <- ' + how}")

    apps = _ssh(dest, "ls -d /var/containers/Bundle/Application/*/*.app 2>/dev/null", check=False).decode()
    for label, bundle in APPS.items():
        present = f"/{bundle}" in apps
        lines.append(f"  {'ok ' if present else '-- '} {label:16} {'installed' if present else 'missing   <- App Store, by hand'}")

    prefs = _shadow_prefs(dest)
    ig = prefs.get(IG, {})
    if "me.jjolano.shadow" in vers:
        on = bool(ig.get("App_Enabled"))
        if not on:
            ok = False
        diff = [k for k, v in SHADOW_IG_PROFILE.items() if ig.get(k) != v]
        glob = bool(prefs.get("Global_Enabled"))
        lines.append(f"  {'ok ' if on and not diff and not glob else '-- '} Shadow->Instagram "
                     f"{'on' if on else 'OFF'}"
                     + (f", differs from F profile: {', '.join(diff)}" if on and diff else "")
                     + (", Global_Enabled is ON (must be off: would hook Shadowrocket/XXTouch too)" if glob else ""))
        rules = _ssh(dest, "stat -c %Y /var/jb/Library/Shadow/Rulesets/dpkgInstalled.plist /var/jb/var/lib/dpkg/status 2>/dev/null",
                     check=False).decode().split()
        if len(rules) == 2 and int(rules[0]) < int(rules[1]):
            ok = False
            lines.append("  --  Shadow ruleset    older than the dpkg status -> `shdw -g` (install does it)")

    home = _ssh(dest, "echo ~", check=False).decode().strip()
    tz = _ssh(dest, "readlink /var/db/timezone/localtime", check=False).decode().strip().replace("/var/db/timezone/zoneinfo/", "")
    gp = _global_prefs(dest)
    locale = gp.get("AppleLocale", "?")
    langs = ",".join(gp.get("AppleLanguages", [])[:2])
    us = tz.startswith("America/") and str(locale).endswith("US")
    lines.append(f"  {'ok ' if us else '!! '} region/time zone {locale} [{langs}] {tz or '?'}"
                 + ("" if us else "   <- Settings by hand: US time zone + en_US for US proxies"))
    lines.append(f"  i   home {home}  (rootless: .ssh/authorized_keys lives under this path)")
    return ok, [f"{name} ({dest}):"] + lines


# ---------------------------------------------------------------- install

def _cached_deb(pkg: str, url: str) -> str:
    os.makedirs(DEB_CACHE, exist_ok=True)
    path = os.path.join(DEB_CACHE, url.rsplit("/", 1)[1])
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        with urllib.request.urlopen(url, timeout=120) as r, open(path + ".part", "wb") as f:
            f.write(r.read())
        os.replace(path + ".part", path)
    return path


def install(ref: str, password: str, log=print) -> list[str]:
    rec_id, p, dest = _resolve(ref)
    name = p.get("stream") or ref
    done = []
    vers = _dpkg_versions(dest)
    if not vers:
        raise SystemExit(f"{name}: no dpkg answer over SSH -- phone off or not jailbroken")

    # 1+2: apt sources + packages
    src = ""
    for fname, text in APT_SOURCES.items():
        src += (f"[ -f /var/jb/etc/apt/sources.list.d/{fname}.sources ] || "
                f"S sh -c {shlex.quote(f'printf %s {shlex.quote(text)} > /var/jb/etc/apt/sources.list.d/{fname}.sources')}; ")
    missing = [x for x in APT_PKGS if x not in vers]
    if missing:
        log(f"{name}: apt-get install {' '.join(missing)} ...")
        out = _sudo_script(dest, src + "S apt-get update -qq >/dev/null 2>&1; "
                           f"DEBIAN_FRONTEND=noninteractive S apt-get install -y -qq {' '.join(missing)} 2>&1 | tail -3", password)
        done.append(f"apt: {' '.join(missing)} ({out.strip().splitlines()[-1][:80] if out.strip() else 'ok'})")
    else:
        _sudo_script(dest, src + "true", password)

    # 3: the Shadow debs
    todo = [(pkg, ver, url) for pkg, ver, url in DEBS if vers.get(pkg) != ver]
    if todo:
        names = []
        for pkg, ver, url in todo:
            path = _cached_deb(pkg, url)
            fname = os.path.basename(path)
            log(f"{name}: pushing {fname} ...")
            with open(path, "rb") as f:
                _ssh(dest, f"mkdir -p ~/debs && cat > ~/debs/{shlex.quote(fname)}", f.read(), timeout=300)
            names.append(f"~/debs/{shlex.quote(fname)}")
        log(f"{name}: dpkg -i {len(names)} deb(s) ...")
        # HookKit 3.x on the phone conflicts with 2.5.1 -> dpkg -i downgrades it (--force-downgrade)
        out = _sudo_script(dest, f"cd ~ && S dpkg -i --force-downgrade {' '.join(names)} 2>&1 | tail -4; "
                           "DEBIAN_FRONTEND=noninteractive S apt-get -f install -y -qq 2>&1 | tail -2; rm -rf ~/debs", password)
        done.append("debs: " + ", ".join(pkg for pkg, _, _ in todo))
        vers = _dpkg_versions(dest)
        bad = [pkg for pkg, ver, _ in DEBS if vers.get(pkg) != ver]
        if bad:
            raise RuntimeError(f"{name}: after dpkg -i still wrong/missing: {bad}\n{out[-600:]}")

    # 4: ruleset
    _sudo_script(dest, "S shdw -g 2>&1 | tail -1", password)
    done.append("shdw -g (ruleset regenerated)")

    # 5: Shadow on for Instagram, off globally; written as the file libSandy hands Shadow, then cfprefsd
    # dropped so its cache cannot write the old values back
    prefs = _shadow_prefs(dest)
    want = {**prefs, "Global_Enabled": False, IG: {**prefs.get(IG, {}), **SHADOW_IG_PROFILE}}
    if want != prefs:
        data = plistlib.dumps(want, fmt=plistlib.FMT_BINARY)
        _ssh(dest, f"cat > {SHADOW_PREFS}.tmp && mv {SHADOW_PREFS}.tmp {SHADOW_PREFS} && chmod 644 {SHADOW_PREFS}", data)
        _sudo_script(dest, "S killall -9 cfprefsd 2>/dev/null; killall Instagram 2>/dev/null; true", password)
        done.append("Shadow prefs: Instagram on, global off (Instagram closed so the next launch is hooked)")
    return done or ["nothing to do -- everything already in place"]


# ---------------------------------------------------------------- verify

def verify(ref: str, wait: int = 25) -> tuple[bool, str]:
    """Launch Instagram under Shadow and make sure it is still alive `wait` s later with no new crash
    log. Shadow leaves no trace inside the app we could read from outside, so 'runs and does not
    crash' is the check -- a jailbreak-detection test app is the stronger one (by hand, see research)."""
    rec_id, p, dest = _resolve(ref)
    name = p.get("stream") or ref
    prefs = _shadow_prefs(dest)
    if not prefs.get(IG, {}).get("App_Enabled"):
        return False, f"{name}: Shadow is not enabled for Instagram -- run install"
    # a dark/locked screen swallows uiopen (rc 0, no process). WDA wakes + launches where it runs
    # (F's VNC is not reachable from the server, so iphone_farm_config.wake() is the fallback only).
    before = _ssh(dest, "ls /var/mobile/Library/Logs/CrashReporter/ 2>/dev/null | grep -c -i instagram", check=False).decode().strip() or "0"
    _ssh(dest, "killall Instagram 2>/dev/null; true", check=False)
    if p.get("wda_port"):
        from adb_bot.automation import iphone_farm_wda
        with iphone_farm_wda.connect(ref, timeout=60) as w:
            if w.locked():
                w.unlock()
            w.launch(IG)
    else:
        woke, why = iphone_farm_config.wake(rec_id)
        if not woke:
            return False, f"{name}: could not wake the screen ({why})"
        _ssh(dest, f"uiopen --bundleid {IG}", check=False)
    out = _ssh(dest, f"sleep {wait}; "
                     f"ps ax | grep -c '[I]nstagram.app/Instagram'; "
                     "ls /var/mobile/Library/Logs/CrashReporter/ 2>/dev/null | grep -c -i instagram; "
                     "shdw /var/jb/usr/bin/ssh 2>&1 | tail -1", timeout=wait + 90, check=False).decode().strip().splitlines()
    running = out[-3] if len(out) >= 3 else "0"
    after = out[-2] if len(out) >= 2 else before
    rule = out[-1] if out else ""
    good = running.strip() == "1" and after.strip() == before.strip()
    return good, (f"{name}: Instagram {'alive' if running.strip() == '1' else 'NOT running'} after {wait}s, "
                  f"crash logs {before}->{after}, ruleset says ssh is {rule.strip() or '?'}")


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("check", help="what is on the phone (read-only)")
    s.add_argument("phone", nargs="?")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("install", help="apt + Shadow stack + Shadow on for Instagram (needs the sudo password)")
    s.add_argument("phone")
    s.add_argument("--password", help="default: $IPHONE_SUDO_PASSWORD or asked interactively")
    s.add_argument("--no-verify", action="store_true")
    s = sub.add_parser("verify", help="start Instagram under Shadow, confirm it survives")
    s.add_argument("phone")
    s.add_argument("--wait", type=int, default=25)
    a = ap.parse_args(argv)

    if a.cmd == "check":
        from adb_bot.automation.iphone_farm_wda import phones
        refs = list(phones()) if a.all or not a.phone else [a.phone]
        rc = 0
        for ref in refs:
            try:
                ok, lines = check(ref)
            except Exception as exc:
                ok, lines = False, [f"{ref}: {exc}"]
            rc |= 0 if ok else 1
            print("\n".join(lines) + ("" if ok else "\n  => install can fix the '--' lines marked install:"))
        return rc
    if a.cmd == "install":
        pw = a.password or os.environ.get("IPHONE_SUDO_PASSWORD") or getpass.getpass("sudo password of mobile: ")
        for line in install(a.phone, pw):
            print("  " + line)
        if not a.no_verify:
            ok, msg = verify(a.phone)
            print(("ok  " if ok else "!!  ") + msg)
            return 0 if ok else 1
        return 0
    if a.cmd == "verify":
        ok, msg = verify(a.phone, a.wait)
        print(("ok  " if ok else "!!  ") + msg)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
