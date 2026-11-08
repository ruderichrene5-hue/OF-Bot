"""Pull a logged-in Instagram session off one mobile phone, as browser cookies.

Goal: skip Instagram's mobile login flow (which can take 6+ hours through
verification) for accounts that are already logged in on a phone, by lifting
the session cookies straight off the device and handing them to
`inject_cookies_mlx.py`, which writes them into a Multilogin X browser profile.

Works against either fleet this bot drives -- GeeLark cloud phones or MLX's
own (legacy) mobile phones -- via `--platform`.

How it gets the cookies: Instagram's Android app authenticates its API calls
with the same handful of cookie values a browser session would carry
(`sessionid`, `csrftoken`, `ds_user_id`, `mid`, `ig_did`, `rur`). Those live in
the app's private shared_prefs, readable only as root -- this script shells in
via `su`, dumps every shared_prefs XML for `com.instagram.android`, and
pattern-matches the cookie values out of it rather than assuming one fixed
file/key, since that has moved across app versions.

If the phone has no root (`su` unavailable), this fails loudly rather than
silently writing an empty session -- see the printed message for the fallback
(traffic sniffing) instead of guessing.

Usage:
    python3 scripts/extract_ig_session.py <profile_id> --platform geelark
    python3 scripts/extract_ig_session.py <profile_id> --platform mlx [--out FILE]

Requires the same environment this bot already runs under (credentials in
/etc/adbbot/env, sourced before running) and adb on PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adb_bot.clients.adb import ADBClient
from adb_bot.clients.geelark.adb_enable import GeelarkAdbEnableClient
from adb_bot.clients.geelark.api import GeelarkApiClient
from adb_bot.clients.api import MultiloginApiClient
from adb_bot.clients.multilogin import (
    MultiloginAdbEnableClient,
    MultiloginLauncherClient,
    MultiloginPhoneStatusClient,
    STATUS_READY,
    STATUS_STARTING,
)
from adb_bot.config import settings

IG_PACKAGE = "com.instagram.android"
PREFS_DIR = f"/data/data/{IG_PACKAGE}/shared_prefs"

# What we're trying to lift out of the dumped prefs. Matched as `key=value`
# fragments (how Instagram writes its serialized cookie jar), not as a single
# fixed XML element, because the enclosing key/file has changed across app
# versions and probably will again.
COOKIE_NAMES = ["sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"]

# Loose on purpose: value is everything up to the next `;`, `"`, `&` or
# whitespace, since the surrounding serialization format isn't stable.
COOKIE_PATTERN = re.compile(
    r"(?P<name>" + "|".join(COOKIE_NAMES) + r")=(?P<value>[^;\"&\s<]+)"
)


def run_root_shell(adb: ADBClient, target: str, command: str) -> str | None:
    """`adb -s target shell su -c "<command>"`, or None if su isn't there."""
    quoted = command.replace('"', '\\"')
    out = adb.run_command(f'adb -s {target} shell su -c "{quoted}"')
    if out is None:
        return None
    lowered = out.lower()
    if "not found" in lowered or "permission denied" in lowered or out.strip() == "":
        return None
    return out


