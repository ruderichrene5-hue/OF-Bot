"""Keep chosen MLX *browser* profiles running on this server (2026-09-30, operator request).

Chatters use these profiles over RDP; a closed profile (reboot, crash, someone
clicking X) means nobody can answer messages. Every run checks the local
launcher's running profiles and starts any listed profile that isn't open --
headed, on the server's X display (the MLX agent unit sets DISPLAY=:0), so it is
visible over remote desktop.

Profiles are configured in KEEP_OPEN_BROWSER_PROFILES in /etc/adbbot/env as
comma-separated `<folder_id>/<profile_id>` pairs. `LOCK_PROFILE_ERROR` means the
profile is open on another machine -- logged, not retried harder.
"""

from __future__ import annotations

import os
import sys

import requests
import urllib3

LAUNCHER = "https://launcher.mlx.yt:45001"


def _headers() -> dict:
    from adb_bot.automation.run_loop import _mlx_token
    return {"Authorization": f"Bearer {_mlx_token()}", "Accept": "application/json"}


def running_ids(h: dict) -> set:
    r = requests.get(f"{LAUNCHER}/api/v1/profile/statuses", headers=h, timeout=30, verify=False)
    states = ((r.json() or {}).get("data") or {}).get("states") or {}
    # Only a live browser counts; "start_browser_error", "stopped" etc. do not.
    return {pid for pid, st in states.items()
            if "running" in str((st or {}).get("status", "")).lower()}


def start(h: dict, folder: str, profile: str) -> tuple:
    r = requests.get(
        f"{LAUNCHER}/api/v2/profile/f/{folder}/p/{profile}/start"
        "?automation_type=selenium&headless_mode=false",
        headers=h, timeout=300, verify=False)
    body = r.json() if r.content else {}
    return r.status_code, (body.get("status") or {}).get("error_code") or ""


def main() -> int:
    urllib3.disable_warnings()
    pairs = [p.strip() for p in os.environ.get("KEEP_OPEN_BROWSER_PROFILES", "").split(",") if p.strip()]
    if not pairs:
        print("keep-browser-open: nothing configured")
        return 0
    h = _headers()
    running = running_ids(h)
    for pair in pairs:
        folder, _, profile = pair.partition("/")
        if profile in running:
            print(f"keep-browser-open: {profile} running")
            continue
        code, err = start(h, folder, profile)
        print(f"keep-browser-open: {profile} was closed -> start {code} {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
