"""Drive one photo post on one profile, by hand.

The photo-post flow is reached three ways in production -- the UI's flow
dropdown, a Warmup Plan row with a `Feed Posts` count, and the Posting Queue --
and none of those is a good way to try a change to it. This is the fourth: name
a profile, name a picture, watch it happen, get an exit code.

    python -m adb_bot.automation.photo_post_cli --profile Rodrigo --photo pic.jpg

It launches the profile, waits for MultiLogin to hand over ADB, runs the flow,
and closes the phone again on every exit path (including Ctrl-C) because
`run_profile_workflow` is the shutdown-guarded wrapper -- a phone left open
costs ~215 MB and holds a slot against ADBBOT_MAX_LIVE_PROFILES.

`--inspect` does everything except the posting: it launches, reports what
Instagram is showing and what is already in the gallery, and closes. Run it
first on an unfamiliar profile. Nothing it does can post.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from adb_bot.automation.bootstrap import build_automation, build_mlx_clients
from adb_bot.automation.workflow import run_profile_workflow
from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient
from adb_bot.config import settings
from adb_bot.core.logger import get_logger

import os

FLOW_NAME = "instagram_photo_post_u2"


def _token(cli_value=None) -> str:
    return ((cli_value or "").strip()
            or (os.environ.get("MULTILOGIN_TOKEN", "") or "").strip()
            or settings.get_saved_bearer_token())


def resolve_profile_id(token: str, wanted: str) -> tuple[str, str]:
    """Turn a profile name into its MLX id. An id passed straight through is
    still looked up, so a typo fails here rather than as a mystery launch error.

    Returns (profile_id, display_name). Raises SystemExit with the near-misses
    listed when the name does not resolve -- MLX names are hand-typed and
    "Rodrigo " with a trailing space is a real thing that happens.

    NOTE: the name lives in `serial_name`, not `name`. The list endpoint returns
    a `name` key too and it is null for every mobile profile in this workspace.
    """
    items = MultiloginMobileListClient(token).list_mobile_profiles()
    wanted_clean = wanted.strip().lower()

    for item in items:
        if str(item.get("id") or "") == wanted.strip():
            return str(item["id"]), str(item.get("serial_name") or item["id"])
    exact = [i for i in items if (i.get("serial_name") or "").strip().lower() == wanted_clean]
    if len(exact) == 1:
        return str(exact[0]["id"]), str(exact[0]["serial_name"])
    if len(exact) > 1:
        ids = ", ".join(str(i["id"]) for i in exact)
        raise SystemExit(f"[fatal] {len(exact)} profiles are called {wanted!r} ({ids}). "
                         f"Pass the id instead.")

    near = sorted({(i.get("serial_name") or "") for i in items
                   if wanted_clean in (i.get("serial_name") or "").lower()})
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise SystemExit(f"[fatal] No MLX profile named {wanted!r}.{hint}")


def _adb(target: str, *args: str, timeout: int = 30) -> str:
    result = subprocess.run(["adb", "-s", target, *args],
                            capture_output=True, text=True, timeout=timeout, check=False)
    return (result.stdout or result.stderr or "").strip()


def inspect_phone(target: str, log) -> None:
    """Read-only: what is Instagram showing, and what is in the gallery?

    Everything here is a query. Nothing taps, types, or posts.
    """
    log.info("--- inspect %s ---", target)
    # `dumpsys activity activities` does not carry mResumedActivity on every
    # build; `dumpsys window` answers on all of them, so try the cheap and
    # reliable one first and only fall back to parsing the big dump.
    focus = _adb(target, "shell", "dumpsys", "window", "displays")
    focused = [line.strip() for line in focus.splitlines()
               if "mCurrentFocus" in line or "mFocusedApp" in line]
    if not focused:
        activity = _adb(target, "shell", "dumpsys", "activity", "activities")
        focused = [line.strip() for line in activity.splitlines()
                   if "mResumedActivity" in line or "mFocusedActivity" in line]
    log.info("foreground: %s", focused[0] if focused else "unknown")
    log.info("instagram installed: %s",
             "yes" if "com.instagram.android" in
             _adb(target, "shell", "pm", "list", "packages", "com.instagram.android") else "NO")

    for directory in ("/sdcard/Pictures", "/sdcard/DCIM/Camera", "/sdcard/Download"):
        listing = _adb(target, "shell", "ls", "-1", directory)
        if "No such file" in listing or not listing:
            log.info("%s: empty or absent", directory)
            continue
        names = [n for n in listing.splitlines() if n.strip()]
        log.info("%s: %s file(s) -- %s", directory, len(names), ", ".join(names[:8]))

    indexed = _adb(target, "shell", "content", "query",
                   "--uri", "content://media/external/images/media",
                   "--projection", "_display_name")
    # "No result found." is an empty answer, not a row. Counting it as one is
    # how an empty gallery reads as "there is already a picture on here".
    lines = [line.strip() for line in indexed.splitlines()
             if line.strip() and "no result found" not in line.lower()]
    log.info("images in MediaStore: %s", len(lines))
    for line in lines[:8]:
        log.info("   %s", line)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Post one photo to Instagram from one MultiLogin profile.")
    parser.add_argument("--profile", required=True,
                        help="MLX profile name (its serial_name, e.g. 'Rodrigo') or id.")
    parser.add_argument("--photo",
                        help="Image file, or a folder to take the next unused image from. "
                             "Required unless --inspect.")
    parser.add_argument("--caption", default=None,
                        help="Optional caption. Omitted means an uncaptioned post, which is "
                             "what every post the fleet has ever made has been.")
    parser.add_argument("--handle", default=None,
                        help="Bare Instagram handle this post is for, on a phone carrying two "
                             "accounts. Omit on single-account phones.")
    parser.add_argument("--inspect", action="store_true",
                        help="Launch, report the phone's state, close. Never posts.")
    parser.add_argument("--keep-open", action="store_true",
                        help="Leave the phone running afterwards (for a look at the screen). "
                             "It still counts against the live-profile ceiling.")
    parser.add_argument("--launch-wait", type=int, default=40,
                        help="Seconds to wait after launch before checking ADB readiness.")
    parser.add_argument("--token", default=None, help="MultiLogin token override.")
    args = parser.parse_args(argv)

    if not args.inspect and not args.photo:
        parser.error("--photo is required unless --inspect is given")

    log = get_logger("photo_post_cli", log_file="logs/photo_post_cli.log")

    token = _token(args.token)
    if not token:
        raise SystemExit("[fatal] No MultiLogin token (MULTILOGIN_TOKEN / dev settings / --token).")

    photo = None
    if args.photo:
        photo = Path(args.photo).expanduser().resolve()
        if not photo.exists():
            raise SystemExit(f"[fatal] No such photo: {photo}")

    profile_id, display = resolve_profile_id(token, args.profile)
    log.info("Profile %r resolves to MLX id %s", display, profile_id)

    clients = build_mlx_clients(token)
    automation = build_automation()

    log.info("Launching %s (%s) on MultiLogin", display, profile_id)
    response = clients.launcher.start_profiles([profile_id])
    if isinstance(response, dict) and response.get("status") == "error":
        log.error("Launch failed for %s: %s", display, response)
        return 2
    log.info("Launch accepted; waiting %ss for the phone to boot", args.launch_wait)
    time.sleep(args.launch_wait)

    outcome: dict = {}

    def capture(result) -> None:
        if isinstance(result, dict):
            outcome.update(result)

    if args.inspect:
        # Reach the phone the same way the flow would, then only read from it.
        from adb_bot.automation.workflow import prepare_profile_for_adb, connect_with_retries
        from adb_bot.clients.adb import ADBClient

        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, log,
            max_attempts=8, wait_seconds=10, launcher_client=clients.launcher)
        if profile is None:
            log.error("%s never became ADB-ready", display)
            if not args.keep_open:
                clients.shutdown.shutdown_profiles([profile_id])
            return 3
        adb_client = ADBClient()
        target = connect_with_retries(adb_client, profile, log, profile_id)
        if not target:
            log.error("Could not connect ADB to %s", display)
            if not args.keep_open:
                clients.shutdown.shutdown_profiles([profile_id])
            return 3
        try:
            inspect_phone(target, log)
        finally:
            if not args.keep_open:
                log.info("Closing %s", display)
                clients.shutdown.shutdown_profiles([profile_id])
        return 0

    log.info("Running %s on %s with %s", FLOW_NAME, display, photo.name)
    run_profile_workflow(
        profile_id,
        token,
        clients.api,
        clients.adb_enable,
        clients.shutdown,
        automation,
        log,
        flow_name=FLOW_NAME,
        readiness_wait_seconds=10,
        readiness_max_attempts=8,
        media_path=str(photo),
        caption=args.caption,
        target_handle=args.handle,
        shutdown_on_success=not args.keep_open,
        shutdown_on_abort=not args.keep_open,
        result_callback=capture,
        launcher_client=clients.launcher,
    )

    # Three outcomes, not two. "Uncertain" means Share was tapped and we could
    # not prove what happened -- the post may well be live, so this must never
    # be reported as a clean failure, and the run must not simply be repeated.
    if outcome.get("success"):
        log.info("POSTED. %s: %s", display, outcome.get("verify_detail") or "confirmed")
        return 0
    if outcome.get("uncertain"):
        log.warning("UNCERTAIN for %s: Share was tapped but the post could not be confirmed "
                    "(%s). Check the account before running this again -- the ledger will "
                    "refuse the same photo in the meantime.",
                    display, outcome.get("verify_detail") or "no signal")
        return 4
    if outcome.get("already_shared"):
        log.warning("SKIPPED for %s: this exact photo was already shared (%s).",
                    display, outcome.get("verify_detail"))
        return 5
    log.error("NOT POSTED for %s: %s", display, outcome or "flow returned nothing")
    return 1


if __name__ == "__main__":
    sys.exit(main())
