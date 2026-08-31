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
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from adb_bot.automation.flows.instagram import InstagramScrollFlow, InstagramWarmUpDay1Flow
# The u2 flow, not the legacy dump/OCR one: only this one carries the
# post_ledger duplicate-post guard and reports "uncertain" (share tapped,
# outcome unproven) as distinct from a conclusive failure. Switched 2026-08-30
# after finding the legacy flow has neither -- every non-success it returns
# would have looked like a safely-retryable "post_failed", including the one
# case a retry can actually double-post. u2 is already what the warm-up and
# scroll flows use on this fleet, so the dependency is already proven here.
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
from adb_bot.automation.flows.verification import (
    RESULT_BANNED,
    RESULT_IN_REVIEW,
    RESULT_SIGNED_OUT,
    RESULT_SOLVED,
    TAG_BANNED,
    TAG_HUMAN_VERIFICATION,
    TAG_IN_REVIEW,
    TAG_LOGGED_OUT,
    run_verification,
    simplified_status_tag,
)
from adb_bot.automation.flows.verification_driver import AdbChallengeDriver
from adb_bot.automation.verification_probe import _open_instagram
from adb_bot.clients.sms.router import build_router
from adb_bot.automation.workflow import connect_with_retries
from adb_bot.clients.geelark.ip_rotation import load_reboot_config
from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.clients.geelark.session import GeelarkSession, start_session, stop_session
from adb_bot.clients.geelark.tags import GeelarkTagClient
from adb_bot.clients.geelark.transport import GeelarkTransport

TAG_WARMUP = "Warmup"
TAG_ACTIVE_POSTING = "Active_Posting"

# Outcomes worth relaunching the same profile for within one call: these say
# nothing about the account itself, unlike an "aborted_*" challenge result --
# retrying past a captcha screen would just waste another launch.
# Confirmed real-world mix 2026-08-30: 47/155 lease-timeout errors (since
# fixed at the source) plus 16 could_not_reach_over_adb in one manual run,
# both of which previously just sat until someone noticed and reran by hand.
#
# "post_failed" (a conclusive negative -- error dialog, discard-draft prompt,
# stuck on the composer) is safe to retry: InstagramReelUploadU2Flow's own
# post_ledger blocks a second Share tap on the same clip once one attempt is
# unresolved, so a retry can never turn into a duplicate post. "post_uncertain"
# (Share was tapped, upload was still going per the notification "progress
# bar" when the wait ran out) is deliberately NOT in this set -- see the
# comment at its call site.
_RETRYABLE_RESULTS = {"error", "could_not_reach_over_adb", "post_failed"}
_DEFAULT_MAX_CYCLE_ATTEMPTS = 3

# Active_Posting's pre-post scroll, per the confirmed protocol. Warm-up stays
# on InstagramWarmUpDay1Flow's own 600s default, untouched.
ACTIVE_POSTING_SCROLL_SECONDS = 40.0

_ROTATION_STATE_FILE = Path(
    os.environ.get("ADBBOT_STATE_DIR", "/root/.adb_bot")) / "geelark_real_proxy_rotation.json"


def _real_proxies(transport: GeelarkTransport) -> list[dict]:
    """The account's saved proxies that sit on one of our real, rotatable
    ports (from GEELARK_PROXY_REBOOT_URLS) -- not MultiLogin's relay.

    One entry per real port -- the account has had a couple of accidental
    re-adds of the same port (serialNo 11/12 duplicating 6/5, confirmed
    2026-08-30). Picks the LOWEST serialNo per port deterministically,
    rather than whichever the API happens to list first: `list_proxies()`'s
    order isn't documented as stable, and without pinning this, different
    calls silently split real usage across both the original and the
    duplicate row for the same physical modem -- confirmed live 2026-08-30
    (serialNo 11/12 alone showed 20-21 profiles' worth of use next to 37-57
    on 5-8), which is also what makes the duplicates look "still in use"
    and blocks deleting them.
    """
    from adb_bot.clients.geelark.proxies import GeelarkProxyClient

    real_ports = set(load_reboot_config().keys())
    if not real_ports:
        raise RuntimeError(
            "GEELARK_PROXY_REBOOT_URLS is not set; cannot resolve which "
            "proxies are our own real ports")
    by_port: dict[int, dict] = {}
    for proxy in GeelarkProxyClient(transport).list_proxies():
        port = proxy.get("port")
        if port not in real_ports:
            continue
        current = by_port.get(port)
        if current is None or (proxy.get("serialNo") or 0) < (current.get("serialNo") or 0):
            by_port[port] = proxy
    return sorted(by_port.values(), key=lambda p: p.get("port", 0))


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
            # A phone's own tags are {"name": ...} dicts, never bare
            # strings (confirmed live 2026-08-30 -- 51 of 53 profiles in
            # the first real night run crashed here with "unhashable type:
            # 'dict'" from treating them as strings further down).
            existing_names = [str(t.get("name")) for t in (row.get("tags") or [])
                             if t.get("name")]
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
    # A full warm-up/posting cycle runs 12-15 min; 120s was too short under
    # the 4-worker queue and caused 47/155 spurious "already leased" errors
    # in the 2026-08-30 manual run even though every port was legitimately
    # busy, not stuck. 1200s comfortably covers one sibling cycle finishing.
    session = start_session(phone_id, transport=transport, logger=logger,
                            owner=phone_id, wait_for_lease_seconds=1200.0)
    target = connect_with_retries(adb_client, session.profile, logger,
                                  phone_id, max_attempts=5, retry_delay_seconds=5)
    return session, target


