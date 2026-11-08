"""Daily observer round: launch the observer phone, read every tracked account, shut down.

Tracked accounts = every non-empty `Username` in the "Reels" Airtable base
(Accounts table -- where the team maintains iPhone / Crane container per account)
plus the handles listed in ~/.adb_bot/observer/targets.txt (one per line; our
MLX accounts until they are entered in Airtable). Nothing is written to Airtable.

Env:
  OBSERVER_MLX_PROFILE   MLX mobile profile id of the observer phone (cloud phone)
  OBSERVER_SERIAL        alternatively: adb serial of a real Android observer phone
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

from adb_bot.automation import ig_observer

REELS_BASE = "appwk9LrgAhJv8iEH"
ACCOUNTS_TABLE = "Accounts"
TARGETS_FILE = ig_observer.STORE_DIR / "targets.txt"


def airtable_accounts() -> list:
    """[{username, model, iphone, container, status}] with a Username, from the Reels base."""
    tok = os.environ.get("AIRTABLE_TOKEN", "")
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{REELS_BASE}/{ACCOUNTS_TABLE}",
                         headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            u = str(f.get("Username") or "").strip().lstrip("@")
            if u:
                out.append({"username": u, "model": f.get("Model"), "iphone": f.get("iPhone"),
                            "container": f.get("Container"), "status": f.get("Status"),
                            "phone": f.get("Phone / Profile")})
        offset = j.get("offset")
        if not offset:
            return out


def file_targets() -> list:
    """[{username, model}] from targets.txt (`handle` or `handle,model` per line)."""
    out = []
    if TARGETS_FILE.exists():
        for line in TARGETS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            h, _, model = line.partition(",")
            out.append({"username": h.strip().lstrip("@"), "model": model.strip() or None})
    return out


def targets() -> list:
    names = [a["username"] for a in airtable_accounts()]
    names += [t["username"] for t in file_targets()]
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _launch_mlx(profile_id: str, logger):
    from adb_bot.clients.adb import ADBClient
    from adb_bot.automation.bootstrap import build_mlx_clients
    from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
    from adb_bot.automation.run_loop import _mlx_token
    from adb_bot.automation.verification_probe import _open_instagram
    clients = build_mlx_clients(_mlx_token())
    adb = ADBClient()
    for _ in range(3):
        clients.launcher.start_profiles([profile_id])
        prof = prepare_profile_for_adb(profile_id, clients.api, clients.adb_enable, logger,
                                       max_attempts=15, wait_seconds=15,
                                       launcher_client=clients.launcher, status_client=clients.status)
        if prof:
            target = connect_with_retries(adb, prof, logger, profile_id, max_attempts=6, retry_delay_seconds=5)
            if target and _open_instagram(target, adb, logger):
                return clients, target
        clients.shutdown.shutdown_profiles([profile_id])
        time.sleep(20)
    return clients, None


def main(argv=None) -> int:
    import argparse
    from adb_bot.core.logger import get_logger
    from adb_bot.core import locks
    ap = argparse.ArgumentParser(prog="observer_run")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--detail", type=int, default=10)
    ap.add_argument("--list", action="store_true", help="only print the targets")
    ap.add_argument("--only", default="", help="comma-separated handles instead of the full list")
    a = ap.parse_args(argv)
    log = get_logger("adb_bot")
    names = [h.strip().lstrip("@") for h in a.only.split(",") if h.strip()] or targets()
    if a.limit:
        names = names[:a.limit]
    log.info("observer: %d account(s) to read", len(names))
    if a.list:
        print("\n".join(names))
        return 0
    serial = os.environ.get("OBSERVER_SERIAL", "").strip()
    profile = os.environ.get("OBSERVER_MLX_PROFILE", "").strip()
    clients = None
    with locks.ProfileLocks(owner="observer") as held:
        if profile and not serial:
            if not held.acquire_all([profile]):
                log.warning("observer: phone %s busy, skipping this round", profile)
                return 3
            clients, serial = _launch_mlx(profile, log)
            if not serial:
                log.error("observer: phone never came up")
                return 4
        if not serial:
            log.error("observer: set OBSERVER_MLX_PROFILE or OBSERVER_SERIAL")
            return 2
        try:
            obs = ig_observer.Observer(serial, logger=log)
            snaps = []
            for n in names:
                snaps.append(obs.observe(n, detail=a.detail))
                ig_observer.save([snaps[-1]])        # write as we go: a crash keeps what was read
                time.sleep(8)                         # look like browsing, not a sweep
        finally:
            if clients and profile:
                clients.shutdown.shutdown_profiles([profile])
    ok = sum(1 for s in snaps if s.status == "ok")
    log.info("observer: done, %d/%d read ok", ok, len(snaps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
