"""Write Shadowrocket (or any Crane-cloned app's) server config straight into a
Crane container's files over SSH -- no Shortcuts/VNC/typing involved.

Why this exists: on the physical-iPhone farm (jailbroken, Crane app-cloning +
Shadowrocket per-container proxies, see memory iphone-farm-*), entering a
proxy by hand means 4 fields x N containers in Shadowrocket's own UI. This
script does it instead by:

1. Reading one already-configured container's `ServerManager` file (a binary
   NSKeyedArchiver plist) as a template.
2. Substituting just the three fields that actually vary per proxy -- host,
   port, and a fresh server UUID -- and leaving the rest of the object graph
   byte-identical, so Shadowrocket reads it the same way it reads one it
   built itself.
3. Writing that + a matching `Library/Preferences/group.<bundle>.plist`
   selector file (which points SelectedServerUUID at the new entry) into the
   target container's folder over SCP.

Verified against a real, Shadowrocket-populated container 2026-10-01 (see
memory iphone-farm-catvnc-passwords.md's sibling notes): byte-for-byte
round-trip through plistlib, SCP transfer diffed identical, and the value
Shadowrocket actually displays after a real Crane container switch matched.

Separately, `register_container` / `unregister_container` add or remove an
entry in Crane's own prefs (`com.opa334.craneprefs.plist`), which is what
makes a container selectable in the Shortcuts "Switch to Crane Container"
picker in the first place -- confirmed working the same way (a hand-added
test entry showed up in the picker, see the commit this file landed in).

This never touches Instagram's containers -- only ever run it against an app
with no live account/session state you care about preserving, and always
read the *target* container's existing files (if any) before overwriting.
"""

from __future__ import annotations

import copy
import plistlib
import subprocess
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8"]


@dataclass
class Proxy:
    label: str     # the Crane container's display name, e.g. "B1 Proxy"
    host: str
    port: str       # Shadowrocket stores this as a string, not an int
    user: str
    password: str


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {r.returncode}: {r.stderr.strip()}")
    return r.stdout


def ssh(ip: str, key_path: str, remote_cmd: str) -> str:
    return _run(["ssh", "-n", "-i", key_path, *SSH_OPTS, f"mobile@{ip}", remote_cmd])


def scp_pull(ip: str, key_path: str, remote_path: str, local_path: str) -> None:
    _run(["scp", "-i", key_path, *SSH_OPTS, f"mobile@{ip}:{remote_path}", local_path])


def scp_push(ip: str, key_path: str, local_path: str, remote_path: str) -> None:
    _run(["scp", "-i", key_path, *SSH_OPTS, local_path, f"mobile@{ip}:{remote_path}"])


# --------------------------------------------------------------------------- generate

def build_server_manager(template_bytes: bytes, proxy: Proxy) -> bytes:
    """Clone a working ServerManager (NSKeyedArchiver bplist) with a new
    host/port/uuid. Object indices below were hand-identified from a real
    HTTP-type Shadowrocket server entry -- see this module's docstring."""
    d = plistlib.load(__import__("io").BytesIO(template_bytes), fmt=plistlib.FMT_BINARY)
    objs = d["$objects"]
    objs[24] = str(proxy.port)
    objs[26] = proxy.host
    objs[30]["NS.string"] = str(uuidlib.uuid4()).upper()
    # user/password are left as whatever the template had unless the caller's
    # proxy differs from it -- nearly always the whole farm shares one login.
    if proxy.user:
        objs[29] = proxy.user
    if proxy.password:
        objs[28] = proxy.password
    out = __import__("io").BytesIO()
    plistlib.dump(d, out, fmt=plistlib.FMT_BINARY)
    return out.getvalue(), objs[30]["NS.string"]


def build_selector_plist(proxy: Proxy, server_uuid: str) -> bytes:
    selector = {
        "group.com.liguangming.SelectedServerName": f"{proxy.host}:{proxy.port}",
        "group.com.liguangming.PermissionNotificationEnabled": False,
        "group.com.liguangming.PermissionLocationEnabled": False,
        "group.com.liguangming.SelectedServerUUID": server_uuid,
    }
    out = __import__("io").BytesIO()
    plistlib.dump(selector, out, fmt=plistlib.FMT_BINARY)
    return out.getvalue()


# ----------------------------------------------------------------------------- push

def push_proxy(ip: str, key_path: str, app_group_path: str, container_uuid: str,
                proxy: Proxy, template_bytes: bytes, plist_name: str,
                tmp_dir: str = "/tmp") -> str:
    """Write one proxy into one already-registered Crane container. Returns
    the generated server UUID (useful for logging/verification)."""
    sm_bytes, server_uuid = build_server_manager(template_bytes, proxy)
    sel_bytes = build_selector_plist(proxy, server_uuid)

    sm_local = f"{tmp_dir}/{proxy.label}_ServerManager"
    sel_local = f"{tmp_dir}/{proxy.label}_selector.plist"
    Path(sm_local).write_bytes(sm_bytes)
    Path(sel_local).write_bytes(sel_bytes)

    dest = f"{app_group_path}/Library/___Crane_Containers/{container_uuid}"
    ssh(ip, key_path, f"mkdir -p '{dest}/Library/Preferences'")
    scp_push(ip, key_path, sm_local, f"{dest}/ServerManager")
    scp_push(ip, key_path, sel_local, f"{dest}/Library/Preferences/{plist_name}")
    return server_uuid


# ------------------------------------------------------------- Crane's own registry

def _craneprefs_path() -> str:
    return "/var/jb/var/mobile/Library/Preferences/com.opa334.craneprefs.plist"


def register_container(ip: str, key_path: str, bundle_app_key: str, name: str,
                        tmp_dir: str = "/tmp") -> str:
    """Add `name` to Crane's container list for `bundle_app_key` (e.g.
    "com.liguangming.Shadowrocket"), so it shows up in the Shortcuts "Switch
    to Crane Container" picker. Returns the new container's uuid -- the
    *folder* for it is created lazily the first time something writes to it
    (push_proxy does this itself) or the first time the app is actually
    switched into it."""
    local = f"{tmp_dir}/craneprefs_current.plist"
    scp_pull(ip, key_path, _craneprefs_path(), local)
    d = plistlib.load(open(local, "rb"))
    key = f"appSettings_{bundle_app_key}"
    new_uuid = str(uuidlib.uuid4()).upper()
    d[key]["Containers"].append({"identifier": new_uuid, "name": name})
    out = f"{tmp_dir}/craneprefs_new.plist"
    with open(out, "wb") as f:
        plistlib.dump(d, f, fmt=plistlib.FMT_XML)
    scp_push(ip, key_path, out, _craneprefs_path())
    return new_uuid


def unregister_container(ip: str, key_path: str, bundle_app_key: str,
                          container_uuid: str, tmp_dir: str = "/tmp") -> None:
    local = f"{tmp_dir}/craneprefs_current.plist"
    scp_pull(ip, key_path, _craneprefs_path(), local)
    d = plistlib.load(open(local, "rb"))
    key = f"appSettings_{bundle_app_key}"
    d[key]["Containers"] = [c for c in d[key]["Containers"] if c.get("identifier") != container_uuid]
    out = f"{tmp_dir}/craneprefs_new.plist"
    with open(out, "wb") as f:
        plistlib.dump(d, f, fmt=plistlib.FMT_XML)
    scp_push(ip, key_path, out, _craneprefs_path())
