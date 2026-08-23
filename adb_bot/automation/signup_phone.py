"""Take one blank phone all the way to an Instagram account, in a single launch.

    python -m adb_bot.automation.signup_phone --profile "Blank caio 1"
    python -m adb_bot.automation.signup_phone --profile "Blank caio 1" --apply

Four steps that used to be four separate sessions:

1. put the assigned Gmail on the phone, through the **Play Store**;
2. install Instagram from the Play Store, by deep link;
3. create the account, taking the email hatch rather than renting a number;
4. write the credentials down, twice -- locally first, then into the VA base.

**One launch, because these phones live about fifteen minutes** and there is no
resuming a half-made Instagram account: a restarted app returns to "Join
Instagram" and throws away an already-verified code. Steps 1 and 2 are the
exception -- both persist on the MLX profile, so a second run of this skips
them and goes straight to the signup with most of the phone's life still ahead
of it.

Dry-run by default. `--apply` is what launches phones and creates accounts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.flows import (
    google_signin, play_install, signup, verification,
)
from adb_bot.automation.flows.gmail_code import GMAIL_PACKAGE, PhoneMailbox
from adb_bot.automation.flows.signup_driver import AdbSignupDriver
from adb_bot.automation.flows.verification_driver import AdbChallengeDriver
from adb_bot.automation.signup_identity import make_identity, record_account
from adb_bot.automation.verification_probe import (
    _find_profile, _profiles, _resolve_token,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

INSTAGRAM_PACKAGE = "com.instagram.android"

# How long one of these phones is worth planning around. They die by themselves
# at about fifteen minutes with no exit line anywhere, and there is no resuming
# a half-made account, so everything a run intends to do has to fit inside this.
PHONE_LIFE_SECONDS = 13 * 60

# The least time worth starting a verification chain in. Below this the run
# would rent a number -- real money, and 45 seconds before it is even usable --
# on a phone that will die before the code can be typed. Skipping and saying so
# leaves an account somebody can verify later; starting and dying wastes the
# number and tells nobody.
MIN_VERIFY_SECONDS = 150

# Which mailbox belongs to which phone. Written by whoever reserved them, so a
# rerun uses the same address rather than burning a second one.
ASSIGNMENTS = Path.home() / ".adb_bot" / "mailboxes.json"


class MlxHost:
    """Launch and stop one MultiLogin phone.

    The three steps between -- sign the mailbox in, install the apps, create the
    account -- are the same wherever the phone is hosted, and were written
    against MultiLogin only because that is where the fleet lived. Keeping the
    host behind this seam is what lets the identical, hard-won chain run on
    Geelark without a second copy of it drifting out of step.
    """

    def __init__(self, clients, args) -> None:
        self.clients = clients
        self.args = args

    def launch(self, profile_id: str, logger):
        self.clients.launcher.start_profiles([profile_id])
        return prepare_profile_for_adb(
            profile_id, self.clients.api, self.clients.adb_enable, logger,
            max_attempts=self.args.readiness_attempts,
            wait_seconds=self.args.readiness_wait,
            launcher_client=self.clients.launcher)

    def shutdown(self, profile_id: str, logger) -> None:
        try:
            self.clients.shutdown.shutdown_profiles([profile_id])
        except Exception as exc:
            logger.warning("signup_phone: shutdown failed for %s (%s)",
                           profile_id, exc)


class GeelarkHost:
    """Launch and stop one Geelark cloud phone.

    Stopping matters more here than on MultiLogin: a Geelark phone left running
    bills by the minute *and* holds one of only four parallel slots, so a
    forgotten phone stalls the next run as well as costing money.

    `proxy_port`, when given, is leased exclusively for the phone's whole
    life (`launch` acquires, `shutdown` releases) -- confirmed live
    2026-08-23: this pipeline was starting phones on their statically
    assigned proxy with no check that another phone was already running on
    the same port, which the four-modem pool cannot tell apart as two
    devices. `adb_bot.clients.geelark.session` already solved this for its
    own single-phone manual path; this reuses the same `proxy_pool` rather
    than inventing a second mechanism.
    """

    def __init__(self, transport=None, args=None, proxy_port=None) -> None:
        from adb_bot.clients.geelark import GeelarkTransport

        self.transport = transport or GeelarkTransport()
        self.args = args
        self.proxy_port = proxy_port
        self._lease = None

    def launch(self, profile_id: str, logger):
        from adb_bot.clients.geelark import prepare_geelark_profile_for_adb
        from adb_bot.clients.geelark import proxy_pool

        if self.proxy_port is not None:
            self._lease = proxy_pool.acquire_proxy(
                [self.proxy_port], owner=str(profile_id), wait_seconds=60.0)
            if self._lease is None:
                logger.warning(
                    "signup_phone: proxy port %s is already leased by "
                    "another running phone; not starting %s on it",
                    self.proxy_port, profile_id)
                return None

        profile = prepare_geelark_profile_for_adb(
            profile_id, self.transport, logger=logger)
        if profile is None and self._lease is not None:
            # Never strand a lease on a launch that did not happen.
            proxy_pool.release_proxy(self._lease)
            self._lease = None
        return profile

    def shutdown(self, profile_id: str, logger) -> None:
        from adb_bot.clients.geelark import release_geelark_phone
        from adb_bot.clients.geelark import proxy_pool

        if self._lease is not None:
            proxy_pool.release_proxy(self._lease)
            self._lease = None
        try:
            release_geelark_phone(profile_id, self.transport, logger=logger)
        except Exception as exc:
            logger.warning("signup_phone: shutdown failed for %s (%s)",
                           profile_id, exc)


def open_instagram(driver, adb_client, target: str, logger=None,
                   attempts: int = 5, wait_seconds: int = 8) -> bool:
    """Bring Instagram to the front, and prove it got there.

    Two things this replaces, both of which failed silently.

    **The launcher intent was missing.** Every other Instagram launch in this
    repo -- reels, stories, the posting flow -- fires
    `monkey -c android.intent.category.LAUNCHER` *before* naming the activity.
    Only the signup path named `.activity.MainTabActivity` directly, and a
    freshly installed Instagram does not always expose it, so the start was a
    no-op that returned nothing anyone looked at.

    **The wait was a fixed twelve seconds.** Whatever was on screen when it
    elapsed became the signup's first screen. On Geelark that was the Play
    Store's signed-out page, which the flow could not name, so four phones were
    written off for `unknown_screen` at step 1 with Instagram never opened and
    no number ever rented.

    So: launcher intent first, then the activity, then *look* -- and if
    Instagram is not there yet, say so and try again.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level)("open_instagram: " + message, *args)

    for attempt in range(1, attempts + 1):
        adb_client.run_command(
            f"adb -s {target} shell monkey -p {INSTAGRAM_PACKAGE} "
            f"-c android.intent.category.LAUNCHER 1")
        adb_client.run_command(
            f"adb -s {target} shell am start -n "
            f"{INSTAGRAM_PACKAGE}/.activity.MainTabActivity")
        time.sleep(wait_seconds)
        screen = driver.read_screen() or ""
        # Ask the dump who drew the screen, and only fall back to "does the
        # classifier recognise it" when the driver cannot say. The classifier
        # answers no for every screen nobody has named yet -- Instagram's
        # "set up on new device" onboarding and Meta's ads consent among them --
        # so judging by it relaunched Instagram five times over an app that was
        # fully drawn and in front.
        showing = getattr(driver, "showing_package", None)
        if callable(showing):
            if showing(INSTAGRAM_PACKAGE):
                log("info", "instagram is in front after %d attempt(s)",
                    attempt)
                return True
        elif signup.classify_signup_screen(screen) != signup.SCREEN_UNKNOWN:
            log("info", "instagram is in front after %d attempt(s)", attempt)
            return True
        log("info", "instagram is not in front yet (%d/%d); on screen: %r",
            attempt, attempts, screen[:120])
    log("warning", "instagram would not come to the front after %d attempts",
        attempts)
    return False


