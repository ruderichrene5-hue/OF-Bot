"""Create accounts on blank phones, a batch at a time, with a person watching.

    python -m adb_bot.automation.signup_runner --list
    python -m adb_bot.automation.signup_runner --limit 3            # dry run
    python -m adb_bot.automation.signup_runner --limit 3 --apply    # spends money

**Deliberately not a loop, and deliberately not on a timer.** Account creation
is the most policed action on the platform; an unattended run that goes wrong
does not waste one phone, it burns a batch of staging profiles and the SMS
balance before anyone reads a log. `verification_runner` made the same call for
a cheaper operation and has no timer either.

Dry-run by default: it picks the phones, invents the identities and prints what
it would do, without launching anything or spending a cent.

Each account is created inside **one** launch of one phone, because there is no
resuming -- a restarted Instagram returns to "Join Instagram" and throws away
an already-verified number.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import signup
from adb_bot.automation.flows.signup_driver import AdbSignupDriver
from adb_bot.automation.flows.verification_driver import VerificationRecorder
from adb_bot.automation.signup_identity import make_identity, record_account
from adb_bot.automation.verification_probe import (
    _open_instagram,
    _profiles,
    _resolve_token,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

# A phone that has nothing on it yet. `Blank (NN)` and `Default profile name
# (NN)` are MultiLogin's own names for a profile nobody has renamed.
STAGING_PREFIXES = ("blank", "default profile")

# Tags that mean "not free", whatever the name says. `Issue` parks a profile for
# a person; `Created` means it is already somebody's account and is a warm-up
# target; the rest are states of an account that exists.
BUSY_TAGS = ("Issue", "Created", "Account creation done", "gmail", "review",
             "Banned / Dead", "logged out")

# Two consecutive failures that are not about one account -- an empty wallet, a
# provider refusing, MultiLogin not starting phones -- end the batch. Carrying
# on would spend the rest of the budget proving the same thing.
MAX_CONSECUTIVE_FAILURES = 2

# One account's whole chain, launch included. Past this the phone is the
# problem, not the account.
PER_ACCOUNT_SECONDS = 600


def _staging_profiles(items) -> list:
    out = []
    for item in items:
        name = str(item.get("serial_name") or "")
        if not name.lower().startswith(STAGING_PREFIXES):
            continue
        tags = item.get("tags") or []
        if any(tag in tags for tag in BUSY_TAGS):
            continue
        out.append(item)
    return sorted(out, key=lambda i: str(i.get("serial_name") or ""))


def cmd_list(args) -> int:
    items = _profiles(_resolve_token(args.mlx_token))
    free = _staging_profiles(items)
    print(f"{len(free)} staging phone(s) with nothing on them, of {len(items)} "
          f"profiles\n")
    for item in free[:args.limit or 40]:
        print(f"  {str(item.get('serial_name')):26} {item.get('id')}  "
              f"tags={item.get('tags') or []}")
    if len(free) > (args.limit or 40):
        print(f"  ... and {len(free) - (args.limit or 40)} more")
    return 0


def _create_one(profile_item, clients, adb_client, router, args, logger) -> dict:
    """One account, start to finish, on one phone. Always shuts the phone down."""
    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    identity = make_identity(random.Random())

    print(f"\n{'=' * 66}\n{name} ({profile_id})\n  {identity.summary()}")
    if not args.apply:
        print("  DRY RUN -- nothing launched, no number rented")
        return {"profile": name, "status": "dry-run", "username": identity.username}

    if not locks.acquire(profile_id, owner="signup"):
        print("  busy -- another loop holds its lock")
        return {"profile": name, "status": "busy", "username": ""}

    # Written down **before** the phone is touched, not after success. The
    # 2026-08-13 run that created `@hanna.sommer33` ended `stuck` on a late
    # screen, so this was never reached -- the account exists and its generated
    # password does not, anywhere. An account whose password was only ever in
    # memory is the state sixteen fleet profiles are already in.
    record_account(profile_id, name, identity, status="attempting")

    recorder = VerificationRecorder(f"signup-{name}", logger=logger)
    started = time.monotonic()
    try:
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=args.readiness_attempts,
            wait_seconds=args.readiness_wait,
            launcher_client=clients.launcher)
        if not profile:
            return {"profile": name, "status": "not-ready", "username": ""}

        target = connect_with_retries(adb_client, profile, logger, profile_id,
                                      max_attempts=5, retry_delay_seconds=5)
        if not target:
            return {"profile": name, "status": "unreachable", "username": ""}

        if not _open_instagram(target, adb_client, logger):
            return {"profile": name, "status": "no-instagram", "username": ""}

        driver = AdbSignupDriver(target, adb_client, logger=logger,
                                 recorder=recorder, act=True,
                                 # Screenshots cost ~3s each and the phone only
                                 # lives ~15 minutes; the dumps carry the run.
                                 screenshots=args.screenshots)
        result = signup.run_signup(driver, router, identity, logger=logger)

        if result.ok:
            path = record_account(profile_id, name, identity,
                                  phone_number=result.phone_number,
                                  status="created")
            print(f"  CREATED @{identity.username} "
                  f"({result.numbers_used} number(s)) -- credentials in {path}")
        else:
            # Keep the credentials and say how far it got: a half-made account
            # is still an account somebody may have to log into.
            record_account(profile_id, name, identity,
                           phone_number=result.phone_number,
                           status=result.status)
            print(f"  {result.status}: {result.detail[:160]}")
        return {"profile": name, "status": result.status,
                "username": identity.username, "numbers": result.numbers_used,
                "elapsed": int(time.monotonic() - started),
                "recording": str(recorder.dir or "")}
    finally:
        try:
            clients.shutdown.shutdown_profiles([profile_id])
        except Exception as exc:
            logger.warning("signup: shutdown failed for %s (%s)", profile_id, exc)
        locks.release(profile_id)


def cmd_run(args) -> int:
    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)
    free = _staging_profiles(items)

    if not free:
        print("[stop] no staging phone is free.", file=sys.stderr)
        return 2

    chosen = free[:args.limit]
    mode = "APPLY (spends money, creates real accounts)" if args.apply else "DRY RUN"
    print(f"\n{'=' * 66}\n{mode}\n{len(chosen)} phone(s) of {len(free)} free\n"
          f"{'=' * 66}")

    router = None
    if args.apply:
        from adb_bot.clients.sms.router import build_router
        router = build_router(logger=logger)

    clients = build_mlx_clients(token)
    adb_client = ADBClient()

    results = []
    consecutive = 0
    for item in chosen:
        outcome = _create_one(item, clients, adb_client, router, args, logger)
        results.append(outcome)
        if outcome["status"] in (signup.RESULT_CREATED, "dry-run"):
            consecutive = 0
        else:
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"\n[stop] {consecutive} failures in a row -- something "
                      f"bigger than one account is wrong. Stopping the batch.")
                break

    made = [r for r in results if r["status"] == signup.RESULT_CREATED]
    print(f"\n{'=' * 66}\n{len(made)} account(s) created of {len(results)} attempted")
    for outcome in results:
        print(f"  {outcome['profile']:26} {outcome['status']:16} "
              f"{('@' + outcome['username']) if outcome.get('username') else ''}")
    if made:
        print("\nEach one still needs a person: bio, profile picture, and a "
              "first post made by hand.\nNone of them has a recovery email -- "
              "see SIGNUP_RUN_2026-08-13.md §4a.")
    return 0 if made or not args.apply else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create Instagram accounts on blank phones, supervised.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--list", action="store_true",
                        help="show the staging phones that are free, then stop")
    parser.add_argument("--limit", type=int, default=1,
                        help="how many accounts to attempt (default 1)")
    parser.add_argument("--apply", action="store_true",
                        help="actually launch phones, rent numbers and create "
                             "accounts. Without it nothing is touched.")
    parser.add_argument("--screenshots", action="store_true",
                        help="save a screenshot per screen (~3s each)")
    parser.add_argument("--readiness-attempts", type=int, default=8)
    parser.add_argument("--readiness-wait", type=int, default=15)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_list(args) if args.list else cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