def _check_for_challenge_and_abort(target: str, adb_client, phone_id: str, transport,
                                   name: str, current_tag: str, logger=None) -> str | None:
    """Read the current screen before doing any real work; if it's one of the
    3 non-healthy simplified states (human verification / in review / logged
    out / banned), retag away from `current_tag` and return the new tag --
    the caller must abort rather than scroll or post through a challenge
    screen. Returns None (safe to proceed) for a healthy feed or anything
    `simplified_status_tag` doesn't recognise.

    Confirmed launch-readiness gap 2026-08-30: a captcha/SMS screen mid-cycle
    previously did nothing but let the flow run anyway.
    """
    driver = AdbChallengeDriver(target, adb_client, logger=logger, act=False)
    text = driver.read_screen()
    tag = simplified_status_tag(text)
    if tag is None:
        return None
    _retag(phone_id, transport, remove=current_tag, add=tag, logger=logger)
    if logger:
        logger.warning("geelark_lifecycle: %s hit %r mid-cycle; aborting and retagging",
                       name, tag)
    return tag


def run_warmup_cycle(phone_id: str, name: str, adb_client, transport=None,
                     logger=None, max_attempts: int = _DEFAULT_MAX_CYCLE_ATTEMPTS
                     ) -> dict:
    """One Warmup pass, relaunched up to `max_attempts` times if it hits an
    infrastructure hiccup (`_RETRYABLE_RESULTS`) rather than a real challenge
    or a clean success. On a clean run, retags Warmup -> Active_Posting so
    this never fires again for the same profile. Aborts and retags instead
    if a challenge screen is already showing before warm-up even starts --
    that outcome is never retried; another launch won't clear a captcha."""
    transport = transport or GeelarkTransport()
    out = {}
    for attempt in range(1, max_attempts + 1):
        out = _run_warmup_cycle_once(phone_id, name, adb_client, transport, logger)
        out["attempt"] = attempt
        if out["result"] not in _RETRYABLE_RESULTS or attempt == max_attempts:
            return out
        if logger:
            logger.warning(
                "geelark_lifecycle: warmup cycle for %s got %r (attempt %s/%s); retrying",
                name, out["result"], attempt, max_attempts)
    return out