def load_assignment(profile_name: str, path: Path | None = None) -> dict:
    path = path or ASSIGNMENTS
    if not path.exists():
        raise SystemExit(
            f"[stop] no mailbox assignments at {path}. Reserve some first:\n"
            f"    python -m adb_bot.automation.signup_mailboxes --take 3")
    data = json.loads(path.read_text())
    if profile_name not in data:
        raise SystemExit(
            f"[stop] no mailbox assigned to {profile_name!r}. "
            f"Assigned: {sorted(data)}")
    return data[profile_name]


def run_phone(profile_item, box, host, adb_client, args, logger) -> dict:
    profile_id = str(profile_item.get("id"))
    name = str(profile_item.get("serial_name") or profile_id)
    # The join key everywhere else in this codebase is the first word of the
    # profile name (ONBOARDING_A_MODEL.md) -- "Cloe new 1" -> "Cloe" -- so
    # reusing it here needs no extra field threaded through from the caller.
    model = name.split()[0] if name and not name == profile_id else ""
    identity = make_identity(model=model)
    if box is not None:
        # `run_signup` picks its chain off `identity.email`: an address takes
        # the "sign up with email" hatch, no address takes the mobile-number
        # screen Instagram offers first. Leaving it unset is how the SMS
        # fallback is selected.
        identity.email = box["address"]
        identity.email_password = box.get("password", "")

    out = {"profile": name, "id": profile_id,
           "email": box["address"] if box else "",
           "username": identity.username, "steps": {},
           # The object itself, not just its handle: a caller that has to write
           # the account somewhere else afterwards -- the Geelark remark, the
           # mailbox claim -- needs the password and full name too, and
           # rebuilding an identity from its username would invent a new one.
           "identity": identity}

    print(f"\n{'=' * 68}\n{name} ({profile_id})")
    print(f"  mailbox  {box['address'] if box else '(none -- SMS)'}")
    print(f"  identity {identity.summary()}")
    if not args.apply:
        out["status"] = "dry-run"
        print("  DRY RUN -- nothing launched, no account created")
        return out

    if not locks.acquire(profile_id, owner="signup-phone"):
        out["status"] = "busy"
        return out

    # Before the phone is touched: an account whose password only ever existed
    # in memory is the state sixteen fleet profiles are already in.
    record_account(profile_id, name, identity, status="attempting")

    started = time.monotonic()
    try:
        profile = host.launch(profile_id, logger)
        if not profile:
            out["status"] = "not-ready"
            return out

        # Eight, not five. A freshly launched phone answers `adb connect` well
        # before it leaves `offline`, and five attempts is 50 seconds of
        # backoff: on 2026-08-17 one run got in on the fifth and the next used
        # all five and gave up. Eight is a little over two minutes, which is
        # cheap against a launch, and costs nothing when the phone is healthy
        # because the ladder stops at the first success.
        target = connect_with_retries(adb_client, profile, logger, profile_id,
                                      max_attempts=8, retry_delay_seconds=5)
        if not target:
            out["status"] = "unreachable"
            return out
        out["target"] = target
        driver_target = target

        driver = AdbSignupDriver(target, adb_client, logger=logger, act=True,
                                 screenshots=args.screenshots)

        # --- 1. the mailbox ---------------------------------------------------
        # Skipped entirely when there is no mailbox to sign in: the account
        # then verifies by SMS instead. That is the worse account -- a rented
        # number is released and nobody can ever recover it -- so it is a
        # deliberate fallback for when the mailbox pool cannot deliver, never
        # the default.
        if box is not None:
            verdict = google_signin.sign_in(
                driver, adb_client, target, box["address"], box["password"],
                box["totp_secret"], logger=logger)
            out["steps"]["google_signin"] = verdict
            print(f"  google sign-in: {verdict} "
                  f"({int(time.monotonic() - started)}s)")
            if verdict not in (google_signin.RESULT_SIGNED_IN,
                               google_signin.RESULT_ALREADY):
                out["status"] = f"mailbox-{verdict}"
                return out

        # --- 2. Instagram, and Gmail to read its code out of -------------------
        # Gmail is *not* preinstalled on these phones -- `Blank caio 2` spent a
        # whole launch on 2026-08-17 waiting for a code from an app that was
        # not there. Instagram first: it is the one the run cannot proceed
        # without, and the phone's life is finite. With no mailbox there is
        # nothing to read a code out of, so Gmail is not worth the minutes.
        wanted = [(INSTAGRAM_PACKAGE, "instagram")]
        if box is not None:
            wanted.append((GMAIL_PACKAGE, "gmail"))
        for package, what in wanted:
            verdict = play_install.install(driver, adb_client, target,
                                           package, logger=logger)
            out["steps"][f"install-{what}"] = verdict
            print(f"  {what} install: {verdict} "
                  f"({int(time.monotonic() - started)}s)")
            if verdict not in (play_install.RESULT_INSTALLED,
                               play_install.RESULT_ALREADY):
                out["status"] = f"install-{what}-{verdict}"
                return out

        # --- 3. the account ---------------------------------------------------
        if not open_instagram(driver, adb_client, target, logger=logger):
            # Deliberately its own status, and deliberately not one of the
            # signup results: Instagram never opened, so nothing was typed
            # anywhere and the phone is as unused as before the launch.
            out["status"] = "app-instagram-would-not-open"
            return out

        if box is not None:
            mailbox = PhoneMailbox(target, adb_client, box["address"],
                                   logger=logger, driver=driver)
            router = None
        else:
            # Built here, not earlier: a router that is never asked for a
            # number costs nothing, but building one proves the keys are
            # present before a phone has been launched on the assumption.
            from adb_bot.clients.sms.router import build_router

            mailbox = None
            router = build_router(logger=logger)
        result = signup.run_signup(driver, router, identity, logger=logger,
                                   mailbox=mailbox,
                                   country=getattr(args, "country", None))
        out["steps"]["signup"] = result.status
        out["status"] = result.status
        out["detail"] = result.detail[:300]
        # Re-read the handle: the signup changes it when Instagram says the
        # first choice is taken, and `out` was filled in before the phone was
        # touched. Reporting the intended handle for a live account is how
        # somebody goes looking for @lena.berg and finds nothing, while
        # @lena.berg1968 sits there unclaimed.
        out["username"] = identity.username
        print(f"  signup: {result.status}  {result.detail[:160]}")

        record_account(profile_id, name, identity, status=result.status)
        if result.ok:
            where = box["address"] if box else "a rented number"
            print(f"  CREATED @{identity.username} on {where}")

        # --- 4. the checkpoint ------------------------------------------------
        # Instagram holds a brand-new account behind "confirm you're human"
        # within seconds of creating it, so an account that stops here is real
        # but unusable. This has to happen in the *same* launch: a restarted
        # Instagram comes back to "Join Instagram" and the account cannot be
        # picked up again from a later run.
        if result.status == signup.RESULT_CREATED_UNVERIFIED and args.verify:
            left = seconds_left_for_verification(time.monotonic() - started)
            if left is None:
                spent = int(time.monotonic() - started)
                out["steps"]["verification"] = "skipped-no-time"
                print(f"  verification: skipped, {spent}s of the phone already "
                      f"spent and fewer than {MIN_VERIFY_SECONDS}s left")
                return out
            verdict = verify_account(profile_id, name, identity, driver_target,
                                     adb_client, args, logger, seconds=left)
            out["steps"]["verification"] = verdict["verification"]
            out.update(verdict)
        return out
    except Exception as exc:
        logger.warning("signup_phone: %s failed (%s)", name, exc)
        out["status"] = "error"
        out["detail"] = str(exc)[:300]
        return out
    finally:
        out["elapsed"] = int(time.monotonic() - started)
        host.shutdown(profile_id, logger)
        locks.release(profile_id)


