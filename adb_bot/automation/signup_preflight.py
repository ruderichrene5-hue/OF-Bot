"""Ask each candidate phone whether it is actually empty, before anything is created.

    python -m adb_bot.automation.signup_preflight --candidates          # what it would check
    python -m adb_bot.automation.signup_preflight --check               # launches phones
    python -m adb_bot.automation.signup_preflight --check --only "Blank (21)"

**Why this exists.** `signup_runner` picked its phones by *name*: anything called
`Blank (NN)` or `Default profile name (NN)` that did not carry one of seven
"busy" tags. On 2026-08-16 that list offered eight phones, and looking at them
found:

* `Default profile name (42)` -- offered as empty -- running a **logged-in
  Instagram account**, feed, story avatar and all;
* `Blank (5)`, `(6)`, `(7)`, `(10)` carrying 15-42 Run Log rows each, including
  successful `warm_up_process` runs, which only an account can do.

A name is not evidence. Worse, handing a phone that already has an account to
`run_signup` produces a **false success**: the flow's first look sees a healthy
feed, classifies it `done`, and reports `CREATED @<invented-username>` for an
account that was never made, on somebody else's phone.

So blankness is decided the only way it can honestly be decided -- by opening
Instagram on the phone and reading the screen. Three sources are consulted
first, purely to keep that expensive check off phones that are obviously taken:

* MLX tags -- `Active / Posting` is the fleet's own word for "this one is live",
  and it was right about `(42)` while the name was wrong;
* the Run Log, which is profile-keyed and is the one link that works;
* the prod Profiles table's account links and posting queue.
"""

from __future__ import annotations

import argparse
import sys
import time

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import signup
from adb_bot.automation.flows.verification_driver import AdbChallengeDriver
from adb_bot.automation.verification_probe import (
    _open_instagram,
    _profiles,
    _resolve_token,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

# What the phone turned out to be.
BLANK = "blank"                 # signed out -- an account can be created here
OCCUPIED = "occupied"           # somebody's account is logged in on it
BANNED = "banned"               # an account is on it and it is disabled
UNCLEAR = "unclear"             # a screen nobody has named -- do not use it
UNREACHABLE = "unreachable"     # never came up, or died before it could be read

# Tags that mean a person or a loop has claimed this phone. Every tag the fleet
# actually uses is in here except the three that are plainly about nothing
# (`Task A`, `Task B`, `tests`), because the failure that matters is treating a
# claimed phone as free -- not the reverse.
BUSY_TAGS = frozenset({
    "Issue", "Created", "Account creation done", "gmail", "review",
    "Banned / Dead", "logged out",
    # Added 2026-08-16, all seen on phones the old list called empty:
    "Active / Posting",             # `Default profile name (42)`: live account
    "Ready for Posting",
    "Second Account", "2 accounts",
    "unable to verify",
    "Link",
    "Warmup ready, need Bio and Pic",
    "Warmup Day 2 Done", "Warmup Day 3 Done",   # `Blank (5)`, `Blank (6)`
})

STAGING_PREFIXES = ("blank", "default profile")


def looks_free(item, run_log_names=(), prod_by_mlx=None) -> tuple[bool, str]:
    """Whether this profile is worth spending a launch on, and why not if not.

    Cheap checks only -- the verdict is the phone itself.
    """
    name = str(item.get("serial_name") or "")
    tags = set(item.get("tags") or [])

    busy = sorted(tags & BUSY_TAGS)
    if busy:
        return False, f"tagged {', '.join(busy)}"

    if name in set(run_log_names):
        return False, "has Run Log history (a flow has run on it)"

    prod = (prod_by_mlx or {}).get(str(item.get("id")), {})
    if prod.get("Accounts"):
        return False, "linked to an account in the base"
    if prod.get("Posting Queue"):
        return False, "has posting queue rows"

    return True, ""


def check_phone(profile_item, clients, adb_client, logger, args) -> dict:
    """Launch one phone, read Instagram's first screen, shut it down."""
    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    out = {"profile": name, "id": profile_id, "state": UNREACHABLE, "text": ""}

    if not locks.acquire(profile_id, owner="signup-preflight"):
        out["state"] = UNREACHABLE
        out["text"] = "busy -- another loop holds its lock"
        return out

    try:
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=args.readiness_attempts,
            wait_seconds=args.readiness_wait,
            launcher_client=clients.launcher)
        if not profile:
            out["text"] = "never became ADB-ready"
            return out

        target = connect_with_retries(adb_client, profile, logger, profile_id,
                                      max_attempts=5, retry_delay_seconds=5)
        if not target:
            out["text"] = "could not be reached over ADB"
            return out

        if not _open_instagram(target, adb_client, logger):
            out["text"] = "Instagram would not open"
            return out

        driver = AdbChallengeDriver(target, adb_client, logger=logger,
                                    act=False, screenshots=False)
        # Read more than once: the first dump after a launch routinely catches
        # the app mid-draw, and a half-drawn screen is exactly what gets
        # misnamed.
        text = ""
        for attempt in range(3):
            text = driver.read_screen() or ""
            if text.strip() and signup.classify_signup_screen(text) not in (
                    signup.SCREEN_LOADING, signup.SCREEN_UNKNOWN):
                break
            time.sleep(6)

        out["text"] = text[:600]
        if not text.strip():
            out["text"] = "the phone stopped answering"
            return out

        screen = signup.classify_signup_screen(text)
        if screen == signup.SCREEN_ENTRY:
            out["state"] = BLANK
        elif screen == signup.SCREEN_DONE:
            out["state"] = OCCUPIED
        elif screen == signup.SCREEN_BANNED:
            out["state"] = BANNED
        else:
            out["state"] = UNCLEAR
        out["screen"] = screen
        return out
    except Exception as exc:            # one bad phone must not end the sweep
        logger.warning("preflight: %s failed (%s)", name, exc)
        out["text"] = str(exc)[:200]
        return out
    finally:
        try:
            clients.shutdown.shutdown_profiles([profile_id])
        except Exception as exc:
            logger.warning("preflight: shutdown failed for %s (%s)",
                           profile_id, exc)
        locks.release(profile_id)


