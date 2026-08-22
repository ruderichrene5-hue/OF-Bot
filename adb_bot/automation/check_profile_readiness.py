"""Tag Geelark phones with what their Instagram profile still needs.

    python -m adb_bot.automation.check_profile_readiness --list
    python -m adb_bot.automation.check_profile_readiness --limit 5 --apply

Walks phones tagged `IG connected` that are not yet `Post Ready`, opens
Instagram's Edit Profile screen, and tags `Bio Done` / `Link Done` based on
what is actually there right now -- not what somebody asked for, since a
bio update can fail silently and a checkbox does not know that.

**`Post Ready` needs a third thing this cannot check yet.** It is only
meant to fire once Bio, Link AND Profile Picture are all confirmed --
picture detection is a separate, harder piece (no existing code reads a
profile picture's state; it is an image, not text) that has not landed.
Until it does, `Post Ready` is never set here, on purpose, rather than
claiming a profile is ready to post when a third of the check was skipped.

Dry run by default. `--apply` is what launches phones and writes tags.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from adb_bot.automation.flows.instagram import InstagramProfileReadinessFlow
from adb_bot.automation.signup_phone import GeelarkHost
from adb_bot.automation.workflow import connect_with_retries
from adb_bot.clients.adb import ADBClient
from adb_bot.clients.geelark import GeelarkTransport
from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.clients.geelark.tags import GeelarkTagClient
from adb_bot.core.logger import get_logger

CONNECTED_TAG = "IG connected"
BIO_TAG = "Bio Done"
LINK_TAG = "Link Done"
POST_READY_TAG = "Post Ready"


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


def apply_tags(phone: dict, readiness, transport, logger) -> None:
    """Write `Bio Done`/`Link Done` onto the phone, replacing nothing else.

    Additive, unlike `signup_geelark.write_back`'s terminal-tag swap: a
    profile can be `IG connected` AND `Bio Done` AND `Link Done` all at
    once, so existing tags are kept, not replaced.
    """
    tags = GeelarkTagClient(transport)
    by_name = tags.tag_ids_by_name()
    wanted = {str(t.get("name")) for t in (phone.get("tags") or [])
             if t.get("name")}

    if readiness.bio:
        wanted.add(BIO_TAG)
        tags.ensure_tag(BIO_TAG, "green")
    if readiness.link:
        wanted.add(LINK_TAG)
        tags.ensure_tag(LINK_TAG, "green")
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
    out = {"profile": name, "id": profile_id, "status": "dry-run"}
    if not args.apply:
        print(f"  {name:18} DRY RUN")
        return out

    host = GeelarkHost(transport, args)
    adb_client = ADBClient()
    try:
        profile = host.launch(profile_id, logger)
        if not profile:
            out["status"] = "not-ready"
            return out
        target = connect_with_retries(adb_client, profile, logger,
                                      profile_id, max_attempts=8,
                                      retry_delay_seconds=5)
        if not target:
            out["status"] = "unreachable"
            return out

        readiness = InstagramProfileReadinessFlow().check(
            target, adb_client, logger=logger)
        if readiness.blocked:
            out["status"] = f"blocked-{readiness.blocked}"
            print(f"  {name:18} blocked ({readiness.blocked})")
            return out

        apply_tags(phone, readiness, transport, logger)
        out["status"] = "checked"
        out["bio"], out["link"] = readiness.bio, readiness.link
        print(f"  {name:18} bio={'yes' if readiness.bio else 'no':<3} "
              f"link={'yes' if readiness.link else 'no'}")
    finally:
        host.shutdown(profile_id, logger)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Tag Geelark phones with Bio/Link readiness.")
    parser.add_argument("--list", action="store_true",
                        help="show the phones that would be checked")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--readiness-attempts", type=int, default=10)
    parser.add_argument("--readiness-wait", type=int, default=15)
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
        print("\nDRY RUN -- pass --apply to launch phones and write tags")
        return 0

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
            thread.join(15 * 60)
        time.sleep(5)

    print(f"\nchecked {len(results)} phone(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