def _run_warmup_cycle_once(phone_id: str, name: str, adb_client, transport,
                           logger) -> dict:
    out = {"id": phone_id, "name": name, "at": time.time(), "cycle": "warmup"}
    session = None
    try:
        session, target = _launch(phone_id, transport, logger, adb_client)
        if not target:
            out["result"] = "could_not_reach_over_adb"
            return out

        blocked = _check_for_challenge_and_abort(target, adb_client, phone_id,
                                                 transport, name, TAG_WARMUP, logger)
        if blocked:
            out["result"] = f"aborted_{blocked.replace(' ', '_')}"
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
        if logger:
            logger.exception("geelark_lifecycle: %s cycle raised for %s (%s)",
                            out.get("cycle"), name, exc)
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
                             logger=None, media_paths: list[str] | None = None,
                             caption: str | None = None,
                             max_attempts: int = _DEFAULT_MAX_CYCLE_ATTEMPTS) -> dict:
    """One Active_Posting pass, relaunched up to `max_attempts` times on an
    infrastructure hiccup or a conclusive posting failure (`_RETRYABLE_RESULTS`)
    -- same reasoning as `run_warmup_cycle`. `post_uncertain` (Share was
    tapped but the outcome couldn't be proven either way -- e.g. the upload
    was still showing its in-progress notification when the wait gave up) is
    deliberately NOT retried: the post_ledger blocks another Share attempt on
    that clip regardless, and retrying can't resolve the uncertainty any
    faster than just waiting could.

    `media_paths` is a list, not a single path (changed 2026-08-31): the
    launch itself (GeeLark cold boot + IP rotation + ADB connect) is a fixed
    ~90s cost paid once per launch, so posting N clips in one already-open
    session instead of N separate launches saves (N-1) x that cost. Each
    entry gets its own scroll immediately before its post -- scrolling is
    still per-post, only the launch is shared. A retry relaunches the whole
    batch; anything already posted is safe because the post_ledger refuses a
    second Share on the same clip regardless of how it gets asked again.
    With no content for the day (`media_paths` empty or None) this cycle
    does neither scroll nor post and reports "no_content" (changed
    2026-08-31, was scroll-always). The tag is otherwise permanent once a
    profile reaches it -- the one exception is a challenge screen showing up
    before this cycle even starts, which pulls the profile straight out of
    Active_Posting instead of scrolling/posting through it."""
    transport = transport or GeelarkTransport()
    out = {}
    for attempt in range(1, max_attempts + 1):
        out = _run_active_posting_cycle_once(phone_id, name, adb_client, transport,
                                             logger, media_paths, caption)
        out["attempt"] = attempt
        if out["result"] not in _RETRYABLE_RESULTS or attempt == max_attempts:
            return out
        if logger:
            logger.warning(
                "geelark_lifecycle: active_posting cycle for %s got %r (attempt %s/%s); retrying",
                name, out["result"], attempt, max_attempts)
    return out


def _run_active_posting_cycle_once(phone_id: str, name: str, adb_client, transport,
                                   logger, media_paths: list[str] | None,
                                   caption: str | None) -> dict:
    media_paths = list(media_paths or [])
    out = {"id": phone_id, "name": name, "at": time.time(), "cycle": "active_posting",
          "posts": []}
    session = None
    try:
        session, target = _launch(phone_id, transport, logger, adb_client)
        if not target:
            out["result"] = "could_not_reach_over_adb"
            return out

        blocked = _check_for_challenge_and_abort(target, adb_client, phone_id,
                                                 transport, name, TAG_ACTIVE_POSTING, logger)
        if blocked:
            out["result"] = f"aborted_{blocked.replace(' ', '_')}"
            return out

        if not media_paths:
            out["result"] = "no_content"
            return out

        # Scroll only runs ahead of an actual post now (changed 2026-08-31,
        # was unconditional) -- with several posts/day/profile targeted and
        # only 4 real proxy ports, a fixed 40s scroll on every cycle --
        # including the ones with nothing to post -- was most of the day's
        # posting budget spent on nothing.
        worst = "posted"   # priority for the retry decision: post_failed > post_uncertain > posted
        for media_path in media_paths:
            scroll_result = InstagramScrollFlow(scroll_seconds=ACTIVE_POSTING_SCROLL_SECONDS).run(
                session.profile, adb_client=adb_client, logger=logger)

            session.profile.media_path = media_path
            if caption is not None:
                session.profile.caption = caption
            post_result = InstagramReelUploadU2Flow().run(
                session.profile, adb_client=adb_client, logger=logger)
            if post_result.get("success"):
                post_outcome = "posted"
            elif post_result.get("uncertain"):
                # Share was tapped but nothing proved it landed (or failed) --
                # a real duplicate-post risk. The flow's own post_ledger
                # already blocks a second Share tap for this exact clip, but
                # a retry still can't resolve the uncertainty any faster than
                # waiting can, so this is deliberately not retried.
                post_outcome = "post_uncertain"
            else:
                post_outcome = "post_failed"
            out["posts"].append({"media_path": media_path, "result": post_outcome,
                                "scroll_result": scroll_result, "post_result": post_result})
            if post_outcome == "post_failed":
                worst = "post_failed"
            elif post_outcome == "post_uncertain" and worst != "post_failed":
                worst = "post_uncertain"

        out["result"] = worst
    except Exception as exc:
        out["result"] = "error"
        out["error"] = str(exc)
        if logger:
            logger.exception("geelark_lifecycle: %s cycle raised for %s (%s)",
                            out.get("cycle"), name, exc)
    finally:
        if session is not None:
            try:
                stop_session(session, logger=logger)
            except Exception as exc:
                if logger:
                    logger.warning("geelark_lifecycle: stop_session failed for %s (%s)",
                                   name, exc)
    return out


