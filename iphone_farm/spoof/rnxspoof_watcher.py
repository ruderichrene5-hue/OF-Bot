#!/usr/bin/env python3
"""rnxspoof-watcher: phone-side spoof-plist daemon.

Watches Crane's preference file. When the Crane container actually changes --
either because the server-side opener did it over SSH, or because the user tapped
a Shortcut on the phone that calls `cranectl --switch` directly -- the watcher
re-derives the per-container device identity and writes the spoof profile plist,
then drops cfprefsd so the next Instagram launch reads the fresh values.

Why this exists (Option B): the server-side `iphone_farm_spoof.apply()` only runs
when `iphone_farm_fastopen` opens Instagram over SSH. Manual Shortcut taps bypass
that path, so without this daemon the spoof plist would still carry the previous
container's identity.

Identity derivation MUST stay bit-for-bit identical to the server-side one in
`adb_bot.automation.iphone_farm_spoof.identity_for`. The parity test
`tests/test_spoof_watcher_identity.py` enforces that. If you change one side,
update the other and re-run the test.

v1 scope: writes `apps=['com.burbn.instagram']` by default (treats every container
as "fresh" when the sticky-list is empty or missing). Server-side policy
(established vs free, Airtable lookup) remains authoritative during the normal
`open_container_cli` flow -- this daemon is the backup for manual switches.
When the server later syncs `/var/mobile/rnxspoof_active.json` down to the phone,
containers in that list are forced spoof-ON (sticky) regardless of future taps.

Dedup: remembers the last-applied container name in memory. We re-write the
plist and kill cfprefsd ONLY when the active container actually changed, so idle
polling costs are one stat() per second and nothing else.

Robustness: every plist read is wrapped. A corrupt or truncated Crane plist mid
write is logged and skipped -- the watcher keeps running and will retry on the
next mtime bump. Nothing in this script may raise to the top-level loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

CRANE_PLIST   = "/var/mobile/Library/Preferences/com.opa334.craneprefs.plist"
SPOOF_PLIST   = "/var/mobile/Library/Preferences/com.reynex.spoof.plist"
SALT_PATH     = "/var/jb/etc/rnxspoof.salt"
STICKY_LOCAL  = "/var/mobile/rnxspoof_active.json"
LOG_PATH      = "/var/log/rnxspoof.log"
LOG_MAX_BYTES = 1 * 1024 * 1024

CRANE_KEY_APPSETTINGS = "appSettings_com.burbn.instagram"
CRANE_KEY_CONTAINERS  = "appSettings_com.burbn.instagram.Containers"
CRANE_IG_BUNDLE       = "com.burbn.instagram"

SPOOFED_APPS = ["com.burbn.instagram"]
POLL_INTERVAL_SECONDS = 1.0

_IPHONE_X_TAC = "35345908"


# ---------------------------------------------------------------- logging

def _log(msg: str) -> None:
    """Append one line to LOG_PATH, rotate at LOG_MAX_BYTES. Never raises."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {msg}\n"
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                try:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
                except OSError:
                    pass
        except FileNotFoundError:
            pass
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:                                                # noqa: BLE001
        pass


# ---------------------------------------------------------------- identity derivation
# MUST mirror iphone_farm_spoof._uuid*/_udid_for/_serial_for/_imei_for exactly.

_SALT_CACHE: list[str] = []


def _salt() -> str:
    if _SALT_CACHE:
        return _SALT_CACHE[0]
    try:
        s = Path(SALT_PATH).read_text().strip()
    except FileNotFoundError:
        # No fallback: the server-side salt is the only valid one. Refusing to invent
        # one keeps us from writing identities that will not match the server's.
        raise SystemExit(f"salt file {SALT_PATH} missing -- deploy-watcher must push it")
    _SALT_CACHE.append(s)
    return s


def _h(container: str, key: str, n: int = 32) -> str:
    return hashlib.sha256(f"{_salt()}|{container}|{key}".encode()).hexdigest()[:n]


def _uuidv4_for(container: str, key: str) -> str:
    h = _h(container, key, 32)
    variant = f"{(int(h[16], 16) & 0x3) | 0x8:x}"
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{variant}{h[17:20]}-{h[20:32]}".upper()


def _udid_for(container: str) -> str:
    h = _h(container, "udid", 24)
    return f"{h[0:8]}-{h[8:24]}".upper()


def _serial_for(container: str) -> str:
    body = _h(container, "serial", 20).upper()
    body = "".join(c for c in body if c.isalnum())[:5].ljust(5, "0")
    return f"F2L{body}NQNG"[:12]


def _imei_for(container: str) -> str:
    body = _IPHONE_X_TAC + ("".join(c for c in _h(container, "imei", 24) if c.isdigit()) + "000000")[:6]
    total = 0
    for i, c in enumerate(body[::-1]):
        d = int(c)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


def identity_for(container: str) -> dict:
    return {
        "apps":   list(SPOOFED_APPS),
        "idfv":   _uuidv4_for(container, "idfv"),
        "idfa":   _uuidv4_for(container, "idfa"),
        "udid":   _udid_for(container),
        "serial": _serial_for(container),
        "imei":   _imei_for(container),
    }


# ---------------------------------------------------------------- Crane plist parsing

