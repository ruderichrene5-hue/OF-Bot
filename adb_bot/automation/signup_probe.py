"""Create an Instagram account by hand on a fleet phone, one step at a time.

    python -m adb_bot.automation.signup_probe open --profile "Blank (12)"
    python -m adb_bot.automation.signup_probe look
    python -m adb_bot.automation.signup_probe tap "Create new account"
    python -m adb_bot.automation.signup_probe type --hint email --value a@b.com
    python -m adb_bot.automation.signup_probe close

**This is deliberately not a flow.** Account creation is the most policed
action on the platform and every screen ahead of it is one nobody here has
seen, so it is driven by a person who looks at each screen and decides the next
move -- which is exactly how `flows/signup.py` will eventually get written
(TODO_2026-08-13 §5 step 2: *the single highest-value hour in the whole plan*).

The difference from `verification_probe` is that the phone **stays open between
commands**. The probe launches, watches for two minutes and shuts down; that is
useless for a chain of twenty screens with a human thinking in between. So
`open` leaves the phone running and writes what it needs to `session.json`,
every later command reattaches to it, and `close` is what finally shuts it
down. The profile lock is held for the whole session and refreshed on each
command, because the 45-minute TTL is shorter than a supervised signup.

Everything a step sees goes into one folder under `~/.adb_bot/signup/` -- dump,
screenshot, text, and a `run.log` of the actions -- so the marker lists in the
eventual flow get written from real screens rather than from guesses. Every
marker list written from general knowledge on 2026-08-12/13 was wrong in some
way; the ones written from a dump worked first time.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows.verification_driver import (
    AdbChallengeDriver,
    VerificationRecorder,
)
from adb_bot.automation.verification_probe import (
    _find_profile,
    _foreground_app,
    _open_instagram,
    _profiles,
    _resolve_token,
    AmbiguousProfile,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.core import locks
from adb_bot.core.adb_commands import swipe as swipe_cmd
from adb_bot.core.logger import get_logger

SESSION_ROOT = Path.home() / ".adb_bot" / "signup"
SESSION_FILE = SESSION_ROOT / "session.json"

# Keyevents worth having names for.
KEYS = {"back": 4, "home": 3, "enter": 66, "del": 67, "tab": 61, "escape": 111}


# --------------------------------------------------------------------- session

def _load_session() -> dict:
    if not SESSION_FILE.exists():
        raise SystemExit(
            "[stop] no signup session is open. Start one with:\n"
            "    python -m adb_bot.automation.signup_probe open --profile \"<name>\"")
    return json.loads(SESSION_FILE.read_text())


def _save_session(data: dict) -> None:
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2))


def _refresh_lock(profile_id: str) -> None:
    """Keep the held lock from ageing out mid-session.

    `locks` calls a lock stale on mtime, and the TTL is 45 minutes -- shorter
    than a supervised signup with a person reading each screen. Touching it on
    every command means the posting loop cannot decide this phone is free and
    launch it underneath us.
    """
    path = locks.lock_dir() / f"{profile_id}.lock"
    try:
        path.touch()
    except OSError:
        pass


def _attach(session: dict, logger, screenshots: bool = True):
    """Rebuild a driver against the phone `open` left running."""
    target = session["target"]
    adb_client = ADBClient()

    # The device can drop out of the local adb server between commands -- there
    # were 107 stale `offline` entries on this box -- so prove the connection
    # before reading anything from it, and reconnect once if it is gone.
    probe = adb_client.run_command(f"adb -s {target} shell echo ok") or ""
    if "ok" not in probe:
        logger.warning("signup: %s did not answer (%r); reconnecting",
                       target, probe.strip()[:120])
        adb_client.run_command(f"adb connect {target}")
        time.sleep(3)
        probe = adb_client.run_command(f"adb -s {target} shell echo ok") or ""
        if "ok" not in probe:
            raise SystemExit(
                f"[stop] the phone at {target} is not reachable any more. "
                f"It may have been shut down -- run `close` and start again.")

    recorder = VerificationRecorder("resumed", logger=logger, enabled=False)
    # One folder for the whole session rather than one per command: the value of
    # the recording is the *order* of the screens, and a folder per step loses it.
    recorder.enabled = True
    recorder.dir = Path(session["run_dir"])
    recorder.index = session.get("step", 0)

    driver = AdbChallengeDriver(target, adb_client, logger=logger,
                               recorder=recorder, act=True,
                               screenshots=screenshots)
    return driver, recorder, adb_client, target


def _remember_step(session: dict, recorder) -> None:
    session["step"] = recorder.index
    _save_session(session)


def _show(driver, target, adb_client, logger) -> str:
    """Read the screen and print what a person needs to choose the next move."""
    from adb_bot.automation.flows import instagram as ig

    activity = ig._adb_get_foreground_activity(target, logger=logger)
    if not activity:
        # Never describe a screen without saying what app drew it. A read taken
        # off the Android launcher looks exactly like a clean Instagram screen.
        print(f"\n[warning] Instagram is NOT in the foreground. "
              f"On top: {_foreground_app(target, adb_client)}")
    else:
        print(f"\nforeground: {activity}")

    text = driver.read_screen()
    fields = driver._edit_fields(driver._root)
    labels = driver._clickable_labels(driver._root)

    print(f"read via  : {driver._source}")
    print(f"\n--- text -------------------------------------------------------")
    print((text or "<nothing read>")[:1800])
    print(f"\n--- input fields ({len(fields)}) ----------------------------------")
    for field in fields:
        print(f"  hint={field['hint']!r:50} value={field['value']!r:20} "
              f"at={field['center']} focused={field['focused']}")
    print(f"\n--- clickable labels ({len(labels)}) ------------------------------")
    for label in labels[:40]:
        print(f"  {label!r}")
    print("-" * 66)
    return text


# --------------------------------------------------------------------- commands

def cmd_open(args) -> int:
    logger = get_logger("adb_bot")

    if SESSION_FILE.exists() and not args.force:
        old = json.loads(SESSION_FILE.read_text())
        raise SystemExit(
            f"[stop] a session is already open on {old.get('name')} "
            f"({old.get('profile_id')}). Close it first:\n"
            f"    python -m adb_bot.automation.signup_probe close\n"
            f"(or pass --force to abandon it -- that leaves the phone running)")

    token = _resolve_token(args.mlx_token)
    try:
        item = _find_profile(_profiles(token), args.profile)
    except AmbiguousProfile as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        return 2
    if item is None:
        print(f"[fatal] no MultiLogin profile named or id'd {args.profile!r}.",
              file=sys.stderr)
        return 2

    profile_id = str(item.get("id"))
    name = str(item.get("serial_name") or profile_id)
    print(f"\n{'=' * 70}\nSUPERVISED SIGNUP -- the phone stays open until `close`\n"
          f"profile : {name} ({profile_id})\ntags    : {item.get('tags') or []}\n"
          f"remark  : {item.get('remark') or '-'}\n{'=' * 70}\n")

    if not locks.acquire(profile_id, owner="signup"):
        print(f"[stop] {name} is busy -- another loop holds its lock.",
              file=sys.stderr)
        return 3

    clients = build_mlx_clients(token)
    adb_client = ADBClient()
    try:
        logger.info("signup: launching %s", name)
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=args.readiness_attempts,
            wait_seconds=args.readiness_wait,
            launcher_client=clients.launcher,
        )
        if not profile:
            raise SystemExit(f"[fail] {name} never became ADB-ready.")

        target = connect_with_retries(adb_client, profile, logger, profile_id,
                                      max_attempts=5, retry_delay_seconds=5)
        if not target:
            raise SystemExit(f"[fail] could not reach {name} over ADB.")

        opened = _open_instagram(target, adb_client, logger)
        if not opened and not args.force:
            raise SystemExit(
                f"[fail] Instagram did not open on {name} -- see the log for "
                f"whether it is even installed. Pass --force to go on anyway.")
    except BaseException:
        # Anything that goes wrong before the session exists must not leave the
        # phone running and the lock held with nothing on disk to close them.
        logger.warning("signup: opening failed; shutting %s back down", profile_id)
        try:
            clients.shutdown.shutdown_profiles([profile_id])
        except Exception:
            pass
        locks.release(profile_id)
        raise

    recorder = VerificationRecorder(f"signup-{name}", root=SESSION_ROOT,
                                    logger=logger)
    session = {
        "profile_id": profile_id,
        "name": name,
        "target": target,
        "run_dir": str(recorder.dir),
        "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "step": 0,
    }
    _save_session(session)
    recorder.event(f"supervised signup session opened on {name} ({profile_id})")

    driver = AdbChallengeDriver(target, adb_client, logger=logger,
                               recorder=recorder, act=True,
                               screenshots=not args.no_screenshots)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    print(f"\nrecording to {recorder.dir}")
    print("next:  look | tap \"<label>\" | type --hint <h> --value <v> | close")
    return 0


def cmd_look(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    _refresh_lock(session["profile_id"])
    driver, recorder, adb_client, target = _attach(
        session, logger, screenshots=not args.no_screenshots)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    return 0


def cmd_tap(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    _refresh_lock(session["profile_id"])
    driver, recorder, adb_client, target = _attach(session, logger)

    driver.read_screen()
    center = driver._find_exact([args.label])
    if center is None:
        labels = driver._clickable_labels(driver._root)
        print(f"[stop] nothing on screen is exactly {args.label!r}. "
              f"Clickable labels are:\n" +
              "\n".join(f"  {l!r}" for l in labels[:40]), file=sys.stderr)
        _remember_step(session, recorder)
        return 2

    recorder.event(f"tap {args.label!r} at {center}")
    driver._tap(center, f"{args.label!r}")
    time.sleep(args.settle)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    return 0


def cmd_tapxy(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    _refresh_lock(session["profile_id"])
    driver, recorder, adb_client, target = _attach(session, logger)
    recorder.event(f"tap raw coordinates ({args.x}, {args.y})")
    driver._tap((args.x, args.y), f"({args.x}, {args.y})")
    time.sleep(args.settle)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    return 0


def cmd_type(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    _refresh_lock(session["profile_id"])
    driver, recorder, adb_client, target = _attach(session, logger)

    driver.read_screen()
    hints = [h.strip().lower() for h in args.hint.split(",") if h.strip()]
    field = driver._pick_field(hints)
    if field is None:
        print(f"[stop] no field on screen matches {hints}. Fields are:\n" +
              "\n".join(f"  hint={f['hint']!r} value={f['value']!r}"
                        for f in driver._edit_fields(driver._root)),
              file=sys.stderr)
        _remember_step(session, recorder)
        return 2

    # Secrets are typed, never printed: this run log is a debugging artefact
    # that gets pasted around, and a password in it outlives the account.
    shown = args.value if not args.secret else f"<{len(args.value)} characters>"
    recorder.event(f"type into hint={field['hint']!r}: {shown}")
    driver._type(field, args.value, args.hint)
    time.sleep(args.settle)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    return 0


def cmd_key(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    _refresh_lock(session["profile_id"])
    driver, recorder, adb_client, target = _attach(session, logger)
    code = KEYS.get(str(args.key).lower(), None)
    if code is None:
        try:
            code = int(args.key)
        except ValueError:
            raise SystemExit(f"[stop] unknown key {args.key!r}. "
                             f"Known: {', '.join(KEYS)} or a number.")
    recorder.event(f"keyevent {args.key} ({code})")
    adb_client.run_command(f"adb -s {target} shell input keyevent {code}")
    time.sleep(args.settle)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    return 0


def cmd_swipe(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    _refresh_lock(session["profile_id"])
    driver, recorder, adb_client, target = _attach(session, logger)
    recorder.event(f"swipe {args.x1},{args.y1} -> {args.x2},{args.y2}")
    adb_client.run_command(
        f"adb -s {target} shell {swipe_cmd(args.x1, args.y1, args.x2, args.y2)}")
    time.sleep(args.settle)
    _show(driver, target, adb_client, logger)
    _remember_step(session, recorder)
    return 0


def cmd_status(args) -> int:
    session = _load_session()
    print(json.dumps(session, indent=2))
    held = locks.is_locked(session["profile_id"])
    print(f"\nlock held: {held}")
    print(f"recording: {session['run_dir']}")
    return 0


def cmd_close(args) -> int:
    logger = get_logger("adb_bot")
    session = _load_session()
    profile_id = session["profile_id"]

    token = _resolve_token(args.mlx_token)
    clients = build_mlx_clients(token)
    print(f"shutting down {session['name']} ({profile_id})")
    try:
        clients.shutdown.shutdown_profiles([profile_id])
    except Exception as exc:
        logger.warning("signup: shutdown failed for %s (%s)", profile_id, exc)
        print(f"[warning] MultiLogin refused the shutdown ({exc}). "
              f"The phone may still be running -- check the MLX console.")
    locks.release(profile_id)

    run_dir = session.get("run_dir")
    try:
        SESSION_FILE.unlink()
    except OSError:
        pass
    print(f"session closed. Everything it saw is in:\n  {run_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an Instagram account by hand on a fleet phone.")
    parser.add_argument("--mlx-token")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, settle_default=2.5):
        p.add_argument("--settle", type=float, default=settle_default,
                       help="seconds to wait before reading the next screen")
        p.add_argument("--no-screenshots", action="store_true")

    p_open = sub.add_parser("open", help="launch a profile and hold it open")
    p_open.add_argument("--profile", required=True)
    p_open.add_argument("--force", action="store_true")
    p_open.add_argument("--readiness-attempts", type=int, default=8)
    p_open.add_argument("--readiness-wait", type=int, default=15)
    common(p_open)
    p_open.set_defaults(func=cmd_open)

    p_look = sub.add_parser("look", help="read the screen, change nothing")
    common(p_look)
    p_look.set_defaults(func=cmd_look)

    p_tap = sub.add_parser("tap", help="tap a node by its exact label")
    p_tap.add_argument("label")
    common(p_tap)
    p_tap.set_defaults(func=cmd_tap)

    p_xy = sub.add_parser("tapxy", help="tap raw coordinates")
    p_xy.add_argument("x", type=int)
    p_xy.add_argument("y", type=int)
    common(p_xy)
    p_xy.set_defaults(func=cmd_tapxy)

    p_type = sub.add_parser("type", help="type into a field chosen by hint")
    p_type.add_argument("--hint", required=True,
                        help="comma-separated hint words, e.g. 'email,mail'")
    p_type.add_argument("--value", required=True)
    p_type.add_argument("--secret", action="store_true",
                        help="keep the value out of the run log")
    common(p_type)
    p_type.set_defaults(func=cmd_type)

    p_key = sub.add_parser("key", help="send a keyevent (back, enter, ...)")
    p_key.add_argument("key")
    common(p_key)
    p_key.set_defaults(func=cmd_key)

    p_swipe = sub.add_parser("swipe", help="swipe between two points")
    for arg in ("x1", "y1", "x2", "y2"):
        p_swipe.add_argument(arg, type=int)
    common(p_swipe)
    p_swipe.set_defaults(func=cmd_swipe)

    p_status = sub.add_parser("status", help="what session is open")
    p_status.set_defaults(func=cmd_status)

    p_close = sub.add_parser("close", help="shut the phone down, release the lock")
    p_close.set_defaults(func=cmd_close)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