def run_in_review_recheck_cycle(phone_id: str, name: str, adb_client, transport=None,
                                logger=None, resolved_tag: str = TAG_ACTIVE_POSTING
                                ) -> dict:
    """One daily look at an `in review`-tagged profile: read the screen,
    nothing else -- no challenge-solving, no scroll, no post -- then close
    the app and retag by what's actually showing. Confirmed protocol
    2026-08-30:

    * a healthy feed -> Instagram cleared the review -> retag to
      `resolved_tag` (defaults to Active_Posting: a profile that reached
      `in review` already got through Warmup once, so it resumes posting
      rather than warming up again)
    * still a review screen -> tag stays exactly where it is
    * logged out or banned -> retag to match, same as any other cycle would

    Anything else `simplified_status_tag` doesn't recognise (a captcha, a
    code screen, ...) also leaves the tag untouched: this cycle only acts on
    the 3 outcomes the user specified, not a good moment to invent a fourth.
    """
    transport = transport or GeelarkTransport()
    out = {"id": phone_id, "name": name, "at": time.time(), "cycle": "in_review_recheck"}
    session = None
    try:
        session, target = _launch(phone_id, transport, logger, adb_client)
        if not target:
            out["result"] = "could_not_reach_over_adb"
            return out

        _open_instagram(target, adb_client, logger)
        driver = AdbChallengeDriver(target, adb_client, logger=logger, act=False)
        text = driver.read_screen()
        tag = simplified_status_tag(text)
        out["screen_tag"] = tag

        if tag is None:
            _retag(phone_id, transport, remove=TAG_IN_REVIEW, add=resolved_tag,
                  logger=logger)
            out["result"] = "cleared"
        elif tag == TAG_IN_REVIEW:
            out["result"] = "still_in_review"
        elif tag in (TAG_LOGGED_OUT, TAG_BANNED):
            _retag(phone_id, transport, remove=TAG_IN_REVIEW, add=tag, logger=logger)
            out["result"] = tag.replace(" ", "_")
        else:
            # human_verification or anything else unrecognised: leave it be,
            # not one of the 3 outcomes this cycle is meant to act on.
            out["result"] = "unchanged"

        adb_client.run_command(f"adb -s {target} shell am force-stop com.instagram.android")
    except Exception as exc:
        out["result"] = "error"
        out["error"] = str(exc)
        if logger:
            logger.exception("geelark_lifecycle: %s cycle raised for %s (%s)",
                            out.get("cycle"), name, exc)
    finally:
        if session is not None:
            try:
                stop_session(session, logger=logger)
            except Exception as exc:
                if logger:
                    logger.warning("geelark_lifecycle: stop_session failed for %s (%s)",
                                   name, exc)
    return out


# thispersondoesnotexist.com serves an HTML page to a bare GET (a naive
# request saves a 4.5KB text/html file that looks like a broken image); a
# browser User-Agent plus a same-site Referer is what actually gets the
# JPEG. Confirmed 2026-08-28 across 11 real profiles, Meta APPROVED the
# result -- see the "geelark-selfie-verification-ai-face" memory.
_AI_FACE_URL = "https://thispersondoesnotexist.com/random-person.jpeg"
_AI_FACE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Referer": "https://thispersondoesnotexist.com/",
}


def _fetch_ai_face(out_dir: Path, logger=None) -> str | None:
    """A fresh (never reused) AI-generated face, downscaled and ready for
    `AdbChallengeDriver.upload_photo`. `None` on any failure -- a photo
    challenge that can't get a face is a legitimate `needs_human` outcome,
    never an exception a caller has to catch.

    The downscale is not cosmetic: a straight-from-the-source ~508KB JPEG
    left Instagram's Submit button spinning 4+ minutes and never completed
    (confirmed 2026-08-28); the same face scaled to 720px width / ~38KB
    submitted in seconds. Every call fetches a new face -- reusing one
    across accounts is an obvious link between them.
    """
    try:
        response = requests.get(_AI_FACE_URL, headers=_AI_FACE_HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as exc:
        if logger:
            logger.warning("geelark_lifecycle: AI face fetch failed (%s)", exc)
        return None

    raw_path = out_dir / "face_raw.jpg"
    raw_path.write_bytes(response.content)
    small_path = out_dir / "face.jpg"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path), "-vf", "scale=720:-1", "-q:v", "4",
             str(small_path)],
            capture_output=True, timeout=30)
    except Exception as exc:
        if logger:
            logger.warning("geelark_lifecycle: AI face downscale failed (%s)", exc)
        return None
    if result.returncode != 0 or not small_path.exists():
        if logger:
            logger.warning("geelark_lifecycle: ffmpeg downscale failed (%s)",
                           result.stderr.decode(errors="replace")[-300:])
        return None
    return str(small_path)