def extract_cookies(dumped_prefs: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in COOKIE_PATTERN.finditer(dumped_prefs):
        name, value = match.group("name"), match.group("value")
        # Keep the first (usually only, and usually the freshest at the top of
        # a concatenated dump) value seen for each cookie name.
        found.setdefault(name, value)
    return found


def ready_profile_geelark(profile_id: str):
    print(f"[1/5] enabling ADB on {profile_id} ...")
    adb_enable = GeelarkAdbEnableClient()
    enable_outcome = adb_enable.enable_and_wait([profile_id])
    if not enable_outcome.succeeded:
        print(f"[-] could not enable ADB: {enable_outcome.failures()}")
        return None

    print("[2/5] fetching ADB credentials ...")
    api = GeelarkApiClient()
    creds = api.fetch_adb_credentials([profile_id])
    profiles = api.parse_profiles(creds)
    profile = next((p for p in profiles if p.id == profile_id), None)
    if profile is None or not profile.is_ready:
        print(f"[-] phone not ready for ADB: {profile.status if profile else 'not found'}")
        return None
    return profile


def ready_profile_mlx(profile_id: str, max_attempts: int = 8, wait_seconds: int = 15):
    """Launch (if needed), enable ADB and poll credentials for an MLX mobile phone.

    A phone that's currently STATUS_SHUTDOWN needs an actual launch call, not
    just an ADB enable -- enabling ADB on a stopped phone just gets you
    "not running" forever. STATUS_STARTING gets a free wait instead of hammering
    the enable endpoint, same as the loop this mirrors in workflow.py.
    """
    token = settings.get_saved_bearer_token()
    if not token:
        print("[-] no MULTILOGIN_TOKEN / saved bearer token found")
        return None

    status_client = MultiloginPhoneStatusClient(token)
    launcher = MultiloginLauncherClient(token)
    adb_enable = MultiloginAdbEnableClient(token)
    api = MultiloginApiClient(token)

    print(f"[1/5] checking phone status for {profile_id} ...")
    status = status_client.status_for(profile_id)
    if status != STATUS_READY:
        print(f"    status={status}, launching ...")
        launcher.start_profiles([profile_id])

    for attempt in range(1, max_attempts + 1):
        print(f"[2/5] waiting for readiness, attempt {attempt}/{max_attempts} ...")
        time.sleep(wait_seconds)

        status = status_client.status_for(profile_id)
        if status == STATUS_STARTING:
            print("    still starting, skipping ADB enable this round")
            continue

        adb_enable.enable_adb([profile_id], enabled=True)

        creds = api.fetch_adb_credentials([profile_id])
        profiles = api.parse_profiles(creds)
        profile = next((p for p in profiles if str(p.id) == str(profile_id)), None)
        if profile and profile.is_ready:
            return profile

    print("[-] phone never became ADB-ready in time")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_id", help="phone id")
    parser.add_argument("--platform", choices=["geelark", "mlx"], required=True,
                        help="which fleet this phone id belongs to")
    parser.add_argument("--out", default=None,
                        help="Where to write the session JSON "
                             "(default: ig_session_<profile_id>.json)")
    args = parser.parse_args()

    out_path = Path(args.out or f"ig_session_{args.profile_id}.json")

    if args.platform == "geelark":
        profile = ready_profile_geelark(args.profile_id)
    else:
        profile = ready_profile_mlx(args.profile_id)
    if profile is None:
        return 1

    print(f"[3/5] connecting to {profile.target} ...")
    adb = ADBClient()
    if not adb.connect(profile) or not adb.authenticate(profile):
        print("[-] adb connect/authenticate failed")
        return 1
    target = profile.target
    assert target

    print("[4/5] dumping Instagram shared_prefs as root ...")
    dumped = run_root_shell(adb, target, f"cat {PREFS_DIR}/*.xml 2>/dev/null")
    if dumped is None:
        print(
            "[-] no root shell available on this phone (su not found, or "
            "permission denied).\n"
            "    Root-based extraction doesn't work here -- the fallback is "
            "sniffing the phone's traffic instead:\n"
            "    point this phone's proxy at a mitmproxy instance (chained to "
            "its real proxy) and read the `Cookie:` header off any "
            "instagram.com API request while the app is open. Say the word "
            "and I'll build that version instead."
        )
        return 1

    cookies = extract_cookies(dumped)
    missing = [name for name in ("sessionid", "csrftoken", "ds_user_id") if name not in cookies]
    if missing:
        print(f"[-] dumped prefs but couldn't find: {missing}")
        print("    (dump was non-empty, so this account may just not be logged in,")
        print("     or Instagram changed how it stores these -- can dig into the raw dump.)")
        return 1

    print(f"[5/5] found {list(cookies.keys())}")
    out_path.write_text(json.dumps({
        "profile_id": args.profile_id,
        "cookies": cookies,
    }, indent=2))
    print(f"[+] wrote {out_path}")
    print("    copy this file to the machine running Multilogin X, then run:")
    print(f"    python3 inject_cookies_mlx.py {out_path.name} <mlx_browser_profile_id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