def seconds_left_for_verification(elapsed: float) -> float | None:
    """How long verification may run, or None if it must not start.

    Not a fixed budget: what is left of the phone. `run_verification` defaults
    to fifteen minutes, which is longer than these phones live, so left alone it
    would still be renting numbers after the device had gone.

    None rather than a small number, because "start and die" is the expensive
    outcome -- a rented number costs money and 45 seconds before it is even
    usable, and one abandoned mid-chain tells nobody it was wasted. An account
    left at its checkpoint can still be verified later; a burned number cannot
    be got back.
    """
    left = PHONE_LIFE_SECONDS - elapsed
    return left if left >= MIN_VERIFY_SECONDS else None


def _download_model_photo(name: str, logger) -> str:
    """A local copy of the model's own photo, for the verification photo
    challenge (`AdbChallengeDriver.upload_photo`). "" if there is no model
    to derive, no picture on her Geelark tag, or the download fails --
    callers must treat that as "none available", never invent a path.
    """
    model = name.split()[0] if name else ""
    if not model:
        return ""
    try:
        import requests

        from adb_bot.clients.geelark import GeelarkTransport, library

        url = library.picture_url_for_tag(model, transport=GeelarkTransport())
        if not url:
            return ""
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        suffix = Path(url).suffix or ".jpg"
        dest = Path(tempfile.gettempdir()) / f"verify-photo-{model}{suffix}"
        dest.write_bytes(response.content)
        return str(dest)
    except Exception as exc:
        if logger:
            logger.warning("signup_phone: could not fetch %s's photo for "
                           "the verification challenge (%s)", model, exc)
        return ""


