"""Point the verification flow at one flagged profile, and watch what it sees.

    python -m adb_bot.automation.verification_probe --list
    python -m adb_bot.automation.verification_probe --profile "Jil 2"
    python -m adb_bot.automation.verification_probe --profile "Jil 2" --apply

**`--list`** shows the profiles carrying the MultiLogin `Issue` tag -- the
fleet's flag for "a person has to look at this" -- so you can pick one.

**Without `--apply` nothing is touched.** The profile is launched, Instagram is
opened, and then the run reads the screen on a timer: classifying it, saving the
UI dump, a screenshot and the extracted text into `~/.adb_bot/verification/`,
and printing what the real flow *would* have done. No number is rented, no
captcha is bought, nothing is tapped or typed. That is the intended first run
against any profile, and it is how the marker lists in `verification.py` get
corrected from real screens rather than guesses (TODO_2026-08-12 §3).

**With `--apply`** the same driver runs for real through `run_verification`:
numbers get rented, codes typed, captchas bought. It spends money.

Either way the profile lock is taken first, so this cannot collide with the
posting or warm-up loop working the same phone -- and the profile is shut down
at the end, on every exit path, because a phone left open holds one of the
fleet's concurrency slots.
"""

from __future__ import annotations

import argparse
import sys
import time

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import verification
from adb_bot.automation.flows.verification_driver import (
    RUN_ROOT,
    AdbChallengeDriver,
    VerificationRecorder,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

# The MultiLogin tag that means "a person has to look at this". Applying it
# flags a profile and removing it clears and reactivates it -- it is the whole
# interface, so it is also the right way to find work for this flow.
ISSUE_TAG = "Issue"

# How long to watch, and how often to look, in observation mode. Instagram's
# challenge screens can take a while to appear after the app opens, and some
# only appear after the feed has loaded.
OBSERVE_SECONDS = 120
OBSERVE_INTERVAL = 10


def _resolve_token(explicit=None) -> str:
    from adb_bot.automation.run_loop import _mlx_token
    token = _mlx_token(explicit)
    if not token:
        raise SystemExit("[fatal] No MultiLogin token "
                         "(MULTILOGIN_TOKEN / dev settings / --mlx-token).")
    return token


def _profiles(token: str) -> list:
    return MultiloginMobileListClient(token).list_mobile_profiles()


class AmbiguousProfile(LookupError):
    """More than one MultiLogin profile answers to that name."""

    def __init__(self, name: str, matches) -> None:
        self.name = name
        self.matches = list(matches)
        ids = "\n".join(f"    --profile {m.get('id')}   "
                        f"(tags={m.get('tags') or []}, "
                        f"remark={str(m.get('remark') or '-')[:40]!r})"
                        for m in self.matches)
        super().__init__(
            f"{len(self.matches)} profiles are named {name!r}. Pass the id "
            f"instead:\n{ids}")


def _find_profile(items, wanted: str) -> dict | None:
    """Match on id first, then on name, so either can be passed to --profile.

    Names are **not unique** in this workspace: `Blank (10)`, `Blank (11)` and
    `Blank (13)` each name two different profiles, seen 2026-08-11. Returning
    the first match would silently pick one of them -- which is tolerable for a
    read-only look and not at all tolerable for `--apply`, where it means
    renting numbers against an account nobody chose. So a duplicate name raises
    rather than guesses, the same way the driver's field picker does.

    Ids are checked first because an id is never ambiguous.
    """
    wanted = str(wanted).strip()
    for item in items:
        if str(item.get("id", "")).strip() == wanted:
            return item

    needle = wanted.lower()
    matches = [item for item in items
               if str(item.get("serial_name", "")).strip().lower() == needle]
    if len(matches) > 1:
        raise AmbiguousProfile(wanted, matches)
    return matches[0] if matches else None


def cmd_list(args) -> int:
    items = _profiles(_resolve_token(args.mlx_token))
    flagged = [i for i in items if ISSUE_TAG in (i.get("tags") or [])]
    print(f"{len(flagged)} profile(s) carrying the '{ISSUE_TAG}' tag "
          f"(of {len(items)} total)\n")
    # Names repeat in this workspace, so say which ones do. A duplicate name
    # cannot be passed to --profile at all, and it is better to see that here
    # than to have the run refuse later.
    counts: dict = {}
    for item in flagged:
        name = str(item.get("serial_name") or "?")
        counts[name] = counts.get(name, 0) + 1

    for item in sorted(flagged, key=lambda i: str(i.get("serial_name") or "")):
        name = str(item.get("serial_name") or "?")
        remark = str(item.get("remark") or "").strip()
        dup = "  [name shared -- use the id]" if counts.get(name, 0) > 1 else ""
        print(f"  {name:18} {item.get('id')}"
              f"{'   -- ' + remark if remark else ''}{dup}")

    shared = sum(1 for n, c in counts.items() if c > 1)
    if shared:
        print(f"\n{shared} name(s) belong to more than one profile.")
    print("\nPick one and run:  --profile \"<name or id>\"   "
          "(add --apply to act on it)")
    return 0


INSTAGRAM_PACKAGE = "com.instagram.android"
# Android's own runtime-permission dialog. It overlays Instagram, holds the
# foreground, and nothing dismisses it on its own.
PERMISSION_CONTROLLER_PACKAGE = "com.android.permissioncontroller"

# How long to give Instagram to reach the foreground before giving up on it.
#
# 45s was not enough: in a 14-profile sweep three phones sat on
# `com.android.launcher3` for the whole window while `am start` kept reporting
# `Starting: Intent{...}` quite happily. The same profiles had opened fine in an
# earlier, smaller sweep, so this is contention -- the posting and warm-up loops
# are launching phones at the same time -- rather than anything wrong with those
# accounts. Waiting longer is nearly free; a false "Instagram did not open"
# costs a profile its turn.
APP_START_SECONDS = 75

# Re-issue the start intent every so often while waiting. One `am start` that
# lands during the phone's own boot animation can be dropped silently.
RESTART_EVERY_SECONDS = 24

# A fresh profile can face several permission dialogs in a row. Each one
# cleared buys this much more launch time, up to this many of them -- enough
# for a real chain, not enough for a dialog that keeps coming back.
MAX_PERMISSION_DIALOGS = 6
PERMISSION_GRACE_SECONDS = 30


def _foreground_app(target: str, adb_client) -> str:
    """Whatever app is on top right now, as a readable string.

    Deliberately not `instagram._adb_get_foreground_activity`, which only ever
    reports Instagram and returns None for everything else -- useless for saying
    what went wrong. This answers the actual question.
    """
    out = adb_client.run_command(
        f"adb -s {target} shell dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    text = " ".join((out or "").split())
    return text[:200] or "<could not read the foreground>"


def _clear_permission_dialog(target, adb_client, logger) -> bool:
    """Grant an Android runtime-permission dialog sitting on top of Instagram.

    `632451306307322212` was held here for the full 75s launch window on
    2026-08-12: `com.android.permissioncontroller/.GrantPermissionsActivity` had
    the foreground, so Instagram never reached it and the probe -- correctly --
    refused to read the screen. The phone was fine; nothing was ever going to
    dismiss the dialog.

    Reuses `interruptions`, which the posting and warm-up flows already grant
    these with, rather than inventing a second answer to the same question. Two
    properties of that code matter here: the label match is EXACT, so "Allow"
    can never hit "Don't allow", and the tap only ever comes from a UI dump,
    never from OCR.

    Gated by the caller on the *foreground package*, not on text markers. The
    package is definitive -- a dialog whose wording nobody has seen still
    reports as `permissioncontroller`, and this fleet is not one Android build.
    """
    from adb_bot.automation.flows import instagram as ig
    from adb_bot.automation.flows import interruptions

    root = ig._adb_capture_ui_dump(target, logger=logger)
    if root is None:
        logger.warning("probe: a permission dialog is up but its screen could not "
                       "be read; leaving it alone")
        return False
    return interruptions._advance_permission_screen(
        target, adb_client, root, logger=logger)


def _open_instagram(target: str, adb_client, logger) -> bool:
    """Start Instagram and confirm it actually reached the foreground.

    Confirming matters more than starting: the first run of this probe watched
    the Android home screen for two minutes and reported "no verification screen
    found", because `monkey` had returned quietly without launching anything.
    A probe that cannot tell "no challenge" from "not looking at Instagram" is
    worse than useless -- it produces a confident wrong answer.
    """
    from adb_bot.automation.flows import instagram as ig

    installed = adb_client.run_command(
        f"adb -s {target} shell pm list packages {INSTAGRAM_PACKAGE}") or ""
    if INSTAGRAM_PACKAGE not in installed:
        logger.error("probe: Instagram is NOT INSTALLED on %s (pm list packages "
                     "returned %r). Nothing to verify on this phone.",
                     target, installed.strip())
        return False
    logger.info("probe: Instagram is installed on %s", target)

    # `am start` first, `monkey` as the fallback. That is the opposite of the
    # order the posting flow uses, and it is deliberate: on these MLX cloud
    # phones `monkey` returned an empty string and started nothing, three runs
    # running, while the explicit intent worked immediately. `monkey` is kept as
    # the fallback because it does not need the activity name to be right.
    logger.info("probe: starting Instagram on %s", target)
    out = adb_client.run_command(
        f"adb -s {target} shell am start -n "
        f"{INSTAGRAM_PACKAGE}/.activity.MainTabActivity")
    logger.info("probe: am start said %r", (out or "").strip()[:200])

    deadline = time.monotonic() + APP_START_SECONDS
    attempt = 0
    dialogs_cleared = 0
    while time.monotonic() < deadline:
        attempt += 1
        time.sleep(3)
        activity = ig._adb_get_foreground_activity(target, logger=logger)
        if activity and INSTAGRAM_PACKAGE in activity:
            # The activity name is worth logging in full: it is what said
            # "signed out" on Jil 2 (BloksSignedOutFragmentActivity) before any
            # screen had been read.
            logger.info("probe: Instagram is in the foreground on %s, activity "
                        "%s", target, activity)
            time.sleep(4)          # let the first screen finish drawing
            return True

        # `_adb_get_foreground_activity` only ever reports Instagram, so a None
        # from it means "not Instagram" and not "could not read". Ask what IS on
        # top separately, or the log says nothing about what went wrong.
        foreground = _foreground_app(target, adb_client)
        logger.info("probe: after %ds Instagram is not in front; the foreground "
                    "is %s", attempt * 3, foreground)

        # An Android permission dialog is not a failed launch -- Instagram is
        # running fine underneath it. Re-issuing the start intent (below) does
        # nothing at all here, which is exactly what the 75s of identical log
        # lines on `Default profile name (47)` were.
        if PERMISSION_CONTROLLER_PACKAGE in (foreground or ""):
            logger.warning("probe: an Android permission dialog is covering "
                           "Instagram; granting it")
            if _clear_permission_dialog(target, adb_client, logger):
                # Clearing one is progress, not waiting, so it must not eat the
                # launch budget: a fresh profile can face a chain of these, and
                # timing out halfway through would report a phone that was
                # actively being fixed as one that never started. Bounded, so a
                # dialog that reappears for ever still ends the run.
                dialogs_cleared += 1
                if dialogs_cleared <= MAX_PERMISSION_DIALOGS:
                    deadline = max(deadline,
                                   time.monotonic() + PERMISSION_GRACE_SECONDS)
                # Straight back to the foreground check: the dialog may be one
                # of a chain, and each round of this loop clears one.
                continue

        # Re-issue the start periodically rather than once. An intent that
        # lands while the phone is still finishing its own boot is dropped
        # without complaint -- `am start` still prints "Starting: Intent{...}".
        if attempt * 3 % RESTART_EVERY_SECONDS == 0:
            if attempt * 3 % (RESTART_EVERY_SECONDS * 2) == 0:
                logger.warning("probe: still not in the foreground; trying monkey")
                out = adb_client.run_command(
                    f"adb -s {target} shell monkey -p {INSTAGRAM_PACKAGE} "
                    f"-c android.intent.category.LAUNCHER 1")
                logger.info("probe: monkey said %r", (out or "").strip()[:200])
            else:
                logger.warning("probe: still not in the foreground; re-issuing "
                               "the start intent")
                out = adb_client.run_command(
                    f"adb -s {target} shell am start -n "
                    f"{INSTAGRAM_PACKAGE}/.activity.MainTabActivity")
                logger.info("probe: am start said %r", (out or "").strip()[:200])

    logger.error("probe: Instagram never reached the foreground on %s within %ds. "
                 "Whatever is on screen is NOT Instagram, so any screen read "
                 "below says nothing about this account's verification state.",
                 target, APP_START_SECONDS)
    return False


def _observe(driver, logger, seconds: int, interval: int, target=None) -> dict:
    """Watch the screen without touching it. Returns what was seen, by kind."""
    from adb_bot.automation.flows import instagram as ig

    seen: dict = {}
    deadline = time.monotonic() + seconds
    round_index = 0

    while time.monotonic() < deadline:
        round_index += 1
        remaining = int(deadline - time.monotonic())
        logger.info("probe: look %d (%ds left of the observation window)",
                    round_index, remaining)

        # Which app is on top is recorded every look, not just at the start.
        # Instagram can be killed or backgrounded mid-run, and a screen read
        # from the launcher classifies as a clean "none" -- indistinguishable
        # from a healthy account unless the foreground is checked too.
        if target is not None:
            activity = ig._adb_get_foreground_activity(target, logger=logger)
            if activity and INSTAGRAM_PACKAGE not in activity:
                logger.warning(
                    "probe: the foreground app is %r, NOT Instagram. This look "
                    "says nothing about the account.", activity)
                seen["not-instagram"] = seen.get("not-instagram", 0) + 1
                time.sleep(interval)
                continue

        text = driver.read_screen()
        challenge = verification.classify_challenge(text)
        seen[challenge] = seen.get(challenge, 0) + 1

        if challenge == verification.CHALLENGE_NONE and text:
            # The most valuable line in the whole run: a challenge screen the
            # markers do not recognise looks exactly like a healthy screen, and
            # the only way to tell them apart is to read the text.
            logger.warning(
                "probe: this screen classified as NONE. If it is in fact a "
                "verification screen, its wording is missing from the marker "
                "lists in flows/verification.py (TODO_2026-08-12 §3). Text was: %r",
                (text or "")[:600])
        else:
            logger.info("probe: screen looks like %s", challenge)

        if challenge in (verification.CHALLENGE_PHONE, verification.CHALLENGE_CODE,
                         verification.CHALLENGE_PHOTO,
                         verification.CHALLENGE_IMAGE_CAPTCHA,
                         verification.CHALLENGE_CHOOSE_METHOD):
            logger.info("probe: --apply would now handle the %s screen", challenge)

        time.sleep(interval)

    return seen


def cmd_probe(args) -> int:
    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)

    items = _profiles(token)
    try:
        profile_item = _find_profile(items, args.profile)
    except AmbiguousProfile as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        return 2
    if profile_item is None:
        print(f"[fatal] no MultiLogin profile named or id'd {args.profile!r}. "
              f"Try --list.", file=sys.stderr)
        return 2

    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    tags = profile_item.get("tags") or []
    mode = "APPLY (spends money, taps the phone)" if args.apply else "OBSERVE ONLY"

    logger.info("probe: %s -- profile %s (%s), tags=%s", mode, name, profile_id, tags)
    print(f"\n{'=' * 70}\n{mode}\nprofile : {name} ({profile_id})\ntags    : {tags}\n"
          f"remark  : {profile_item.get('remark') or '-'}\n{'=' * 70}\n")

    if ISSUE_TAG not in tags and not args.force:
        print(f"[stop] {name} does not carry the '{ISSUE_TAG}' tag, so nothing has "
              f"flagged it for a person to look at. Pass --force to run anyway.",
              file=sys.stderr)
        return 2

    recorder = VerificationRecorder(name, logger=logger)
    clients = build_mlx_clients(token)
    adb_client = ADBClient()

    # The posting and warm-up loops work the same phones. Without this lock a
    # probe can launch a profile a loop is mid-post on, which loses the post and
    # looks like a random Instagram failure.
    with locks.ProfileLocks(owner="verification") as held:
        if not held.acquire_all([profile_id]):
            print(f"[stop] {name} is busy -- another loop holds its lock. "
                  f"Try again in a few minutes.", file=sys.stderr)
            return 3

        try:
            return _run_on_phone(args, clients, adb_client, recorder, logger,
                                 profile_id, name)
        finally:
            # A phone left open holds one of the fleet's concurrency slots, so
            # it is closed whatever happened -- including a crash above.
            logger.info("probe: shutting down profile %s", profile_id)
            try:
                clients.shutdown.shutdown_profiles([profile_id])
            except Exception as exc:
                logger.warning("probe: shutdown failed for %s (%s)", profile_id, exc)


def _run_on_phone(args, clients, adb_client, recorder, logger,
                  profile_id: str, name: str) -> int:
    logger.info("probe: launching %s", name)
    clients.launcher.start_profiles([profile_id])

    profile = prepare_profile_for_adb(
        profile_id, clients.api, clients.adb_enable, logger,
        max_attempts=args.readiness_attempts,
        wait_seconds=args.readiness_wait,
        launcher_client=clients.launcher,
    )
    if not profile:
        print(f"[fail] {name} never became ADB-ready. This is usually MultiLogin's "
              f"cloud being slow -- raise --readiness-attempts and retry.",
              file=sys.stderr)
        return 4

    target = connect_with_retries(adb_client, profile, logger, profile_id,
                                  max_attempts=5, retry_delay_seconds=5)
    if not target:
        print(f"[fail] could not reach {name} over ADB.", file=sys.stderr)
        return 4

    if not _open_instagram(target, adb_client, logger) and not args.force:
        print(f"[fail] Instagram did not open on {name}. Reading the screen now "
              f"would only describe the Android home screen -- see the log above "
              f"for whether the app is even installed. Pass --force to look "
              f"anyway.", file=sys.stderr)
        return 5

    driver = AdbChallengeDriver(target, adb_client, logger=logger,
                                recorder=recorder, act=bool(args.apply),
                                screenshots=not args.no_screenshots)

    if not args.apply:
        seen = _observe(driver, logger, args.seconds, args.interval, target=target)
        print(f"\n{'=' * 70}\nwhat this profile showed, by screen kind:")
        for kind, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:16} x{count}")
        if recorder.dir:
            print(f"\nevery screen saved to: {recorder.dir}")
            print("  *.xml  the UI dump      *.png  a screenshot")
            print("  *.txt  the text read    screens.jsonl  one line per look")
        print(f"{'=' * 70}\n")
        if seen.get("not-instagram"):
            print(f"\n{seen['not-instagram']} look(s) found something other than "
                  f"Instagram in the foreground. Those say nothing about the "
                  f"account.")
        if set(seen) <= {verification.CHALLENGE_NONE}:
            print("No verification screen was recognised in the whole window.\n"
                  "Either this profile is not actually showing one, or its "
                  "wording is missing from the marker lists (TODO_2026-08-12 §3) --\n"
                  "read the saved .txt files to tell which.")
        return 0

    from adb_bot.clients.sms.base import DEFAULT_COUNTRY
    from adb_bot.clients.sms.router import build_router
    router = build_router(logger=logger)
    country = args.country or DEFAULT_COUNTRY
    logger.info("probe: renting %s numbers for %s", country, name)
    result = verification.run_verification(driver, router, logger=logger,
                                           country=country)

    print(f"\n{'=' * 70}\nresult  : {result.status}\ndetail  : {result.detail}\n"
          f"screens : {result.steps}\nnumbers : {result.numbers_used}\n"
          f"code    : {'received' if result.code_received else 'never arrived'}")
    if recorder.dir:
        print(f"saved   : {recorder.dir}")
    print(f"{'=' * 70}\n")

    # Deliberately NOT clearing the Issue tag here. This is the probe; a solved
    # profile still wants a person to confirm before it goes back into posting,
    # and the tag is the only thing keeping it out (TODO_2026-08-12 §4.2 / 4.3).
    if result.ok:
        print(f"{name} cleared its verification chain. The '{ISSUE_TAG}' tag was "
              f"left on: check the account by hand, then remove the tag to put "
              f"it back into the loops.")
    return 0 if result.ok else 1