def run_human_verification_cycle(phone_id: str, name: str, adb_client, transport=None,
                                 logger=None) -> dict:
    """One real attempt at clearing a `human verification`-tagged profile:
    launches the phone, drives whatever challenge chain Instagram shows
    (phone number, SMS code, image captcha, photo) via `run_verification`,
    and retags on a conclusive outcome.

    Reuses the MLX verification stack essentially as-is -- `run_verification`
    only needs a `ChallengeDriver` (ADB-generic; `AdbChallengeDriver` already
    is one) and an `SmsRouter` (provider-agnostic), neither of which has any
    MultiLogin- or Airtable-specific plumbing baked in. Money is spent here
    (SMS numbers, possibly a captcha solve), so unlike the other cycles this
    one is deliberately NOT wrapped in the generic launch-retry -- a blind
    relaunch-and-retry on an infrastructure hiccup could rent a second set of
    numbers for a run that never needed the first set refunded. If the phone
    never comes up over ADB, no number is ever rented.

    * `solved` -> Active_Posting
    * `banned` -> banned (matches _check_for_challenge_and_abort's tag)
    * `signed_out` -> logged out (matches _check_for_challenge_and_abort's tag)
    * `needs_human` / `stuck` / `failed` -> tag stays exactly where it is;
      this profile needs a person, or another attempt later
    """
    transport = transport or GeelarkTransport()
    out = {"id": phone_id, "name": name, "at": time.time(), "cycle": "human_verification"}
    session = None
    face_dir = None
    try:
        session, target = _launch(phone_id, transport, logger, adb_client)
        if not target:
            out["result"] = "could_not_reach_over_adb"
            return out

        face_dir = Path(tempfile.mkdtemp(prefix="geelark-ai-face-"))
        photo_source_path = _fetch_ai_face(face_dir, logger=logger) or ""

        driver = AdbChallengeDriver(target, adb_client, logger=logger,
                                    photo_source_path=photo_source_path)
        router = build_router(logger=logger)
        result = run_verification(driver, router, logger=logger)
        out["status"] = result.status
        out["detail"] = result.detail
        out["numbers_used"] = result.numbers_used

        if result.status == RESULT_SOLVED:
            _retag(phone_id, transport, remove=TAG_HUMAN_VERIFICATION,
                  add=TAG_ACTIVE_POSTING, logger=logger)
            out["result"] = "solved"
        elif result.status == RESULT_BANNED:
            _retag(phone_id, transport, remove=TAG_HUMAN_VERIFICATION,
                  add=TAG_BANNED, logger=logger)
            out["result"] = "banned"
        elif result.status == RESULT_SIGNED_OUT:
            _retag(phone_id, transport, remove=TAG_HUMAN_VERIFICATION,
                  add=TAG_LOGGED_OUT, logger=logger)
            out["result"] = "signed_out"
        elif result.status == RESULT_IN_REVIEW:
            # An appeal is already submitted and waiting on Meta's own
            # review clock -- not human verification (nothing for a person
            # to do either) and not resolved yet. The existing daily
            # in-review recheck (run_in_review_recheck_cycle) already knows
            # how to watch this tag and move it to Active_Posting once
            # Instagram clears it on its own.
            _retag(phone_id, transport, remove=TAG_HUMAN_VERIFICATION,
                  add=TAG_IN_REVIEW, logger=logger)
            out["result"] = "in_review"
        else:
            out["result"] = result.status
    except Exception as exc:
        out["result"] = "error"
        out["error"] = str(exc)
        if logger:
            logger.exception("geelark_lifecycle: %s cycle raised for %s (%s)",
                            out.get("cycle"), name, exc)
    finally:
        if face_dir is not None:
            import shutil
            shutil.rmtree(face_dir, ignore_errors=True)
        if session is not None:
            try:
                stop_session(session, logger=logger)
            except Exception as exc:
                if logger:
                    logger.warning("geelark_lifecycle: stop_session failed for %s (%s)",
                                   name, exc)
    return out
