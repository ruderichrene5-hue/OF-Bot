"""The recheck pass `verification_runner.py` documented but never built.

    python -m adb_bot.automation.review_recheck              # dry run
    python -m adb_bot.automation.review_recheck --apply       # look, retag

When a challenge attempt can't resolve a profile on the spot, the verification
pass pulls `Active / Posting`, tags `review`, and leaves the account for later
-- see `verification_runner.py`'s `_record_review_state` docstring, which
names this exact module as the half that was still missing. This is that half:

    clean screen         -> untag `review`, put `Active / Posting` back on
    signed out           -> untag `review`, tag `Locked Out` instead
    anything else        -> leave `review` on; a person, or the next tick of
                            this same pass, looks again

Read-only, same as `flag_review`: `AdbChallengeDriver` runs with `act=False`,
so a stuck challenge is never nudged, only read.

**Deliberately excludes `Setup`-tagged profiles.** `review` gets reused there
for a different lifecycle stage -- a freshly created account awaiting a first
human look, never yet `Active / Posting` -- and retagging one of those
`Active / Posting` on a clean screen would promote it before anyone decided it
was ready. Those are printed separately, untouched.
"""

from __future__ import annotations

import argparse
import json
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
from adb_bot.automation.verification_runner import ACTIVE_POSTING_TAG, REVIEW_TAG
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

SETUP_TAG = "Setup"
LOCKED_OUT_TAG = "Locked Out"

# Already-named states a screen read must not overrule -- same caution as
# flag_review's DIAGNOSED list, for the same reason (Laila 5).
DIAGNOSED = ("logged out", "locked out", "unable to verify", "banned",
             "banned / dead", "banned/dead", "banned / blocked", "banned/blocked")

RESULT_DIR = Path.home() / ".adb_bot" / "review_recheck"


def select(items) -> tuple:
    """(to check, skipped-as-setup, skipped-as-diagnosed) among `review`-tagged
    profiles."""
    reviewing = [i for i in items if REVIEW_TAG in
                [str(t).strip().lower() for t in (i.get("tags") or [])]
                or REVIEW_TAG.title() in (i.get("tags") or [])]
    to_check, setup, diagnosed = [], [], []
    for item in reviewing:
        tags_lower = [str(t).strip().lower() for t in (item.get("tags") or [])]
        if SETUP_TAG.lower() in tags_lower:
            setup.append(item)
        elif any(t in DIAGNOSED for t in tags_lower):
            diagnosed.append(item)
        else:
            to_check.append(item)
    key = lambda i: str(i.get("serial_name") or "")
    return (sorted(to_check, key=key), sorted(setup, key=key),
            sorted(diagnosed, key=key))


def _look(profile_item, clients, adb_client, args, logger) -> dict:
    """Launch one phone, read one screen, shut it down again. Never taps."""
    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    out = {"name": name, "id": profile_id, "verdict": "", "screen": "", "text": ""}

    if not locks.acquire(profile_id, owner="review-recheck"):
        out["verdict"] = "busy"
        return out

    try:
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=args.readiness_attempts,
            wait_seconds=args.readiness_wait,
            launcher_client=clients.launcher,
            status_client=clients.status)
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

        driver = AdbChallengeDriver(target, adb_client, logger=logger,
                                    act=False, screenshots=False)
        text = driver.read_screen()
        if verification.classify_challenge(text) == verification.CHALLENGE_NONE \
                and not verification.screen_is_healthy(text):
            time.sleep(6)
            text = driver.read_screen()

        challenge = verification.classify_challenge(text)
        out["screen"] = challenge
        out["text"] = " ".join(str(text or "").split())[:200]

        if verification.looks_like_launcher(text):
            out["verdict"] = "instagram-not-in-front"
        elif challenge == verification.CHALLENGE_SIGNED_OUT:
            out["verdict"] = "logged-out"
        elif challenge == verification.CHALLENGE_NONE and verification.screen_is_healthy(text):
            out["verdict"] = "clean"
        else:
            out["verdict"] = "still-stuck"
        return out
    except Exception as exc:
        out["verdict"] = f"error: {str(exc)[:80]}"
        return out
    finally:
        try:
            clients.shutdown.shutdown_profiles([profile_id])
        except Exception as exc:
            logger.warning("review recheck: shutdown failed for %s (%s)", profile_id, exc)
        locks.release(profile_id)


