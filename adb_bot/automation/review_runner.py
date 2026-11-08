"""Recheck `review`-tagged profiles: is the account clear again?

    python -m adb_bot.automation.review_runner              # dry run
    python -m adb_bot.automation.review_runner --apply      # launch, look, retag

Rene, 2026-09-24: the other half of the challenge lifecycle
`verification_runner` doesn't cover. That pass works a `Human Verification`
tag and, once it can't do anything more (solved, stuck, still under Meta's
own appeal review), tags the profile `review` and pulls `Active / Posting`
(see `verification_runner._record_review_state`). Nothing after that point
ever looked at a `review` tag again -- this is that look, meant to run every
two hours.

**Never solves anything and never spends a penny.** Same `act=False` driver
as `flag_review`: it opens Instagram, reads one screen, and decides only
whether the account is healthy now. A profile still mid-challenge is left
exactly as it was, for `verification_runner`'s next pass to keep working.

On a clean screen:
  - `Setup` tag present -> the account was already built (picture, a post,
    the link) before the challenge hit. `review` and `Human Verification`
    come off, `Active / Posting` goes back on -- the account resumes
    exactly where it left off.
  - `Setup` tag absent -> the account was mid-setup when the challenge hit,
    or this is a fresh account that was never finished. `review` comes off
    (the challenge itself is resolved), but `Active / Posting` is
    deliberately NOT added -- an unfinished account has no post to be
    "active" on. Reported as `needs-setup` and left there; wiring the setup
    step itself onto this outcome is the next piece, not yet built.

Reuses `flag_review`'s screen-reading (`_look`) rather than a second copy of
the same driver/classifier plumbing -- the two passes ask the same question
("is this profile healthy right now?") of two different populations.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flag_review import _look, is_model_profile
from adb_bot.automation.verification_probe import _profiles, _resolve_token
from adb_bot.automation.verification_runner import ACTIVE_POSTING_TAG, REVIEW_TAG
from adb_bot.clients.adb import ADBClient
from adb_bot.core.logger import get_logger

HAND_APPLIED_VERIFICATION_TAG = "Human Verification"
SETUP_TAG = "Setup"

REVIEW_DIR = Path.home() / ".adb_bot" / "review_runner"


def select(items) -> list:
    """`review`-tagged model profiles, name-sorted."""
    flagged = [i for i in items
               if REVIEW_TAG in (i.get("tags") or []) and is_model_profile(i)]
    return sorted(flagged, key=lambda i: str(i.get("serial_name") or ""))


def _retag(tag_client, logger, item, outcome) -> None:
    """Act on a clean screen: `review` always comes off; what goes back on
    depends on whether the account was ever finished. Never raises.
    """
    tags = set(item.get("tags") or [])
    try:
        tag_ids = tag_client.tag_ids_by_name()

        for name in (REVIEW_TAG, HAND_APPLIED_VERIFICATION_TAG):
            if name in tags:
                tag_id = tag_ids.get(name.lower())
                if tag_id:
                    tag_client.unassign(outcome["id"], [tag_id])

        if SETUP_TAG in tags:
            active_id = tag_ids.get(ACTIVE_POSTING_TAG.lower())
            if active_id and tag_client.assign(outcome["id"], [active_id]):
                outcome["verdict"] = "clean-resumed"
                logger.info("review pass: %s is clean and set up -- back on %s",
                           outcome["name"], ACTIVE_POSTING_TAG)
            else:
                outcome["verdict"] = "clean-resume-failed"
                logger.warning("review pass: %s is clean but could not be "
                               "retagged %s", outcome["name"], ACTIVE_POSTING_TAG)
        else:
            outcome["verdict"] = "needs-setup"
            logger.info("review pass: %s is clean but was never set up -- "
                        "left off %s, needs the setup step", outcome["name"],
                        ACTIVE_POSTING_TAG)
    except Exception as exc:
        outcome["verdict"] = "clean-retag-error"
        logger.warning("review pass: retagging %s failed (%s)",
                       outcome["name"], exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recheck review-tagged profiles; resume the clean ones.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--apply", action="store_true",
                        help="launch the phones and retag clean profiles. "
                             "Without it, nothing is touched.")
    parser.add_argument("--limit", type=int, default=0,
                        help="check at most this many (0 = all)")
    parser.add_argument("--readiness-attempts", type=int, default=8)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)

    to_check = select(items)
    if args.limit:
        to_check = to_check[:args.limit]

    print(f"\n{'=' * 70}\n{'APPLY -- clean profiles will be retagged' if args.apply else 'DRY RUN -- nothing will be touched'}")
    print(f"{len(to_check)} {REVIEW_TAG!r}-tagged model profile(s) to check\n{'=' * 70}")

    if not args.apply:
        for item in to_check:
            print(f"  {str(item.get('serial_name')):22} {item.get('id')}  would check")
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
            _retag(tag_client, logger, item, outcome)
            print(f"  {outcome['verdict']}")
        else:
            print(f"  {outcome['verdict']} ({outcome['screen'] or '-'}) -- left in review")
        results.append(outcome)

    resumed = [r for r in results if r["verdict"] == "clean-resumed"]
    needs_setup = [r for r in results if r["verdict"] == "needs-setup"]
    print(f"\n{'=' * 70}\n{len(resumed)} resumed on {ACTIVE_POSTING_TAG}, "
          f"{len(needs_setup)} clean but need setup, "
          f"{len(results) - len(resumed) - len(needs_setup)} still not clean\n{'=' * 70}")

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = REVIEW_DIR / f"review-runner-{stamp}.json"
    path.write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(), "results": results}, indent=2))
    print(f"\nfull result written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