def verify_account(profile_id: str, name: str, identity, target: str,
                   adb_client, args, logger, seconds: float) -> dict:
    """Clear the checkpoint Instagram just put the new account behind.

    Separated from `run_phone` only so the budget arithmetic above stays
    readable. Returns the keys to merge into that run's result.

    `seconds` is what is left of the phone, not a fixed budget:
    `run_verification` defaults to fifteen minutes, which is longer than these
    phones live, so left alone it would still be renting numbers after the
    device had gone.
    """
    from adb_bot.clients.sms.base import DEFAULT_COUNTRY
    from adb_bot.clients.sms.router import build_router

    out: dict = {}
    country = getattr(args, "country", None) or DEFAULT_COUNTRY
    print(f"  verification: starting, {int(seconds)}s of phone left "
          f"(numbers cost money)")

    # Android's own dialogs sit on top of whatever Instagram is showing, and
    # the verification loop has no idea what they are: on 2026-08-21 an account
    # behind "confirm you're human" was read as `needs_human` because
    # "allow instagram to send you notifications?" was in front of it. The
    # challenge was never seen, and the report blamed the account.
    from adb_bot.automation.flows import interruptions

    try:
        interruptions.handle_permission_prompts(target, adb_client,
                                                logger=logger, flow="signup")
    except Exception as exc:
        logger.warning("signup_phone: could not clear permission prompts (%s)",
                       exc)
    challenge_driver = AdbChallengeDriver(
        target, adb_client, logger=logger, act=True,
        screenshots=args.screenshots,
        photo_source_path=_download_model_photo(name, logger))
    verdict = verification.run_verification(
        challenge_driver, build_router(logger=logger), logger=logger,
        country=country, max_seconds=seconds)

    out["verification"] = verdict.status
    out["verification_detail"] = verdict.detail[:300]
    out["numbers_used"] = verdict.numbers_used
    print(f"  verification: {verdict.status}  {verdict.detail[:120]}")

    # The account file is the only record of this, and "created but held" and
    # "created and usable" are different things to whoever reads it next.
    if verdict.ok:
        record_account(profile_id, name, identity, status=signup.RESULT_CREATED)
        out["status"] = signup.RESULT_CREATED
        print(f"  VERIFIED @{identity.username} -- usable")
    else:
        record_account(profile_id, name, identity,
                       status=f"created_unverified-{verdict.status}")
        out["status"] = signup.RESULT_CREATED_UNVERIFIED
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="One blank phone to one Instagram account, in one launch.")
    parser.add_argument("--mlx-token")
    parser.add_argument("--profile", action="append", required=True,
                        help="MLX profile name; repeat for several")
    parser.add_argument("--assignments", type=Path, default=None,
                        help=f"mailbox assignments (default {ASSIGNMENTS})")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="stop at the checkpoint instead of clearing it. "
                             "Verification rents SMS numbers, which cost money")
    parser.add_argument("--country", default=None,
                        help="country to rent verification numbers from")
    parser.add_argument("--readiness-attempts", type=int, default=10)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token(args.mlx_token)
    items = _profiles(token)
    clients = build_mlx_clients(token)
    adb_client = ADBClient()

    results = []
    for wanted in args.profile:
        item = _find_profile(items, wanted)
        if item is None:
            print(f"[skip] no MLX profile named {wanted!r}", file=sys.stderr)
            continue
        box = load_assignment(str(item.get("serial_name")), args.assignments)
        results.append(run_phone(item, box, MlxHost(clients, args), adb_client,
                                 args, logger))

    print(f"\n{'=' * 68}")
    for r in results:
        print(f"  {r['profile']:16} {r.get('status', '?'):22} "
              f"@{r.get('username', '')}  {r.get('email', '')}")
    made = [r for r in results if r.get("status") == signup.RESULT_CREATED]
    print(f"\n{len(made)} account(s) created of {len(results)} attempted")
    return 0 if made or not args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