def _retag(tag_client, logger, outcome, *, drop: str, add: str | None) -> bool:
    try:
        tag_ids = tag_client.tag_ids_by_name()
        ok = True
        drop_id = tag_ids.get(drop.lower())
        if drop_id:
            ok = tag_client.unassign(outcome["id"], [drop_id]) and ok
        else:
            logger.warning("review recheck: workspace has no %r tag", drop)
            ok = False
        if add:
            add_id = tag_ids.get(add.lower())
            if add_id:
                ok = tag_client.assign(outcome["id"], [add_id]) and ok
            else:
                logger.warning("review recheck: workspace has no %r tag", add)
                ok = False
        return ok
    except Exception as exc:
        logger.warning("review recheck: retagging %s failed (%s)", outcome["name"], exc)
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mlx-token")
    parser.add_argument("--apply", action="store_true",
                        help="launch the phones and retag them. Without it, "
                             "nothing is touched.")
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--all", action="store_true",
                        help="also check Setup+review and already-diagnosed "
                             "profiles -- read-only report for those, never "
                             "retagged (no defined transition for that "
                             "lifecycle stage, or a diagnosis already stands)")
    parser.add_argument("--readiness-attempts", type=int, default=8)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)

    to_check, setup, diagnosed = select(items)

    # report_only: checked and printed, but never retagged -- either the
    # clean->Active/Posting transition doesn't apply to their lifecycle stage
    # (Setup+review), or a diagnosis already stands and one screenshot must
    # not overrule it (same rule flag_review uses, for the same reason).
    report_only = []
    if args.all:
        report_only = list(setup) + list(diagnosed)
        to_check = sorted(to_check + report_only, key=lambda i: str(i.get("serial_name") or ""))
        setup, diagnosed = [], []

    report_only_ids = {str(i.get("id")) for i in report_only}
    retag_eligible_ids = {str(i.get("id")) for i in to_check} - report_only_ids

    if args.limit:
        to_check = to_check[:args.limit]

    print(f"\n{'=' * 70}\n"
          f"{'APPLY -- retagging based on what the screen shows' if args.apply else 'DRY RUN -- nothing will be touched'}")
    print(f"{len(to_check)} `{REVIEW_TAG}`-tagged profile(s) to check"
          + (f" ({len(report_only)} of them report-only, no retag)" if report_only else "")
          + (f"; {len(setup)} skipped (also `{SETUP_TAG}`)" if setup else "")
          + (f"; {len(diagnosed)} skipped (already diagnosed)" if diagnosed else "")
          + f"\n{'=' * 70}")
    for item in setup:
        print(f"  {str(item.get('serial_name')):22} skipped -- Setup+review")
    for item in diagnosed:
        print(f"  {str(item.get('serial_name')):22} skipped -- already diagnosed")

    if not args.apply:
        print()
        for item in to_check:
            tag = "would check, report-only" if str(item.get("id")) in report_only_ids \
                else "would check"
            print(f"  {str(item.get('serial_name')):22} {item.get('id')}  {tag}")
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
        eligible = str(item.get("id")) in retag_eligible_ids

        if not eligible:
            print(f"  {outcome['verdict']} ({outcome['screen'] or '-'}) -- report-only, no retag")
        elif outcome["verdict"] == "clean":
            outcome["retagged"] = _retag(tag_client, logger, outcome,
                                         drop=REVIEW_TAG, add=ACTIVE_POSTING_TAG)
            print(f"  clean -- {'back on Active / Posting' if outcome['retagged'] else 'RETAG FAILED'}")
        elif outcome["verdict"] == "logged-out":
            outcome["retagged"] = _retag(tag_client, logger, outcome,
                                         drop=REVIEW_TAG, add=LOCKED_OUT_TAG)
            print(f"  logged out -- {'tagged Locked Out' if outcome['retagged'] else 'RETAG FAILED'}")
        else:
            print(f"  {outcome['verdict']} ({outcome['screen'] or '-'}) -- left in review")
        results.append(outcome)

    resolved = [r for r in results if r["verdict"] in ("clean", "logged-out")
               and r["id"] in retag_eligible_ids]
    print(f"\n{'=' * 70}\n{len(resolved)} of {len(retag_eligible_ids)} retag-eligible "
          f"resolved this pass\n{'=' * 70}")
    for outcome in resolved:
        print(f"  {outcome['name']:22} {outcome['verdict']:12} {outcome['id']}")
    print("\nstill in review:")
    for outcome in results:
        if outcome not in resolved:
            note = " (report-only)" if outcome["id"] not in retag_eligible_ids else ""
            print(f"  {outcome['name']:22} {outcome['verdict']:26} {outcome['screen']}{note}")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = RESULT_DIR / f"review-recheck-{stamp}.json"
    path.write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(), "results": results,
         "skipped_setup": [str(i.get("serial_name")) for i in setup],
         "skipped_diagnosed": [str(i.get("serial_name")) for i in diagnosed]},
        indent=2))
    print(f"\nfull result written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
