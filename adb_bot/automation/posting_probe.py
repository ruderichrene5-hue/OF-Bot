"""Post on named profiles, one at a time, with somebody watching.

    python -m adb_bot.automation.posting_probe                      # what would run
    python -m adb_bot.automation.posting_probe --apply "Jil 8" "Laila 4"
    python -m adb_bot.automation.posting_probe --apply --cleared    # the last review's clean list

Why this exists rather than "just run the posting loop": a profile flagged
`No Recent Success` or `Retries Exhausted` is skipped by the planner, and both
of those reasons *are* "this profile is not posting". The flag blocks the only
event that could clear it, so the profile parks forever -- on 2026-08-14,
`Jil 6`, `Jil 8` and `Nikki 12` had each been skipped more than eleven thousand
times and attempted **zero** times in six days. Nothing about those accounts was
ever tested; they were only ever refused.

So this runs the real posting path (`run_posting_queue`, the real flow, the real
write-back) with two differences:

* **one profile at a time**, so the log is readable and a bad screen is
  attributable to one phone rather than to whichever of ten was talking;
* **the Needs Human Check gate off**, which is why it must never be on a timer.

The gate exists for a good reason -- posting into a real Instagram checkpoint is
how an account gets acted on -- so the default target is the flag review's
`cleared` list: profiles positively observed to be healthy. Naming a profile by
hand overrides that, which is the supervised part.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.automation.run_loop import _airtable, _mlx_token
from adb_bot.config import settings
from adb_bot.core.logger import get_logger

PROBE_DIR = Path.home() / ".adb_bot" / "posting_probe"
REVIEW_DIR = Path.home() / ".adb_bot" / "flag_review"


def latest_cleared() -> list:
    """Names from the most recent flag review's `cleared` list.

    Those are the profiles a screen read positively called healthy, which is the
    only set it is reasonable to post on with the flag gate off.
    """
    reviews = sorted(REVIEW_DIR.glob("review-*.json"))
    if not reviews:
        return []
    data = json.loads(reviews[-1].read_text())
    return [str(r.get("name")) for r in data.get("cleared") or []]


def resolve(airtable, names) -> tuple:
    """(launch ids, unmatched names) for `names`, matched on the profile name.

    Matched case-insensitively because the names are typed by a person, and
    returned with the misses rather than silently shortened: a run that quietly
    posts on four of the five profiles asked for is a run whose result cannot be
    read.
    """
    wanted = {str(n).strip().lower(): str(n).strip() for n in names if str(n).strip()}
    found, ids = {}, []
    for row in airtable.posting_profiles():
        key = str(row.get("name") or "").strip().lower()
        launch_id = str(row.get("launch_id") or "").strip()
        if key in wanted and launch_id:
            found[key] = row
            ids.append(launch_id)
    missing = [original for key, original in wanted.items() if key not in found]
    return ids, missing, [found[k] for k in found]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the real posting flow on named profiles, supervised.")
    parser.add_argument("names", nargs="*", help="profile names, e.g. 'Jil 8'")
    parser.add_argument("--cleared", action="store_true",
                        help="use the latest flag review's healthy list")
    parser.add_argument("--apply", action="store_true",
                        help="actually post. Without it, prints the plan only.")
    parser.add_argument("--respect-flags", action="store_true",
                        help="leave the Needs Human Check gate ON. The run then "
                             "does nothing for a flagged profile -- which is the "
                             "state this tool exists to get out of.")
    parser.add_argument("--max-concurrent", type=int, default=1,
                        help="profiles at once (default 1, so the log is readable)")
    parser.add_argument("--posts-per-profile", type=int, default=1,
                        help="posts per phone this run (default 1). A parked "
                             "profile has a backlog -- Jil 8 had 17 rows waiting "
                             "-- and everything due runs sequentially on one "
                             "launch, so uncapped means emptying it in one go.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--airtable-token")
    parser.add_argument("--base-id")
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    airtable = _airtable(args.base_id, args.airtable_token)

    names = list(args.names)
    if args.cleared:
        names.extend(latest_cleared())
    if not names:
        print("Name at least one profile, or pass --cleared.", file=sys.stderr)
        return 2

    launch_ids, missing, rows = resolve(airtable, names)
    print(f"\n{'=' * 70}")
    print("APPLY -- this will post" if args.apply else "DRY RUN -- nothing will post")
    print(f"{'=' * 70}")
    for row in sorted(rows, key=lambda r: str(r.get("name"))):
        flag = "FLAGGED" if row.get("needs_human") else "clear"
        print(f"  {str(row.get('name')):22} {row.get('launch_id')}  {flag:8} "
              f"{row.get('reason') or ''}")
    for name in missing:
        print(f"  {name:22} -- NO MATCHING PROFILE ROW, skipped")
    if not launch_ids:
        print("\nNothing to run.")
        return 1

    if not args.apply:
        print(f"\n{len(launch_ids)} profile(s) would run "
              f"{'with' if args.respect_flags else 'WITHOUT'} the flag gate.")
        return 0

    from adb_bot.automation.bootstrap import build_automation, build_mlx_clients
    from adb_bot.automation.posting_runner import run_posting_queue

    clients = build_mlx_clients(_mlx_token(args.mlx_token))
    result = run_posting_queue(
        airtable, clients.launcher, clients.shutdown, clients.adb_enable,
        clients.api, build_automation(), logger,
        readiness_wait_seconds=settings.get_saved_readiness_wait(),
        readiness_max_attempts=settings.get_saved_readiness_attempts(),
        batch_launch_delay_seconds=settings.get_saved_batch_launch_delay(),
        selected_launch_ids=set(launch_ids),
        max_concurrent_profiles=args.max_concurrent,
        ignore_needs_human=not args.respect_flags,
        max_posts_per_profile=args.posts_per_profile,
    )
    print(f"\n{'=' * 70}\nposting result: {result}\n{'=' * 70}")

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = PROBE_DIR / f"probe-{stamp}.json"
    path.write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(),
         "names": names, "launch_ids": launch_ids, "missing": missing,
         "respected_flags": bool(args.respect_flags),
         "result": result}, indent=2, default=str))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
