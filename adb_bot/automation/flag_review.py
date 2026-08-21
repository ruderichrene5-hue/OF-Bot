"""Look at every flagged model profile and take the flag off the healthy ones.

    python -m adb_bot.automation.flag_review              # dry run: who would be checked
    python -m adb_bot.automation.flag_review --apply      # launch, look, untag the clean

**This never solves anything and never spends a penny.** The driver is built
with `act=False`, so it cannot tap or type; the run opens Instagram, reads one
screen, and decides only whether the profile still deserves its `Issue` tag.
A profile that really is mid-challenge is left exactly as it was for
`verification_runner`, which is the tool that rents numbers.

Why it exists: the `Issue` tag is applied for reasons verification cannot fix —
most of them posting failures — so a flagged profile is not evidence of a
challenge. `Jasmin 5` was tagged for `Retries Exhausted` and found perfectly
healthy; two more were the same on 2026-08-13. Those profiles sit parked, out
of the posting loop, achieving nothing until somebody looks.

The output is two lists: the ones cleared, and the ones left alone with what
was on their screen. The cleared list is the interesting one -- those are
accounts whose flag came from posting, and the place to look next is the
posting run, not the account.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import verification
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

ISSUE_TAG = "Issue"

# Staging phones are not model profiles and were never posting.
STAGING_PREFIXES = ("blank", "default profile")

# `ONBOARDING_A_MODEL.md`: a profile whose name carries `Link` is a
# link-in-bio account, not a posting target, so its flag is not a posting flag.
NOT_POSTING = ("link",)

# Tags a person (or an earlier pass) already put a diagnosis on. Reading them
# is free; launching the phone to rediscover what the tag says is two minutes.
DIAGNOSED = ("logged out", "unable to verify", "banned / dead", "banned/dead")

# What each screen means for the flag.
CLEARS_THE_FLAG = (verification.CHALLENGE_NONE,)
REAL_ISSUES = (
    verification.CHALLENGE_PHONE, verification.CHALLENGE_CODE,
    verification.CHALLENGE_PHOTO, verification.CHALLENGE_IMAGE_CAPTCHA,
    verification.CHALLENGE_CHOOSE_METHOD, verification.CHALLENGE_VERIFY_INTRO,
    verification.CHALLENGE_CONSENT, verification.CHALLENGE_BANNED,
    verification.CHALLENGE_SIGNED_OUT,
)

# Screens the bot answers by itself. Reported separately because calling one a
# "real issue" is how a profile ends up waiting on a person for a button the
# posting loop would have pressed on its next run.
SELF_CLEARABLE = (verification.CHALLENGE_LOGIN_CONFIRM,)

REVIEW_DIR = Path.home() / ".adb_bot" / "flag_review"


def is_model_profile(item) -> bool:
    name = str(item.get("serial_name") or "").strip().lower()
    if not name or name.startswith(STAGING_PREFIXES):
        return False
    return not any(word in name for word in NOT_POSTING)


def select(items) -> tuple:
    """(to check, already diagnosed) among flagged model profiles."""
    flagged = [i for i in items
               if ISSUE_TAG in (i.get("tags") or []) and is_model_profile(i)]
    to_check, diagnosed = [], []
    for item in flagged:
        tags = [str(t).strip().lower() for t in (item.get("tags") or [])]
        (diagnosed if any(t in DIAGNOSED for t in tags) else to_check).append(item)
    return (sorted(to_check, key=lambda i: str(i.get("serial_name") or "")),
            sorted(diagnosed, key=lambda i: str(i.get("serial_name") or "")))


def _look(profile_item, clients, adb_client, args, logger) -> dict:
    """Launch one phone, read one screen, and shut it down again."""
    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    out = {"name": name, "id": profile_id, "verdict": "", "screen": "",
           "text": "", "remark": str(profile_item.get("remark") or "")[:80]}

    if not locks.acquire(profile_id, owner="flag-review"):
        out["verdict"] = "busy"
        return out

    try:
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=args.readiness_attempts,
            wait_seconds=args.readiness_wait,
            launcher_client=clients.launcher)
        if not profile:
            out["verdict"] = "phone-never-ready"
            return out

        target = connect_with_retries(adb_client, profile, logger, profile_id,
                                      max_attempts=4, retry_delay_seconds=5)
        if not target:
            out["verdict"] = "unreachable"
            return out

        if not _open_instagram(target, adb_client, logger):
            out["verdict"] = "instagram-would-not-open"
            return out

        # act=False: this driver cannot tap or type, whatever it sees.
        driver = AdbChallengeDriver(target, adb_client, logger=logger,
                                    act=False, screenshots=False)
        text = driver.read_screen()
        # A second look, because a challenge can be a moment late and a first
        # read that says "nothing wrong" is exactly the mistake to avoid here.
        if verification.classify_challenge(text) == verification.CHALLENGE_NONE \
                and not verification.screen_is_healthy(text):
            time.sleep(6)
            text = driver.read_screen()

        challenge = verification.classify_challenge(text)
        out["screen"] = challenge
        out["text"] = " ".join(str(text or "").split())[:200]

        if verification.looks_like_launcher(text):
            out["verdict"] = "instagram-not-in-front"
        elif challenge in SELF_CLEARABLE:
            out["verdict"] = "self-clearable"
        elif challenge in REAL_ISSUES:
            out["verdict"] = "real-issue"
        elif challenge in CLEARS_THE_FLAG and verification.screen_is_healthy(text):
            out["verdict"] = "clean"
        else:
            # No challenge marker, but nothing that positively says the app is
            # working either. Never cleared on this: it is the reading that has
            # been wrong most often.
            out["verdict"] = "unclear"
        return out
    except Exception as exc:
        out["verdict"] = f"error: {str(exc)[:80]}"
        return out
    finally:
        try:
            clients.shutdown.shutdown_profiles([profile_id])
        except Exception as exc:
            logger.warning("flag review: shutdown failed for %s (%s)", profile_id, exc)
        locks.release(profile_id)


def _untag(tag_client, logger, outcome) -> bool:
    try:
        from adb_bot.automation.issue_tags import resolve_issue_tag_id
        tag_id = resolve_issue_tag_id(tag_client)
        if tag_id and tag_client.unassign(outcome["id"], [tag_id]):
            return True
        logger.warning("flag review: could not untag %s", outcome["name"])
    except Exception as exc:
        logger.warning("flag review: untagging %s failed (%s)", outcome["name"], exc)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Review flagged model profiles; clear the healthy ones.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--apply", action="store_true",
                        help="launch the phones and remove the tag from clean "
                             "profiles. Without it, nothing is touched.")
    parser.add_argument("--limit", type=int, default=0,
                        help="check at most this many (0 = all)")
    parser.add_argument("--readiness-attempts", type=int, default=8)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)
    to_check, diagnosed = select(items)
    if args.limit:
        to_check = to_check[:args.limit]

    print(f"\n{'=' * 70}\n{'APPLY -- clean profiles will be untagged' if args.apply else 'DRY RUN -- nothing will be touched'}")
    print(f"{len(to_check)} flagged model profile(s) to check; "
          f"{len(diagnosed)} skipped as already diagnosed\n{'=' * 70}")
    for item in diagnosed:
        tags = [t for t in (item.get("tags") or []) if str(t).lower() in DIAGNOSED]
        print(f"  {str(item.get('serial_name')):22} skipped -- tagged {tags}")

    if not args.apply:
        print()
        for item in to_check:
            print(f"  {str(item.get('serial_name')):22} {item.get('id')}  "
                  f"would check")
        return 0

    clients = build_mlx_clients(token)
    adb_client = ADBClient()
    tag_client = getattr(clients, "tags", None)
    if tag_client is None:
        from adb_bot.clients.multilogin.tags import MultiloginTagClient
        tag_client = MultiloginTagClient(token)

    results = []
    for index, item in enumerate(to_check, start=1):
        print(f"\n[{index}/{len(to_check)}] {item.get('serial_name')}")
        outcome = _look(item, clients, adb_client, args, logger)
        if outcome["verdict"] == "clean":
            outcome["untagged"] = _untag(tag_client, logger, outcome)
            print(f"  clean -- {'tag removed' if outcome['untagged'] else 'UNTAG FAILED'}")
        else:
            print(f"  {outcome['verdict']} ({outcome['screen'] or '-'}) "
                  f"-- left flagged")
        results.append(outcome)

    cleared = [r for r in results if r["verdict"] == "clean"]
    print(f"\n{'=' * 70}\n{len(cleared)} of {len(results)} were healthy and have "
          f"been unflagged\n{'=' * 70}")
    for outcome in cleared:
        print(f"  {outcome['name']:22} {outcome['id']}")
    print("\nleft flagged:")
    for outcome in results:
        if outcome["verdict"] != "clean":
            print(f"  {outcome['name']:22} {outcome['verdict']:26} "
                  f"{outcome['screen']}")

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = REVIEW_DIR / f"review-{stamp}.json"
    path.write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(),
         "cleared": cleared, "all": results,
         "skipped_diagnosed": [{"name": str(i.get("serial_name")),
                                "id": str(i.get("id")),
                                "tags": i.get("tags") or []} for i in diagnosed]},
        indent=2))
    print(f"\nfull result written to {path}")
    print("The cleared list is the one to post on next: their flags came from "
          "posting, not from the account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
