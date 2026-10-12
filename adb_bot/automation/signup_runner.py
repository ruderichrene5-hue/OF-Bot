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

`--workers N` runs N phones at once instead of one after another -- each
`chosen` phone is a distinct profile before the loop starts, so this is just
running independent, already-separated work concurrently, not a race over who
gets which phone (that's what the per-profile lock in `_create_one` is for
regardless). Default stays 1: sequential, exactly the original behaviour.
"""

from __future__ import annotations

import argparse
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import signup
from adb_bot.automation.flows.signup_driver import AdbSignupDriver
from adb_bot.automation.flows.verification_driver import VerificationRecorder
from adb_bot.automation.signup_identity import make_identity, record_account
from adb_bot.automation.signup_preflight import BUSY_TAGS, STAGING_PREFIXES
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
#
# **The name is not evidence, and this list is not enough on its own.** On
# 2026-08-16 these two prefixes plus the old seven-tag exclusion offered eight
# phones as empty. `Default profile name (42)` was among them and was running a
# logged-in Instagram account -- feed, stories, profile picture -- and
# `Blank (5)`, `(6)`, `(7)` and `(10)` each carried 15-42 Run Log rows including
# successful `warm_up_process` runs. The tags were telling the truth
# (`Active / Posting`, `Warmup Day 3 Done`) while the name was lying, so
# `BUSY_TAGS` now lives in `signup_preflight` and is much longer, and the phone
# itself is asked before anything is created.

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
    identity = make_identity(random.Random(), model=getattr(args, "model", "") or "")

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
            launcher_client=clients.launcher,
            status_client=clients.status)
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

    if getattr(args, "phone_id", None):
        # Bypasses the `Blank`-name staging filter entirely -- for a named
        # test phone (e.g. sitting in a "Test Nikki ..." folder) that will
        # never match `_staging_profiles`'s prefix check but is still a
        # legitimate, empty target a person picked by hand.
        chosen = [i for i in items if str(i.get("id")) == args.phone_id]
        if not chosen:
            print(f"[stop] {args.phone_id} not found in the workspace.", file=sys.stderr)
            return 2
        free = chosen
    else:
        free = _staging_profiles(items)
        if not free:
            print("[stop] no staging phone is free.", file=sys.stderr)
            return 2
        chosen = free[:args.limit or 1]
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
    workers = max(1, args.workers)

    if workers == 1:
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
    else:
        # Each phone still gets its own ADBClient: ADBClient tracks a
        # target->password map per instance, and two phones running at once
        # would otherwise clobber each other's remembered credentials.
        print(f"\nrunning {len(chosen)} phone(s), {workers} at a time")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_create_one, item, clients, ADBClient(),
                                   router, args, logger): item
                      for item in chosen}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = {"profile": str(item.get("serial_name")),
                              "status": f"error: {str(exc)[:80]}", "username": ""}
                results.append(outcome)

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
    # No default, so `--list` can show the whole free pool while a run still
    # attempts exactly one account unless asked for more. Sharing a default of
    # 1 meant `--list` printed one phone and "... and 1 more", which reads like
    # the pool is smaller than it is.
    parser.add_argument("--limit", type=int, default=None,
                        help="how many accounts to attempt (default 1)")
    parser.add_argument("--apply", action="store_true",
                        help="actually launch phones, rent numbers and create "
                             "accounts. Without it nothing is touched.")
    parser.add_argument("--screenshots", action="store_true",
                        help="save a screenshot per screen (~3s each)")
    parser.add_argument("--readiness-attempts", type=int, default=8)
    parser.add_argument("--readiness-wait", type=int, default=15)
    parser.add_argument("--model", default="",
                        help="prefix the generated @handle with this name "
                             "(e.g. nikki -> nikki_1234)")
    parser.add_argument("--workers", type=int, default=1,
                        help="run this many phones at once (default 1, "
                             "sequential)")
    parser.add_argument("--phone-id", default=None,
                        help="target this exact MLX phone id instead of "
                             "auto-picking from staging `Blank` phones -- for "
                             "a named test phone that free-selection would "
                             "never see")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_list(args) if args.list else cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
