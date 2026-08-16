"""Take one blank phone all the way to an Instagram account, in a single launch.

    python -m adb_bot.automation.signup_phone --profile "Blank caio 1"
    python -m adb_bot.automation.signup_phone --profile "Blank caio 1" --apply

Four steps that used to be four separate sessions:

1. put the assigned Gmail on the phone, through the **Play Store**;
2. install Instagram from the Play Store, by deep link;
3. create the account, taking the email hatch rather than renting a number;
4. write the credentials down, twice -- locally first, then into the VA base.

**One launch, because these phones live about fifteen minutes** and there is no
resuming a half-made Instagram account: a restarted app returns to "Join
Instagram" and throws away an already-verified code. Steps 1 and 2 are the
exception -- both persist on the MLX profile, so a second run of this skips
them and goes straight to the signup with most of the phone's life still ahead
of it.

Dry-run by default. `--apply` is what launches phones and creates accounts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import google_signin, play_install, signup
from adb_bot.automation.flows.gmail_code import PhoneMailbox
from adb_bot.automation.flows.signup_driver import AdbSignupDriver
from adb_bot.automation.signup_identity import make_identity, record_account
from adb_bot.automation.verification_probe import (
    _find_profile, _profiles, _resolve_token,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

INSTAGRAM_PACKAGE = "com.instagram.android"

# Which mailbox belongs to which phone. Written by whoever reserved them, so a
# rerun uses the same address rather than burning a second one.
ASSIGNMENTS = Path.home() / ".adb_bot" / "mailboxes.json"


def load_assignment(profile_name: str, path: Path | None = None) -> dict:
    path = path or ASSIGNMENTS
    if not path.exists():
        raise SystemExit(
            f"[stop] no mailbox assignments at {path}. Reserve some first:\n"
            f"    python -m adb_bot.automation.signup_mailboxes --take 3")
    data = json.loads(path.read_text())
    if profile_name not in data:
        raise SystemExit(
            f"[stop] no mailbox assigned to {profile_name!r}. "
            f"Assigned: {sorted(data)}")
    return data[profile_name]


def run_phone(profile_item, box, clients, adb_client, args, logger) -> dict:
    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    identity = make_identity()
    identity.email = box["address"]
    identity.email_password = box.get("password", "")

    out = {"profile": name, "id": profile_id, "email": box["address"],
           "username": identity.username, "steps": {}}

    print(f"\n{'=' * 68}\n{name} ({profile_id})")
    print(f"  mailbox  {box['address']}")
    print(f"  identity {identity.summary()}")
    if not args.apply:
        out["status"] = "dry-run"
        print("  DRY RUN -- nothing launched, no account created")
        return out

    if not locks.acquire(profile_id, owner="signup-phone"):
        out["status"] = "busy"
        return out

    # Before the phone is touched: an account whose password only ever existed
    # in memory is the state sixteen fleet profiles are already in.
    record_account(profile_id, name, identity, status="attempting")

    started = time.monotonic()
    try:
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=args.readiness_attempts,
            wait_seconds=args.readiness_wait,
            launcher_client=clients.launcher)
        if not profile:
            out["status"] = "not-ready"
            return out

        target = connect_with_retries(adb_client, profile, logger, profile_id,
                                      max_attempts=5, retry_delay_seconds=5)
        if not target:
            out["status"] = "unreachable"
            return out
        out["target"] = target

        driver = AdbSignupDriver(target, adb_client, logger=logger, act=True,
                                 screenshots=args.screenshots)

        # --- 1. the mailbox ---------------------------------------------------
        verdict = google_signin.sign_in(
            driver, adb_client, target, box["address"], box["password"],
            box["totp_secret"], logger=logger)
        out["steps"]["google_signin"] = verdict
        print(f"  google sign-in: {verdict} "
              f"({int(time.monotonic() - started)}s)")
        if verdict not in (google_signin.RESULT_SIGNED_IN,
                           google_signin.RESULT_ALREADY):
            out["status"] = f"mailbox-{verdict}"
            return out

        # --- 2. Instagram -----------------------------------------------------
        verdict = play_install.install(driver, adb_client, target,
                                       INSTAGRAM_PACKAGE, logger=logger)
        out["steps"]["install"] = verdict
        print(f"  instagram install: {verdict} "
              f"({int(time.monotonic() - started)}s)")
        if verdict not in (play_install.RESULT_INSTALLED,
                           play_install.RESULT_ALREADY):
            out["status"] = f"install-{verdict}"
            return out

        # --- 3. the account ---------------------------------------------------
        adb_client.run_command(
            f"adb -s {target} shell am start -n "
            f"{INSTAGRAM_PACKAGE}/.activity.MainTabActivity")
        time.sleep(12)

        mailbox = PhoneMailbox(target, adb_client, box["address"],
                               logger=logger, driver=driver)
        result = signup.run_signup(driver, None, identity, logger=logger,
                                   mailbox=mailbox)
        out["steps"]["signup"] = result.status
        out["status"] = result.status
        out["detail"] = result.detail[:300]
        print(f"  signup: {result.status}  {result.detail[:160]}")

        record_account(profile_id, name, identity, status=result.status)
        if result.ok:
            print(f"  CREATED @{identity.username} on {box['address']}")
        return out
    except Exception as exc:
        logger.warning("signup_phone: %s failed (%s)", name, exc)
        out["status"] = "error"
        out["detail"] = str(exc)[:300]
        return out
    finally:
        out["elapsed"] = int(time.monotonic() - started)
        try:
            clients.shutdown.shutdown_profiles([profile_id])
        except Exception as exc:
            logger.warning("signup_phone: shutdown failed for %s (%s)",
                           profile_id, exc)
        locks.release(profile_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="One blank phone to one Instagram account, in one launch.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--profile", action="append", required=True,
                        help="MLX profile name; repeat for several")
    parser.add_argument("--assignments", type=Path, default=None,
                        help=f"mailbox assignments (default {ASSIGNMENTS})")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--readiness-attempts", type=int, default=10)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)
    clients = build_mlx_clients(token)
    adb_client = ADBClient()

    results = []
    for wanted in args.profile:
        item = _find_profile(items, wanted)
        if item is None:
            print(f"[skip] no MLX profile named {wanted!r}", file=sys.stderr)
            continue
        box = load_assignment(str(item.get("serial_name")), args.assignments)
        results.append(run_phone(item, box, clients, adb_client, args, logger))

    print(f"\n{'=' * 68}")
    for r in results:
        print(f"  {r['profile']:16} {r.get('status', '?'):22} "
              f"@{r.get('username', '')}  {r.get('email', '')}")
    made = [r for r in results if r.get("status") == signup.RESULT_CREATED]
    print(f"\n{len(made)} account(s) created of {len(results)} attempted")
    return 0 if made or not args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
