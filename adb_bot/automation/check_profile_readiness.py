"""Tag Geelark phones `Post Ready` via Geelark's own `instagramEdit` RPA task.

    python -m adb_bot.automation.check_profile_readiness --list
    python -m adb_bot.automation.check_profile_readiness --limit 5 --apply

Walks phones tagged `IG connected` that are not yet `Post Ready`, triggers
Geelark's own `instagramEdit` task with a randomized bio, the fixed
Website link, and the model's profile-picture URL, then polls the task
until it finishes.

**`Post Ready` means all three, always.** A phone is skipped entirely --
never sent a two-of-three request -- when either `LINK_URL` is unset or
the model has no picture URL configured yet in `model_media`, and the tag
is only applied once Geelark itself reports the task `Completed`. Calling
a profile ready off a partial request would say more than is true.

Dry run by default. `--apply` is what triggers real Geelark tasks.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from adb_bot.automation import bio_variations, model_media
from adb_bot.clients.geelark import GeelarkTransport, rpa
from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.clients.geelark.tags import GeelarkTagClient
from adb_bot.core.logger import get_logger

CONNECTED_TAG = "IG connected"
POST_READY_TAG = "Post Ready"

# The one destination every profile's bio link points at -- one link for
# everyone, not per model ("always feed our standard link URL", 2026-08-22).
# Left blank on purpose: a real, wrong-for-nobody-in-particular URL has to
# be supplied here before `--apply` will touch any phone.
LINK_URL = ""
LINK_TITLE = ""


def phones_to_check(transport=None) -> list[dict]:
    """`IG connected` phones not already `Post Ready`, in folder order."""
    phones = GeelarkPhoneClient(transport).list_phones()
    out = []
    for phone in phones:
        names = {str((t or {}).get("name")) for t in (phone.get("tags") or [])}
        if CONNECTED_TAG in names and POST_READY_TAG not in names:
            out.append(phone)
    return sorted(out, key=lambda p: (
        str((p.get("group") or {}).get("name") or ""),
        str(p.get("serialName") or "")))


def mark_post_ready(phone: dict, transport, logger) -> None:
    """Add `Post Ready`, keeping every tag the phone already carries."""
    tags = GeelarkTagClient(transport)
    wanted = {str(t.get("name")) for t in (phone.get("tags") or [])
             if t.get("name")}
    wanted.add(POST_READY_TAG)
    tags.ensure_tag(POST_READY_TAG, "green")
    by_name = tags.tag_ids_by_name(refresh=True)

    tag_ids = [by_name[name] for name in wanted if name in by_name]
    if wanted and not tag_ids:
        logger.warning("check_profile_readiness: could not resolve tags %s "
                       "for %s; leaving them as they are",
                       wanted, phone.get("serialName"))
        return
    GeelarkPhoneClient(transport).update_phone(str(phone["id"]),
                                               tag_ids=tag_ids)


def run_one(phone: dict, args, logger, transport) -> dict:
    profile_id = str(phone["id"])
    name = str(phone.get("serialName") or profile_id)
    model = str((phone.get("group") or {}).get("name") or "")
    out = {"profile": name, "id": profile_id, "model": model}

    picture_url = model_media.picture_url_for(model)
    if not picture_url:
        out["status"] = "no-picture-configured"
        print(f"  {name:18} skipped -- no profile picture URL configured "
              f"for {model!r} yet")
        return out
    if not LINK_URL:
        out["status"] = "no-link-configured"
        print(f"  {name:18} skipped -- LINK_URL is not set")
        return out

    bio = bio_variations.build_bio()
    if not args.apply:
        out["status"] = "dry-run"
        print(f"  {name:18} DRY RUN -- bio={bio!r} link={LINK_URL!r} "
              f"picture={picture_url!r}")
        return out

    task_id = rpa.trigger_instagram_edit_profile(
        profile_id, biography=bio, link_url=LINK_URL, link_title=LINK_TITLE,
        profile_picture=picture_url, transport=transport)
    if not task_id:
        out["status"] = "no-task-id"
        print(f"  {name:18} instagramEdit did not return a task id")
        return out
    out["task_id"] = task_id

    detail = rpa.wait_for_task(task_id, transport=transport,
                               timeout_seconds=args.task_timeout)
    status = detail.get("status")
    if status == rpa.STATUS_COMPLETED:
        mark_post_ready(phone, transport, logger)
        out["status"] = "post-ready"
        print(f"  {name:18} Post Ready (task {task_id})")
    elif status == rpa.STATUS_FAILED:
        out["status"] = "failed"
        print(f"  {name:18} failed: {detail.get('failDesc')}")
    else:
        out["status"] = f"unfinished-{status}"
        print(f"  {name:18} still not finished after {args.task_timeout}s "
              f"(status={status})")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Tag Geelark phones Post Ready via instagramEdit.")
    parser.add_argument("--list", action="store_true",
                        help="show the phones that would be checked")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--task-timeout", type=int, default=300,
                        help="seconds to wait for one instagramEdit task")
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    transport = GeelarkTransport()

    phones = phones_to_check(transport)[:max(0, args.limit)]
    print(f"phones to check: {len(phones)}")
    for phone in phones:
        print(f"  {str(phone.get('serialName')):18} "
              f"{str((phone.get('group') or {}).get('name'))}")
    if args.list or not phones:
        return 0
    if not args.apply:
        print("\nDRY RUN -- pass --apply to trigger real Geelark tasks")

    results: list[dict] = []

    def worker(phone):
        try:
            results.append(run_one(phone, args, logger, transport))
        except Exception as exc:
            logger.warning("check_profile_readiness: %s blew up (%s)",
                           phone.get("serialName"), exc)
            results.append({"profile": phone.get("serialName"),
                            "status": "error", "detail": str(exc)[:300]})

    for start in range(0, len(phones), max(1, args.concurrency)):
        batch = phones[start:start + max(1, args.concurrency)]
        threads = [threading.Thread(target=worker, args=(p,), daemon=True)
                   for p in batch]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(args.task_timeout + 60)
        time.sleep(2)

    print(f"\nchecked {len(results)} phone(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
