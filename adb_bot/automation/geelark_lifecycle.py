"""Tag-driven Geelark account lifecycle: Warmup -> Active_Posting.

Confirmed operating protocol, 2026-08-29:

* `Warmup` tag -- day-1 profiles. One 10-minute pass (feed scroll, stories,
  follow ~5 niche accounts) -- exactly what `InstagramWarmUpDay1Flow`
  already does, unmodified. On success the tag flips to `Active_Posting`;
  it never runs again for that profile.
* `Active_Posting` tag -- every day after. A short 30-45s scroll
  (`InstagramScrollFlow(scroll_seconds=...)`), then a post if the caller
  hands one in, then close. The proxy rotates automatically on the *next*
  profile's start (`session.start_session`'s own rotate-before-boot), not
  here -- there is deliberately no extra rotate call in this module.

No Airtable. Tags are the only state, read and written straight from
Geelark's own `/phone/list` and `/phone/detail/update` -- the same tag-merge
approach `signup_phone._apply_status_tags` already uses for signup outcomes,
generalized here since this needed it independently of that module.

Real-proxy assignment (not MultiLogin's relay -- see the 2026-08-26/27
incident in `session.py`'s module docstring for why that distinction
matters) is resolved from `GEELARK_PROXY_REBOOT_URLS`, the same environment
variable `ip_rotation.load_reboot_config` already treats as the fleet's
canonical list of real ports, rather than a second hardcoded list living
only here.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from adb_bot.automation.flows.instagram import InstagramScrollFlow, InstagramWarmUpDay1Flow
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadFlow
from adb_bot.automation.workflow import connect_with_retries
from adb_bot.clients.geelark.ip_rotation import load_reboot_config
from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.clients.geelark.session import GeelarkSession, start_session, stop_session
from adb_bot.clients.geelark.tags import GeelarkTagClient
from adb_bot.clients.geelark.transport import GeelarkTransport

TAG_WARMUP = "Warmup"
TAG_ACTIVE_POSTING = "Active_Posting"

# Active_Posting's pre-post scroll, per the confirmed protocol. Warm-up stays
# on InstagramWarmUpDay1Flow's own 600s default, untouched.
ACTIVE_POSTING_SCROLL_SECONDS = 40.0

_ROTATION_STATE_FILE = Path(
    os.environ.get("ADBBOT_STATE_DIR", "/root/.adb_bot")) / "geelark_real_proxy_rotation.json"


def _real_proxies(transport: GeelarkTransport) -> list[dict]:
    """The account's saved proxies that sit on one of our real, rotatable
    ports (from GEELARK_PROXY_REBOOT_URLS) -- not MultiLogin's relay.

    One entry per real port, first match wins if duplicates exist (the
    account has had a couple of accidental re-adds of the same port; that
    is account-book clutter to clean up separately, not something this
    function needs to care about).
    """
    from adb_bot.clients.geelark.proxies import GeelarkProxyClient

    real_ports = set(load_reboot_config().keys())
    if not real_ports:
        raise RuntimeError(
            "GEELARK_PROXY_REBOOT_URLS is not set; cannot resolve which "
            "proxies are our own real ports")
    seen_ports: set[int] = set()
    out: list[dict] = []
    for proxy in GeelarkProxyClient(transport).list_proxies():
        port = proxy.get("port")
        if port in real_ports and port not in seen_ports:
            seen_ports.add(port)
            out.append(proxy)
    return sorted(out, key=lambda p: p.get("port", 0))


def _next_real_proxy(transport: GeelarkTransport) -> dict:
    """Round-robins through the real proxies. Blind (does not check what's
    live) -- correct here because every caller in this module launches
    through session.start_session, which leases the port for real before
    rotating; a busy port blocks/waits rather than colliding."""
    proxies = _real_proxies(transport)
    if not proxies:
        raise RuntimeError("no real proxies found on the Geelark account")
    idx = 0
    try:
        idx = json.loads(_ROTATION_STATE_FILE.read_text()).get("idx", 0)
    except Exception:
        pass
    proxy = proxies[idx % len(proxies)]
    try:
        _ROTATION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ROTATION_STATE_FILE.write_text(json.dumps({"idx": idx + 1}))
    except OSError:
        pass
    return proxy


def phones_by_tag(tag_name: str, transport: GeelarkTransport | None = None) -> list[dict]:
    transport = transport or GeelarkTransport()
    return [row for row in GeelarkPhoneClient(transport).list_phones()
           if any(t.get("name") == tag_name for t in (row.get("tags") or []))]


def _retag(phone_id: str, transport: GeelarkTransport, *, remove: str, add: str,
          logger=None) -> None:
    """Swap one tag for another, merged with whatever else the phone already
    carries -- `tagIDs` on `/phone/detail/update` replaces rather than adds,
    so this always reads the current set first (same approach as
    `signup_phone._apply_status_tags`)."""
    phone_client = GeelarkPhoneClient(transport)
    tag_client = GeelarkTagClient(transport)

    if add not in tag_client.tag_ids_by_name():
        tag_client.ensure_tag(add)
    by_name = tag_client.tag_ids_by_name(refresh=True)

    existing_names: list[str] = []
    for row in phone_client.list_phones():
        if str(row.get("id")) == str(phone_id):
            existing_names = list(row.get("tags") or [])
            break

    keep = [n for n in existing_names if n != remove]
    if add not in keep:
        keep.append(add)
    ids = [by_name[n] for n in dict.fromkeys(keep) if n in by_name]
    phone_client.update_phone(phone_id, tag_ids=ids)
    if logger:
        logger.info("geelark_lifecycle: retagged %s: %s -> %s", phone_id, remove, add)


def _launch(phone_id: str, transport: GeelarkTransport, logger, adb_client
           ) -> tuple[GeelarkSession, str] | tuple[None, None]:
    """Force `phone_id` onto a real proxy and bring it up over ADB.
    Returns (session, target) or (None, None) if ADB never came up -- the
    session is still returned-stopped-by-caller-safe in that case (the
    phone itself booted; only the ADB handshake failed)."""
    proxy = _next_real_proxy(transport)
    GeelarkPhoneClient(transport).update_phone(phone_id, proxy_id=proxy["id"])
    session = start_session(phone_id, transport=transport, logger=logger,
                            owner=phone_id, wait_for_lease_seconds=120.0)
    target = connect_with_retries(adb_client, session.profile, logger,
                                  phone_id, max_attempts=5, retry_delay_seconds=5)
    return session, target


def run_warmup_cycle(phone_id: str, name: str, adb_client, transport=None,
                     logger=None) -> dict:
    """One Warmup pass. On a clean run, retags Warmup -> Active_Posting so
    this never fires again for the same profile."""
    transport = transport or GeelarkTransport()
    out = {"id": phone_id, "name": name, "at": time.time(), "cycle": "warmup"}
    session = None
    try:
        session, target = _launch(phone_id, transport, logger, adb_client)
        if not target:
            out["result"] = "could_not_reach_over_adb"
            return out
        result = InstagramWarmUpDay1Flow().run(session.profile, adb_client=adb_client,
                                               logger=logger)
        out["flow_result"] = result
        if not result.get("aborted"):
            _retag(phone_id, transport, remove=TAG_WARMUP, add=TAG_ACTIVE_POSTING,
                  logger=logger)
            out["result"] = "warmed_up"
        else:
            out["result"] = "aborted"
    except Exception as exc:
        out["result"] = "error"
        out["error"] = str(exc)
    finally:
        if session is not None:
            try:
                stop_session(session, logger=logger)
            except Exception as exc:
                if logger:
                    logger.warning("geelark_lifecycle: stop_session failed for %s (%s)",
                                   name, exc)
    return out


def run_active_posting_cycle(phone_id: str, name: str, adb_client, transport=None,
                             logger=None, media_path: str | None = None,
                             caption: str | None = None) -> dict:
    """One Active_Posting pass: short scroll, then a post if `media_path` is
    given. No tag change -- this tag is permanent once a profile reaches it."""
    transport = transport or GeelarkTransport()
    out = {"id": phone_id, "name": name, "at": time.time(), "cycle": "active_posting"}
    session = None
    try:
        session, target = _launch(phone_id, transport, logger, adb_client)
        if not target:
            out["result"] = "could_not_reach_over_adb"
            return out

        scroll_result = InstagramScrollFlow(scroll_seconds=ACTIVE_POSTING_SCROLL_SECONDS).run(
            session.profile, adb_client=adb_client, logger=logger)
        out["scroll_result"] = scroll_result

        if media_path:
            session.profile.media_path = media_path
            if caption is not None:
                session.profile.caption = caption
            post_result = InstagramReelUploadFlow().run(
                session.profile, adb_client=adb_client, logger=logger)
            out["post_result"] = post_result
            out["result"] = "posted" if post_result.get("success") else "post_failed"
        else:
            out["result"] = "scrolled_only"
    except Exception as exc:
        out["result"] = "error"
        out["error"] = str(exc)
    finally:
        if session is not None:
            try:
                stop_session(session, logger=logger)
            except Exception as exc:
                if logger:
                    logger.warning("geelark_lifecycle: stop_session failed for %s (%s)",
                                   name, exc)
    return out
