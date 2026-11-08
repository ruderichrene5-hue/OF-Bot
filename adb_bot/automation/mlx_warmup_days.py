"""The 4-day MLX warm-up schedule, tag-driven, no Airtable.

Confirmed protocol, 2026-09-25 (Rene):

    Day 1: 10 min scroll, then close.
    Day 2: 10 min scroll, THEN set the profile picture.
    Day 3: 10 min scroll, THEN set the bio.
    Day 4: 10 min scroll, THEN post the account's first reel.

Each day is a separate invocation, on a separate calendar day -- this is a
warm-up, not four things done back to back in one sitting. What day a profile
is on is read from its own MLX tags (`warmup day N done`), the same tags this
workspace already had before this module existed, so a first run against an
untagged `Warmup` profile is unambiguously Day 1, and running this again
tomorrow against the same profile picks up at Day 2 on its own.

Day 2 and Day 3 need content this module does not invent: a picture file and
bio text are required arguments once a profile reaches that day. Day 4 needs
a media file for the reel. A profile that reaches its day without the content
it needs is left exactly as it was -- tagged for that day, not skipped ahead
-- and reported so a person can supply it.

    python -m adb_bot.automation.mlx_warmup_days <phone_id> [<phone_id> ...] \\
        --picture path/to/pic.jpg --bio "text" --media path/to/reel.mp4 \\
        [--workers=N]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from extract_ig_session import ready_profile_mlx  # noqa: E402

from adb_bot.automation.flows.instagram import (  # noqa: E402
    InstagramUpdateBioU2Flow,
    InstagramUpdateProfilePictureU2Flow,
    InstagramWarmUpDay1Flow,
)
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow  # noqa: E402
from adb_bot.clients.adb import ADBClient  # noqa: E402
from adb_bot.clients.multilogin import MultiloginShutdownClient  # noqa: E402
from adb_bot.clients.multilogin.tags import MultiloginTagClient  # noqa: E402
from adb_bot.config import settings  # noqa: E402
from adb_bot.core.logger import get_logger  # noqa: E402

DAY_TAGS = {
    1: "warmup day 1 done",
    2: "warmup day 2 done",
    3: "warmup day 3 done",
    4: "warmup day 4 done",
}


def current_day(tags: list[str]) -> int:
    """Which day a profile is due for, from its own tags: the day after the
    highest `warmup day N done` it already carries, or 1 if it has none."""
    lowered = [str(t).strip().lower() for t in (tags or [])]
    done = [day for day, name in DAY_TAGS.items() if name in lowered]
    return (max(done) + 1) if done else 1


def _connect(phone_id: str, max_attempts: int = 10):
    profile = ready_profile_mlx(phone_id, max_attempts=max_attempts, wait_seconds=15)
    if profile is None:
        return None, None
    adb = ADBClient()
    for _ in range(5):
        if adb.connect(profile) and adb.authenticate(profile):
            return profile, adb
    return None, None


def run_day(phone_id: str, day: int, tags: list[str], *, picture: str | None,
           bio: str | None, media: str | None, tag_client, shutdown, logger) -> str:
    if day == 2 and not picture:
        return f"day 2 due but no --picture given -- left on day 1 tags"
    if day == 3 and not bio:
        return f"day 3 due but no --bio given -- left on day 2 tags"
    if day == 4 and not media:
        return f"day 4 due but no --media given -- left on day 3 tags"
    if day > 4:
        return "already through all 4 days"

    profile, adb = _connect(phone_id)
    if profile is None:
        return "phone never became reachable"

    try:
        # Every day opens with the same 10-minute scroll -- InstagramWarmUpDay1Flow
        # is exactly that pass regardless of which day calls it; only what
        # runs *after* it differs.
        InstagramWarmUpDay1Flow().run(profile, adb_client=adb, logger=logger)

        if day == 2:
            profile.picture = picture
            InstagramUpdateProfilePictureU2Flow().run(profile, adb_client=adb, logger=logger)
        elif day == 3:
            profile.bio = bio
            InstagramUpdateBioU2Flow().run(profile, adb_client=adb, logger=logger)
        elif day == 4:
            # This account's actual first reel -- submit/verify, the same
            # duplicate-post-safe split the daily posting cycle uses, not the
            # legacy single-shot `run()`.
            profile.media_path = media
            flow = InstagramReelUploadU2Flow()
            outcome = flow.submit(profile, adb_client=adb, logger=logger, platform="mlx")
            if outcome["submitted"]:
                flow.verify_submitted(outcome["state"])

        tag_id = tag_client.tag_ids_by_name().get(DAY_TAGS[day].lower())
        tagged = bool(tag_id and tag_client.assign(phone_id, [tag_id]))
        return f"day {day} done -- {'tagged' if tagged else 'TAG FAILED'} {DAY_TAGS[day]!r}"
    finally:
        try:
            shutdown.shutdown_profiles([phone_id])
        except Exception as exc:
            logger.warning("mlx_warmup_days: shutdown failed for %s (%s)", phone_id, exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone_ids", nargs="+")
    parser.add_argument("--picture", default=None, help="local path, required once a profile reaches day 2")
    parser.add_argument("--bio", default=None, help="required once a profile reaches day 3")
    parser.add_argument("--media", default=None, help="reel video path, required once a profile reaches day 4")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)

    token = settings.get_saved_bearer_token()
    tag_client = MultiloginTagClient(token)
    shutdown = MultiloginShutdownClient(token)

    from adb_bot.clients.multilogin import MultiloginMobileListClient
    items = {str(i.get("id")): i for i in MultiloginMobileListClient(token).list_mobile_profiles()}

    jobs = []
    for phone_id in args.phone_ids:
        item = items.get(phone_id)
        if item is None:
            print(f"{phone_id}: not found in the workspace")
            continue
        day = current_day(item.get("tags") or [])
        jobs.append((phone_id, day))
        print(f"{item.get('serial_name')} ({phone_id}): due for day {day}")

    print(f"\nrunning {len(jobs)} phone(s), {args.workers} at a time", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for phone_id, day in jobs:
            logger = get_logger(f"warmup.{phone_id}")
            tags = items[phone_id].get("tags") or []
            futures[pool.submit(run_day, phone_id, day, tags, picture=args.picture,
                                bio=args.bio, media=args.media, tag_client=tag_client,
                                shutdown=shutdown, logger=logger)] = phone_id
        for index, future in enumerate(as_completed(futures), start=1):
            phone_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = f"ERROR: {exc}"
            print(f"[{index}/{len(jobs)}] {phone_id}: {result}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