def _strip_ig_suffix(name: str) -> str:
    """Crane container names in IG's list look like 'H4 IG', 'Nikki 3 IG'. The server
    side keys identities on the bare container name ('H4', 'Nikki 3')."""
    n = name.strip()
    if n.endswith(" IG"):
        n = n[:-3].rstrip()
    return n


def read_active_container_name() -> str | None:
    """Return the bare Crane container name currently active for Instagram, or None
    if the Crane plist cannot be parsed / IG has no active container selected.

    Reads the binary plist with plistlib (works on both XML and binary). Pulls:
      appSettings_com.burbn.instagram -> activeContainer (UUID string)
      appSettings_com.burbn.instagram.Containers -> list of {identifier, name}
    then matches UUID -> name, strips the ' IG' suffix.
    """
    try:
        with open(CRANE_PLIST, "rb") as f:
            data = plistlib.load(f)
    except (FileNotFoundError, OSError) as e:
        _log(f"crane plist read failed: {e}")
        return None
    except Exception as e:                                           # noqa: BLE001
        _log(f"crane plist parse failed: {e}")
        return None

    app_settings = data.get(CRANE_KEY_APPSETTINGS)
    if not isinstance(app_settings, dict):
        return None
    active_uuid = app_settings.get("activeContainer")
    if not isinstance(active_uuid, str) or not active_uuid:
        return None

    containers = data.get(CRANE_KEY_CONTAINERS)
    if not isinstance(containers, (list, tuple)):
        return None
    for entry in containers:
        if not isinstance(entry, dict):
            continue
        if entry.get("identifier") == active_uuid:
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                return _strip_ig_suffix(name)
    _log(f"active container {active_uuid!r} not found in Containers list")
    return None


# ---------------------------------------------------------------- sticky list (v1: local, optional)

def _load_sticky() -> set[str]:
    try:
        with open(STICKY_LOCAL) as f:
            v = json.load(f)
        if isinstance(v, list):
            return {str(x) for x in v}
    except (FileNotFoundError, OSError, ValueError):
        pass
    return set()


def _decide_apps(container: str) -> list[str]:
    """v1 rule: default spoof ON ('fresh container' heuristic). If a sticky-list file
    is present and the container is NOT in it, we still default ON -- the sticky list
    is advisory for v1 and only used by the server-side `apply()`. v2 can turn this
    into a negative/positive gate once we have a way to sync it reliably."""
    sticky = _load_sticky()
    if sticky and container in sticky:
        return list(SPOOFED_APPS)
    return list(SPOOFED_APPS)


# ---------------------------------------------------------------- write + cfprefsd kick

def _spoof_plist_equivalent(path: str, expected: dict) -> bool:
    """True if the plist at `path` already encodes `expected`. Compare on decoded dict,
    not raw bytes, so a benign re-encode does not force an unnecessary cfprefsd kill."""
    try:
        with open(path, "rb") as f:
            cur = plistlib.load(f)
    except (FileNotFoundError, OSError):
        return False
    except Exception:                                                # noqa: BLE001
        return False
    if not isinstance(cur, dict):
        return False
    return cur == expected


def apply_for_container(container: str) -> bool:
    """Write SPOOF_PLIST for `container` and kick cfprefsd. Returns True if the plist
    was actually changed (i.e. cfprefsd was killed), False if it was already in sync."""
    identity = identity_for(container)
    identity["apps"] = _decide_apps(container)

    if _spoof_plist_equivalent(SPOOF_PLIST, identity):
        return False

    data = plistlib.dumps(identity, fmt=plistlib.FMT_BINARY)
    tmp = SPOOF_PLIST + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, SPOOF_PLIST)
    except OSError as e:
        _log(f"write failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    # Drop the preferences cache so the next IG launch reads the fresh file.
    try:
        subprocess.run(["killall", "-9", "cfprefsd"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return True


# ---------------------------------------------------------------- main loop

_RUNNING = True


def _stop(_signo, _frame) -> None:
    global _RUNNING
    _RUNNING = False


def run() -> int:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    _log("rnxspoof-watcher started")
    try:
        _salt()                                                      # fail-fast if missing
    except SystemExit as e:
        _log(str(e))
        return 2

    last_mtime: float = -1.0
    last_container: str | None = None

    # On startup, treat current state as unseen so we reconcile once.
    while _RUNNING:
        try:
            try:
                mtime = os.stat(CRANE_PLIST).st_mtime
            except FileNotFoundError:
                mtime = 0.0
            except OSError as e:
                _log(f"stat failed: {e}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            if mtime != last_mtime:
                last_mtime = mtime
                container = read_active_container_name()
                if container and container != last_container:
                    try:
                        wrote = apply_for_container(container)
                    except Exception as e:                           # noqa: BLE001
                        _log(f"apply raised for {container}: {e}\n{traceback.format_exc(limit=3)}")
                        wrote = False
                    if wrote:
                        _log(f"applied identity for {container}")
                    else:
                        _log(f"active container is {container} (plist already in sync)")
                    last_container = container
                elif container is None:
                    # Crane plist bumped but we could not read an IG container;
                    # do not overwrite last_container, so we retry next bump.
                    pass
        except Exception as e:                                       # noqa: BLE001
            _log(f"loop error: {e}\n{traceback.format_exc(limit=3)}")

        time.sleep(POLL_INTERVAL_SECONDS)

    _log("rnxspoof-watcher stopping")
    return 0


if __name__ == "__main__":
    sys.exit(run())
