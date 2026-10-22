"""rnxspoof: per-container device identity. The Objective-C tweak (iphone_farm/spoof/Tweak.m)
reads one profile plist on the phone; this module writes that plist with a stable identity
derived from the Crane container name, right before Instagram is launched (plugged into
open_container_cli). "Stable" means the same container -> same IDFV/UDID/serial every time,
so Instagram sees one consistent device per container.

Three entry points:
- deploy(ref, sudo_pw): push dylib + filter plist to a phone, sign with ldid, respring. One-off per phone.
- apply(ref, container): write /var/mobile/Library/Preferences/com.reynex.spoof.plist with the
  identity for that container, kill cfprefsd so the next launch reads it fresh. open_container_cli
  calls this between the IP gate and the IG launch.
- check(ref): is the tweak installed, loaded by SpringBoard, and signed?

Identities are deterministic (sha256(container-name + salt) -> UUIDs / IMEI / serial), so a
container's identity never drifts, never collides with another container, and is reproducible
if we lose the plist. The salt lives in /etc/adbbot/spoof.salt so identities differ between
different installs (dev vs prod) but stay stable within one.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shlex
import subprocess
import uuid
from pathlib import Path

from adb_bot.automation import iphone_farm_config

SSH_KEY = iphone_farm_config.SSH_KEY
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30"]
PATH_EXPORT = "export PATH=/var/jb/usr/bin:/var/jb/usr/local/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:$PATH; "

DYLIB_LOCAL = Path(__file__).resolve().parents[2] / "iphone_farm" / "spoof" / "rnxspoof.dylib"
PLIST_LOCAL = Path(__file__).resolve().parents[2] / "iphone_farm" / "spoof" / "rnxspoof.plist"
DYLIB_REMOTE = "/var/jb/Library/MobileSubstrate/DynamicLibraries/rnxspoof.dylib"
PLIST_REMOTE = "/var/jb/Library/MobileSubstrate/DynamicLibraries/rnxspoof.plist"
PROFILE_REMOTE = "/var/mobile/Library/Preferences/com.reynex.spoof.plist"
SALT_PATH = "/etc/adbbot/spoof.salt"

# Phone-side watcher (Option B: catches manual Crane switches that bypass fastopen).
WATCHER_LOCAL        = Path(__file__).resolve().parents[2] / "iphone_farm" / "spoof" / "rnxspoof_watcher.py"
WATCHER_LD_LOCAL     = Path(__file__).resolve().parents[2] / "iphone_farm" / "spoof" / "com.reynex.spoof.watcher.plist"
WATCHER_BIN_REMOTE   = "/var/jb/usr/local/bin/rnxspoof-watcher"
WATCHER_LD_REMOTE    = "/var/jb/Library/LaunchDaemons/com.reynex.spoof.watcher.plist"
WATCHER_SALT_REMOTE  = "/var/jb/etc/rnxspoof.salt"
WATCHER_LABEL        = "com.reynex.spoof.watcher"

# Which apps the tweak should hook. Keep this LIST SHORT: anything in it gets spoofed,
# anything NOT in it stays untouched (so Shadowrocket, XXTouch, Settings are safe).
SPOOFED_APPS = ["com.burbn.instagram"]


# ---------------------------------------------------------------- identity derivation

def _salt() -> str:
    try:
        return Path(SALT_PATH).read_text().strip()
    except FileNotFoundError:
        s = uuid.uuid4().hex
        Path(SALT_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(SALT_PATH).write_text(s + "\n")
        os.chmod(SALT_PATH, 0o600)
        return s


def _h(container: str, key: str, n: int = 32) -> str:
    """n hex chars from sha256(salt | container | key). Deterministic, per-container, per-field."""
    return hashlib.sha256(f"{_salt()}|{container}|{key}".encode()).hexdigest()[:n]


def _uuid_for(container: str, key: str) -> str:
    h = _h(container, key)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}".upper()


def _uuidv4_for(container: str, key: str) -> str:
    """Deterministic string in valid UUIDv4 shape (version bit = 4 at pos 14, variant bit in {8,9,a,b}
    at pos 19). 2026-10-17 (Rene: "sind das realistische daten"): the plain hex grouping used before
    produced UUIDs any anti-fraud lib that checks `version()` would flag immediately."""
    h = _h(container, key, 32)
    variant = f"{(int(h[16], 16) & 0x3) | 0x8:x}"           # force 8/9/a/b
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{variant}{h[17:20]}-{h[20:32]}".upper()


def _udid_for(container: str) -> str:
    """iOS 7+ UDID shape (25 chars: 8hex-16hex), e.g. "00008030-001A14940231802E"."""
    h = _h(container, "udid", 24)
    return f"{h[0:8]}-{h[8:24]}".upper()


def _serial_for(container: str) -> str:
    """Apple 12-char serial: 3-char plant prefix (F2L = Shenzhen Hon Hai) + 5-char base36 body +
    4-char model code (NQNG = one of the real iPhone X suffixes). Not registered, but passes shape
    checks that look for "starts with a known plant prefix + ends with 4 chars"."""
    body = _h(container, "serial", 20).upper()
    body = "".join(c for c in body if c.isalnum())[:5].ljust(5, "0")
    return f"F2L{body}NQNG"[:12]


_IPHONE_X_TAC = "35345908"                                   # real Apple TAC for iPhone X (A1865)


def _imei_for(container: str) -> str:
    """15-digit IMEI = 8-digit TAC + 6-digit serial + 1-digit Luhn checksum. Using iPhone X's real
    TAC (35345908) so the IMEI parses as "an iPhone X" at any carrier/validator."""
    body = _IPHONE_X_TAC + ("".join(c for c in _h(container, "imei", 24) if c.isdigit()) + "000000")[:6]
    # Luhn checksum on the first 14 digits
    total = 0
    for i, c in enumerate(body[::-1]):
        d = int(c)
        if i % 2 == 0:
            d *= 2
            if d > 9: d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


def identity_for(container: str) -> dict:
    """Deterministic identity for a Crane container name like "G3", "B7".
    2026-10-17: realistic shapes (UUIDv4 bits, iOS-7+ UDID, Apple-style serial, Luhn-valid IMEI with
    real iPhone X TAC). Still 100 % deterministic from (salt, container) so a given container never
    sees a different device."""
    return {
        "apps":   SPOOFED_APPS,
        "idfv":   _uuidv4_for(container, "idfv"),
        "idfa":   _uuidv4_for(container, "idfa"),
        "udid":   _udid_for(container),
        "serial": _serial_for(container),
        "imei":   _imei_for(container),
    }


# ---------------------------------------------------------------- ssh helpers

def _resolve(ref: str) -> tuple[str, dict, str]:
    from adb_bot.automation.iphone_farm_wda import resolve
    rec_id, p = resolve(ref)
    return rec_id, p, (p.get("ssh") or f"mobile@{p['ip']}")


def _ssh(dest: str, cmd: str, data: bytes | None = None, timeout: int = 60, check: bool = True) -> bytes:
    args = ["ssh"] + ([] if data is not None else ["-n"]) + ["-i", SSH_KEY, *SSH_OPTS,
            *iphone_farm_config.lan_ssh_opts(dest), dest, PATH_EXPORT + cmd]
    r = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{dest}: {cmd[:70]}: rc {r.returncode} {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout


def _sudo(dest: str, body: str, password: str, timeout: int = 300) -> str:
    cmd = ("read -r PW; S() { printf '%s\\n' \"$PW\" | sudo -S -p '' \"$@\"; }; S true </dev/null || exit 3; " + body)
    args = ["ssh", "-i", SSH_KEY, *SSH_OPTS, *iphone_farm_config.lan_ssh_opts(dest), dest, PATH_EXPORT + cmd]
    r = subprocess.run(args, input=(password + "\n").encode(), capture_output=True, timeout=timeout)
    if r.returncode == 3:
        raise SystemExit("sudo password rejected on the phone")
    out = (r.stdout + r.stderr).decode(errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"rc {r.returncode}: {out.strip()[-400:]}")
    return out


# ---------------------------------------------------------------- deploy (one-off)

def deploy(ref: str, password: str, log=print) -> list[str]:
    """Push dylib + filter plist to the phone, sign with ldid, drop into Substrate dir, respring."""
    if not DYLIB_LOCAL.exists():
        raise SystemExit(f"missing {DYLIB_LOCAL} -- build it on the Mac mini first (see iphone_farm/spoof/BUILD.md)")
    rec_id, p, dest = _resolve(ref)
    name = p.get("stream") or ref
    log(f"{name}: pushing dylib + plist ...")
    with open(DYLIB_LOCAL, "rb") as f:
        _ssh(dest, "mkdir -p ~/rnxspoof && cat > ~/rnxspoof/rnxspoof.dylib", f.read(), timeout=120)
    with open(PLIST_LOCAL, "rb") as f:
        _ssh(dest, "cat > ~/rnxspoof/rnxspoof.plist", f.read(), timeout=30)
    log(f"{name}: installing ldid if missing, signing, placing files, respring ...")
    out = _sudo(dest, "dpkg -s ldid >/dev/null 2>&1 || (S apt-get update -qq >/dev/null 2>&1; "
                      "DEBIAN_FRONTEND=noninteractive S apt-get install -y -qq ldid >/dev/null 2>&1); "
                      "ldid -S ~/rnxspoof/rnxspoof.dylib 2>&1 | tail -1; "
                      f"S mv ~/rnxspoof/rnxspoof.dylib {DYLIB_REMOTE}; "
                      f"S mv ~/rnxspoof/rnxspoof.plist {PLIST_REMOTE}; "
                      f"S chmod 644 {DYLIB_REMOTE} {PLIST_REMOTE}; "
                      "S killall -9 cfprefsd 2>/dev/null; sbreload 2>&1 | tail -1; echo done", password, timeout=300)
    log(f"{name}: {out.strip().splitlines()[-1] if out.strip() else 'done'}")
    return [f"deployed: {DYLIB_REMOTE}", f"filter:   {PLIST_REMOTE}"]


# ---------------------------------------------------------------- per-container profile write

_AIRTABLE_BASE = "appwk9LrgAhJv8iEH"
_T_CONTAINERS  = "tblyZXN7caKZZBaBj"
_T_ACCOUNTS    = "tbly0y1I4hNDlvh1K"


def _established_account(container: str) -> str | None:
    """Return the Username of an EXISTING IG account bound to this container, or None.
    "Existing" = ANY linked Account with a non-empty Username. We no longer require
    Last Posted to be set: an account can have a Username (= IG signup completed, logged
    in via the real-device IDFV) without having posted yet. Spoofing such an account's
    IDFV would still trigger a device-change challenge. The sticky-rule in apply() (via
    spoof_active_containers.json) protects accounts we created UNDER the spoof even
    before their first Last Posted date.

    Walks ALL linked Accounts (not just the first) to be robust against Container rows
    that have multiple Account references (e.g., old placeholder + real account both
    linked). Returns the first non-empty Username found.

    On ANY failure returns None -> apply() defaults to spoof OFF, which is the SAFE
    behavior for an unknown-state container."""
    import os, requests
    tok = os.environ.get("AIRTABLE_TOKEN")
    if not tok:
        return None
    hdr = {"Authorization": f"Bearer {tok}"}
    try:
        r = requests.get(
            f"https://api.airtable.com/v0/{_AIRTABLE_BASE}/{_T_CONTAINERS}",
            headers=hdr,
            params={"filterByFormula": f"{{Container}}='{container}'", "maxRecords": 1},
            timeout=15,
        ).json()
        if not r.get("records"):
            return None
        acc_ids = r["records"][0].get("fields", {}).get("Accounts") or []
        for aid in acc_ids:
            acc = requests.get(
                f"https://api.airtable.com/v0/{_AIRTABLE_BASE}/{_T_ACCOUNTS}/{aid}",
                headers=hdr, timeout=15,
            ).json()
            username = (acc.get("fields", {}).get("Username") or "").strip()
            if username:
                return username
        return None
    except Exception:                                        # noqa: BLE001 - any error -> unknown
        return None


_SPOOF_STATE_FILE = Path("/opt/adbbot/spoof_active_containers.json")


def _load_spoof_state() -> set[str]:
    """Containers that have been spoofed at least once (and must stay spoofed forever to
    avoid a device-change challenge once the account's IG history is tied to the spoofed IDFV)."""
    import json
    try:
        return set(json.loads(_SPOOF_STATE_FILE.read_text()))
    except (FileNotFoundError, OSError, ValueError):
        return set()


def _save_spoof_state(active: set[str]) -> None:
    import json
    _SPOOF_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SPOOF_STATE_FILE.write_text(json.dumps(sorted(active)))


def apply(ref: str, container: str) -> None:
    """Write the profile plist for `container` on this phone, drop cfprefsd so the next IG
    launch reads the new values. Called from open_container_cli between the IP gate and the
    IG `cranectl --switch`+uiopen (so a fresh IG process is what reads the plist).

    2026-10-18 Rene (live rollout): for ESTABLISHED containers (Account with Username AND
    Last Posted date set), writes apps=[] -> spoof INACTIVE for IG. Keeps the real iPhone
    IDFV that account has been posting on, avoids forcing a device-change challenge on an
    account that has IG history. For FREE / NEW containers (no account, or account has no
    posting history yet), writes apps=['com.burbn.instagram'] -> spoof ACTIVE from first
    use, so brand-new accounts register on the gespooften container-specific IDFV from day
    one. Decision per Airtable lookup. On Airtable error, defaults to spoof OFF (safe: no
    accidental IDFV flip on an unknown-state container).

    STICKY RULE: once a container was spoofed (even once), it STAYS spoofed forever (tracked
    in /opt/adbbot/spoof_active_containers.json). Otherwise once the new account posts its
    first clip, Last Posted gets filled, next apply() would see "established" -> apps=[] ->
    IDFV flips back to real iPhone -> IG challenge for the just-spoofed account. The sticky
    rule prevents that: once spoofed-active container -> always spoofed-active."""
    rec_id, p, dest = _resolve(ref)
    identity = identity_for(container)
    already_spoofed = container in _load_spoof_state()
    if already_spoofed:
        spoof_on = True                                      # sticky: once on, always on
    else:
        spoof_on = _established_account(container) is None   # new / free container -> spoof
    if not spoof_on:
        identity["apps"] = []
    data = plistlib.dumps(identity, fmt=plistlib.FMT_BINARY)
    _ssh(dest, f"cat > {PROFILE_REMOTE}.tmp && mv {PROFILE_REMOTE}.tmp {PROFILE_REMOTE} && "
               f"chmod 644 {PROFILE_REMOTE} && killall -9 cfprefsd 2>/dev/null; true", data, timeout=30)
    if spoof_on and not already_spoofed:
        active = _load_spoof_state()
        active.add(container)
        _save_spoof_state(active)


# ---------------------------------------------------------------- deploy-watcher (Option B)

def deploy_watcher(ref: str, password: str, log=print) -> list[str]:
    """Install the phone-side spoof watcher on `ref`.

    What goes on the phone:
      - WATCHER_BIN_REMOTE: the Python daemon (shebang + chmod 755)
      - WATCHER_LD_REMOTE:  LaunchDaemon plist (root:wheel 644)
      - WATCHER_SALT_REMOTE: a copy of the server's SALT_PATH so phone-side identity
        derivation matches the server bit-for-bit (0600, mobile:mobile is fine; the
        watcher runs as root via launchd and can read either)

    Then: `launchctl bootstrap system <ld>` (fallback to legacy `launchctl load`),
    verify the job is listed. The daemon is KeepAlive so a crash respawns in <= 10 s
    (ThrottleInterval). No ldid signing is needed because this is a Python script,
    not a signed binary with entitlements.

    Idempotent: re-running overwrites files, unloads the previous instance, then
    bootstraps the new one. Nothing on the phone needs a respring."""
    if not WATCHER_LOCAL.exists() or not WATCHER_LD_LOCAL.exists():
        raise SystemExit("missing watcher sources under iphone_farm/spoof/ -- repo tree incomplete")
    salt_local = Path(SALT_PATH)
    if not salt_local.exists():
        raise SystemExit(f"server salt {SALT_PATH} missing -- cannot deploy watcher (identity would diverge)")

    rec_id, p, dest = _resolve(ref)
    name = p.get("stream") or ref
    log(f"{name}: staging watcher script + LaunchDaemon + salt ...")
    with open(WATCHER_LOCAL, "rb") as f:
        _ssh(dest, "mkdir -p ~/rnxspoof && cat > ~/rnxspoof/rnxspoof-watcher", f.read(), timeout=60)
    with open(WATCHER_LD_LOCAL, "rb") as f:
        _ssh(dest, "cat > ~/rnxspoof/com.reynex.spoof.watcher.plist", f.read(), timeout=30)
    with open(salt_local, "rb") as f:
        _ssh(dest, "cat > ~/rnxspoof/rnxspoof.salt", f.read(), timeout=15)

    log(f"{name}: installing python3 if missing, placing files, loading daemon ...")
    body = (
        # Ensure python3 is present (standard Procursus package on Dopamine/palera1n).
        "if ! command -v python3 >/dev/null 2>&1; then "
        "  S apt-get update -qq >/dev/null 2>&1; "
        "  DEBIAN_FRONTEND=noninteractive S apt-get install -y -qq python3 >/dev/null 2>&1; "
        "fi; "
        "command -v python3 || { echo NO_PYTHON3; exit 4; }; "
        # Directories (rootless jailbreak uses /var/jb prefix).
        "S mkdir -p /var/jb/usr/local/bin /var/jb/Library/LaunchDaemons /var/jb/etc; "
        # Watcher binary.
        f"S mv ~/rnxspoof/rnxspoof-watcher {WATCHER_BIN_REMOTE}; "
        f"S chmod 755 {WATCHER_BIN_REMOTE}; "
        f"S chown root:wheel {WATCHER_BIN_REMOTE} 2>/dev/null || true; "
        # LaunchDaemon.
        f"S mv ~/rnxspoof/com.reynex.spoof.watcher.plist {WATCHER_LD_REMOTE}; "
        f"S chmod 644 {WATCHER_LD_REMOTE}; "
        f"S chown root:wheel {WATCHER_LD_REMOTE} 2>/dev/null || true; "
        # Salt (shared with server; the watcher reads it at startup).
        f"S mv ~/rnxspoof/rnxspoof.salt {WATCHER_SALT_REMOTE}; "
        f"S chmod 644 {WATCHER_SALT_REMOTE}; "
        # Log file (writable; truncate if huge).
        "S touch /var/log/rnxspoof.log; S chmod 644 /var/log/rnxspoof.log; "
        # Unload any previous instance, then (re)load. bootstrap is the modern API;
        # fall back to `load` for older launchctl.
        f"S launchctl bootout system {WATCHER_LD_REMOTE} 2>/dev/null; "
        f"S launchctl unload {WATCHER_LD_REMOTE} 2>/dev/null; "
        f"S launchctl bootstrap system {WATCHER_LD_REMOTE} 2>&1 "
        f"  || S launchctl load {WATCHER_LD_REMOTE} 2>&1; "
        # Give it a beat to spawn.
        "sleep 2; "
        # Verify.
        f"S launchctl list 2>/dev/null | grep -F {WATCHER_LABEL} || echo NOT_LOADED; "
        f"pgrep -f rnxspoof-watcher >/dev/null && echo RUN_OK || echo RUN_FAIL; "
        "echo done"
    )
    out = _sudo(dest, body, password, timeout=300)
    tail = out.strip().splitlines()[-6:]
    for line in tail:
        log(f"{name}: {line}")
    loaded = "NOT_LOADED" not in out
    running = "RUN_OK" in out
    status = "running" if (loaded and running) else "NOT running (see log)"
    return [
        f"watcher: {status}",
        f"binary:  {WATCHER_BIN_REMOTE}",
        f"daemon:  {WATCHER_LD_REMOTE}",
        f"salt:    {WATCHER_SALT_REMOTE}",
        f"log:     /var/log/rnxspoof.log",
    ]


def check_watcher(ref: str) -> dict:
    """Report whether the watcher is deployed + loaded + running on `ref`."""
    _, p, dest = _resolve(ref)
    out = _ssh(dest, (
        f"ls {WATCHER_BIN_REMOTE} 2>/dev/null && echo BIN_OK; "
        f"ls {WATCHER_LD_REMOTE} 2>/dev/null && echo LD_OK; "
        f"ls {WATCHER_SALT_REMOTE} 2>/dev/null && echo SALT_OK; "
        f"launchctl list 2>/dev/null | grep -F {WATCHER_LABEL} >/dev/null && echo LOADED; "
        f"pgrep -f rnxspoof-watcher >/dev/null && echo RUNNING; "
        f"tail -n 3 /var/log/rnxspoof.log 2>/dev/null"
    ), check=False).decode(errors="replace")
    return {
        "phone":   p.get("stream") or ref,
        "binary":  "BIN_OK" in out,
        "daemon":  "LD_OK" in out,
        "salt":    "SALT_OK" in out,
        "loaded":  "LOADED" in out,
        "running": "RUNNING" in out,
        "log_tail": out.rsplit("RUNNING", 1)[-1].strip() if "RUNNING" in out else out.strip()[-400:],
    }


# ---------------------------------------------------------------- check

def check(ref: str) -> dict:
    _, p, dest = _resolve(ref)
    out = _ssh(dest, f"ls {DYLIB_REMOTE} 2>/dev/null && echo DY_OK; ls {PLIST_REMOTE} 2>/dev/null && echo PL_OK; "
                     f"ls {PROFILE_REMOTE} 2>/dev/null && echo PR_OK; "
                     "ldid -e " + DYLIB_REMOTE + " 2>/dev/null | head -c 100",
               check=False).decode(errors="replace")
    return {"phone": p.get("stream") or ref,
            "dylib":   "DY_OK" in out,
            "filter":  "PL_OK" in out,
            "profile": "PR_OK" in out,
            "signed":  "<?xml" in out}


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse, getpass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("deploy", help="one-off: push dylib + filter plist, sign, respring (asks sudo pw)")
    s.add_argument("phone")
    s.add_argument("--password", help="default: $IPHONE_SUDO_PASSWORD or asked")
    s = sub.add_parser("apply", help="write the per-container profile plist (what the opener calls)")
    s.add_argument("phone")
    s.add_argument("container", help="Crane container name, e.g. G3")
    sub.add_parser("show-identity").add_argument("container")
    sub.add_parser("check").add_argument("phone")
    s = sub.add_parser("deploy-watcher",
                       help="install the phone-side Crane-switch watcher (asks sudo pw)")
    s.add_argument("phone")
    s.add_argument("--password", help="default: $IPHONE_SUDO_PASSWORD or asked")
    sub.add_parser("check-watcher",
                   help="is the phone-side watcher installed and running?").add_argument("phone")
    a = ap.parse_args(argv)

    if a.cmd == "deploy":
        pw = a.password or os.environ.get("IPHONE_SUDO_PASSWORD") or getpass.getpass("sudo password of mobile: ")
        for line in deploy(a.phone, pw):
            print("  " + line)
        return 0
    if a.cmd == "apply":
        apply(a.phone, a.container)
        print(f"applied identity for {a.container} on {a.phone}")
        return 0
    if a.cmd == "show-identity":
        for k, v in identity_for(a.container).items():
            print(f"  {k:7} {v}")
        return 0
    if a.cmd == "check":
        for k, v in check(a.phone).items():
            print(f"  {k:7} {v}")
        return 0
    if a.cmd == "deploy-watcher":
        pw = a.password or os.environ.get("IPHONE_SUDO_PASSWORD") or getpass.getpass("sudo password of mobile: ")
        for line in deploy_watcher(a.phone, pw):
            print("  " + line)
        return 0
    if a.cmd == "check-watcher":
        for k, v in check_watcher(a.phone).items():
            if k == "log_tail":
                print(f"  log_tail:")
                for ln in str(v).splitlines():
                    print(f"    {ln}")
            else:
                print(f"  {k:8} {v}")
        return 0
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