def _candidates(items, args) -> list:
    out = []
    for item in items:
        name = str(item.get("serial_name") or "")
        if args.only and args.only.lower() not in name.lower():
            continue
        if not args.any_name and not name.lower().startswith(STAGING_PREFIXES):
            continue
        free, why = looks_free(item)
        out.append((item, free, why))
    return sorted(out, key=lambda row: str(row[0].get("serial_name") or ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask each candidate phone whether it is really empty.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--candidates", action="store_true",
                        help="list what would be checked, launch nothing")
    parser.add_argument("--check", action="store_true",
                        help="launch each candidate and read its screen")
    parser.add_argument("--only", help="substring of one profile name")
    parser.add_argument("--any-name", action="store_true",
                        help="do not require a Blank/Default profile name")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--readiness-attempts", type=int, default=8)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)
    rows = _candidates(items, args)

    print(f"\n{len(rows)} candidate(s) of {len(items)} MLX profiles\n")
    for item, free, why in rows:
        mark = "free?" if free else "SKIP "
        print(f"  {mark} {str(item.get('serial_name')):28} {item.get('id')}"
              f"  {why}")

    if not args.check:
        print("\n(nothing was launched -- pass --check to look at the phones)")
        return 0

    worth_it = [item for item, free, _ in rows if free][:args.limit]
    if not worth_it:
        print("\n[stop] nothing worth launching.", file=sys.stderr)
        return 2

    clients = build_mlx_clients(token)
    adb_client = ADBClient()
    results = []
    for item in worth_it:
        name = str(item.get("serial_name"))
        print(f"\n{'=' * 66}\nchecking {name}")
        outcome = check_phone(item, clients, adb_client, logger, args)
        results.append(outcome)
        print(f"  -> {outcome['state']}  {outcome.get('screen', '')}")
        if outcome["state"] not in (BLANK,):
            print(f"     {outcome['text'][:200]}")

    print(f"\n{'=' * 66}\nverdicts")
    for outcome in results:
        print(f"  {outcome['profile']:28} {outcome['state']}")
    blank = [r for r in results if r["state"] == BLANK]
    print(f"\n{len(blank)} phone(s) confirmed empty and safe to create on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