def cmd_sweep(args) -> int:
    """Look at several flagged profiles in turn and report what each showed.

    This is how a real challenge screen actually gets found. The `Issue` tag is
    hand-applied and covers several different problems, so most tagged profiles
    are *not* mid-challenge: of the first two looked at, one was signed out and
    one was on a perfectly healthy feed with a stale tag. Working through them
    one command at a time is too slow to be worth doing, so this does the loop.

    Nothing is ever acted on here -- sweeping is observation only, by
    construction. Each profile costs about two minutes, almost all of it waiting
    for MultiLogin to start the phone.
    """
    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)
    flagged = [i for i in items if ISSUE_TAG in (i.get("tags") or [])]

    # Profiles whose remark already says somebody saw a verification screen are
    # the best odds, so they go first; the rest follow in name order.
    def priority(item):
        remark = str(item.get("remark") or "").lower()
        return (0 if "verif" in remark else 1,
                str(item.get("serial_name") or ""))

    queue = sorted(flagged, key=priority)[:args.sweep]
    print(f"sweeping {len(queue)} of {len(flagged)} flagged profile(s), "
          f"{args.seconds}s each\n")

    findings = []
    for position, item in enumerate(queue, start=1):
        name = str(item.get("serial_name") or item.get("id"))
        print(f"[{position}/{len(queue)}] {name} ...", flush=True)
        one = argparse.Namespace(**vars(args))
        one.profile = str(item.get("id"))
        one.apply = False
        try:
            cmd_probe(one)
            findings.append((name, "see the recording"))
        except Exception as exc:
            logger.warning("sweep: %s raised (%s)", name, exc)
            findings.append((name, f"error: {exc}"))

    print(f"\n{'=' * 70}\nswept {len(findings)} profile(s). Every screen is under "
          f"{RUN_ROOT}\nRead the .txt of any look that classified as 'none' -- "
          f"that is where a missing marker hides.\n{'=' * 70}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adb_bot.automation.verification_probe",
        description="Watch (or run) the verification flow against one flagged profile.")
    parser.add_argument("--profile", help="MultiLogin profile name ('Jil 2') or id.")
    parser.add_argument("--list", action="store_true",
                        help=f"list the profiles carrying the '{ISSUE_TAG}' tag and exit.")
    parser.add_argument("--sweep", type=int, metavar="N",
                        help="observe the first N flagged profiles in turn "
                             "(verification-remarked ones first) instead of one "
                             "named profile. Always observation only. This is how "
                             "a real challenge screen gets found -- most flagged "
                             "profiles are not showing one.")
    parser.add_argument("--apply", action="store_true",
                        help="actually solve the challenges. Rents numbers and buys "
                             "captcha solves -- this spends money. Without it the run "
                             "only reads and records.")
    parser.add_argument("--force", action="store_true",
                        help=f"run even on a profile with no '{ISSUE_TAG}' tag, and "
                             f"even if Instagram never opens.")
    parser.add_argument("--country", default=None,
                        help="canonical country to rent numbers from with --apply "
                             "(default comes from the SMS layer, currently DE -- "
                             "the profiles are German and the challenge screen's "
                             "country picker is +49).")
    parser.add_argument("--no-screenshots", action="store_true",
                        help="skip the per-screen screenshot. Each is ~2MB and takes "
                             "about ten seconds off an MLX cloud phone, so this makes "
                             "the looks much closer together -- at the cost of the "
                             "pictures, which are often what explains a screen.")
    parser.add_argument("--seconds", type=int, default=OBSERVE_SECONDS,
                        help=f"observation window (default {OBSERVE_SECONDS}).")
    parser.add_argument("--interval", type=int, default=OBSERVE_INTERVAL,
                        help=f"seconds between looks (default {OBSERVE_INTERVAL}).")
    parser.add_argument("--readiness-attempts", type=int, default=8,
                        help="how many times to wait for the phone to become "
                             "ADB-ready (default 8; a cold MLX phone takes ~45s).")
    parser.add_argument("--readiness-wait", type=int, default=15,
                        help="seconds per readiness attempt (default 15).")
    parser.add_argument("--mlx-token", default=None)
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args)
    if args.sweep:
        return cmd_sweep(args)
    if not args.profile:
        parser.error("pass --profile <name|id>, --sweep N, or --list to see "
                     "what is flagged.")
    return cmd_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
