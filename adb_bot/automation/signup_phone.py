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
from adb_bot.automation import mailbox_robot_check
from adb_bot.automation.signup_identity import make_identity, record_account
from adb_bot.automation.verification_probe import (
    _find_profile, _profiles, _resolve_token,
)
from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb
from adb_bot.clients.adb import ADBClient
from adb_bot.clients.telegram import TelegramNotifier
from adb_bot.core import locks
from adb_bot.core.logger import get_logger

INSTAGRAM_PACKAGE = "com.instagram.android"

# How long one of these phones is worth planning around. MultiLogin phones die
# by themselves at about fifteen minutes with no exit line anywhere, and there
# is no resuming a half-made account, so everything a run intends to do has to
# fit inside this.
PHONE_LIFE_SECONDS = 13 * 60

# Geelark phones do not die on their own the way MLX ones do -- confirmed
# live across tonight's 2026-08-25 GeeLark rounds, several ran 20-50 minutes
# with no sign of an imposed ceiling. Using the MLX-calibrated 13-minute
# budget here abandoned a genuinely finished signup right before its last
# step: `blank_4059` (ruhu56898@gmail.com) reached `created_unverified` at
# 729s, 51s short of the 150s verification needs, and the phone was shut
# down -- Instagram cannot resume a created-but-unverified account after a
# restart, so that account is gone. Still a real bound, not unlimited (a
# GeeLark phone bills per minute), just one grounded in what these phones
# actually do instead of an assumption inherited from a different host.
PHONE_LIFE_SECONDS_GEELARK = 25 * 60

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
            launcher_client=self.clients.launcher,
            status_client=self.clients.status)

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

    The phone's own proxy port is leased exclusively for its whole life
    (`launch` acquires, `shutdown` releases) -- confirmed live 2026-08-23:
    this pipeline was starting phones on their statically assigned proxy
    with no check that another phone was already running on the same port,
    which the four-modem pool cannot tell apart as two devices.
    `adb_bot.clients.geelark.session` already solved this for its own
    single-phone manual path; this reuses the same `proxy_pool`.

    Callers that already have the phone's `proxy` dict (both batch
    pipelines do, from the same `list_phones()` call that built their
    worklist) should pass `proxy_port` to skip a second lookup. Callers
    that do not -- including any future one-off script -- still get the
    same protection: leaving `proxy_port` unset makes `launch` resolve it
    itself before starting anything, rather than silently skipping the
    lease. There is deliberately no way to construct a `GeelarkHost` that
    starts a phone without going through this.
    """

    def __init__(self, transport=None, args=None, proxy_port=None,
                proxy_identity=None) -> None:
        from adb_bot.clients.geelark import GeelarkTransport

        self.transport = transport or GeelarkTransport()
        self.args = args
        self.proxy_port = proxy_port
        self.proxy_identity = proxy_identity
        self._lease = None

    def _resolve_proxy(self, profile_id: str, logger):
        """(port, identity) already bound to `profile_id`, or (None, "") if it
        truly has none. One `list_phones()` call -- the fleet fits in a few
        pages, and this only runs when the caller has not already handed the
        port in.

        `identity` is the proxy's own username, e.g. Multilogin's mobile
        relay carries a distinct `sid-` per phone in it even though every
        phone reports the identical `gate.multilogin.com:1080` -- see
        `proxy_pool._lease_path`. Geelark's own rotating pool has no per-port
        username to speak of, so this is blank there and leasing falls back
        to the port alone, unchanged.
        """
        from adb_bot.clients.geelark.phones import GeelarkPhoneClient

        try:
            for row in GeelarkPhoneClient(self.transport).list_phones():
                if str(row.get("id")) == str(profile_id):
                    proxy = row.get("proxy") or {}
                    port = proxy.get("port")
                    identity = str(proxy.get("username") or "")
                    return (int(port) if port else None), identity
        except Exception as exc:
            if logger:
                logger.warning(
                    "signup_phone: could not look up %s's proxy port (%s); "
                    "launching without a lease", profile_id, exc)
        return None, ""

    def launch(self, profile_id: str, logger):
        from adb_bot.clients.geelark import prepare_geelark_profile_for_adb
        from adb_bot.clients.geelark import proxy_pool

        port = self.proxy_port
        identity = self.proxy_identity or ""
        if port is None:
            port, identity = self._resolve_proxy(profile_id, logger)

        if port is not None:
            self._lease = proxy_pool.acquire_proxy(
                [port], owner=str(profile_id), wait_seconds=60.0,
                identity=identity)
            if self._lease is None:
                logger.warning(
                    "signup_phone: proxy port %s is already leased by "
                    "another running phone; not starting %s on it",
                    port, profile_id)
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


def _tags_for_outcome(out: dict) -> list[str]:
    """Human-readable tag names describing where a run landed.

    So a person looking at the Geelark dashboard can tell a phone's state
    at a glance -- "Gmail connected", "IG connected", which dead end it hit
    -- without opening a log. Requested live 2026-08-26 after a night of
    round-cycling where nobody but this session could tell which of the
    many "Blank N" phones were actually worth a manual look.
    """
    steps = out.get("steps", {})
    tags: list[str] = []

    if (steps.get("google_signin") in ("signed_in", "already_signed_in")
            and steps.get("install-gmail") in ("installed", "already_installed")):
        tags.append("Gmail connected")

    signup_result = steps.get("signup")
    if signup_result in ("created", "created_unverified"):
        tags.append("IG connected")

    verification = steps.get("verification")
    if verification and verification != "skipped-no-time":
        tags.append(f"verification: {verification}")

    google_signin = steps.get("google_signin")
    if google_signin in ("google_robot_check", "device_verification",
                         "account_not_found", "wrong_password", "stuck"):
        tags.append(f"google: {google_signin.replace('_', ' ')}")

    install_gmail = steps.get("install-gmail")
    if install_gmail in ("no_install_button", "reauth_required", "no_network"):
        tags.append(f"gmail install: {install_gmail.replace('_', ' ')}")

    if signup_result in ("stuck", "unknown_screen"):
        tags.append(f"signup: {signup_result}")

    if not tags:
        tags.append(str(out.get("status", "unclassified")))
    return tags


def _apply_status_tags(profile_id: str, host, out: dict, logger) -> None:
    """Tag a Geelark phone with `_tags_for_outcome(out)`, merged with
    whatever tags it already carries -- `tagIDs` on `/phone/detail/update`
    *replaces* a phone's tags rather than adding to them, and a phone's own
    record only ever carries tag *names*, never ids (`clients/geelark/
    tags.py`), so the merge has to go through a name->id lookup.

    Geelark only (a no-op for MlxHost), and only ever called after the phone
    has stopped -- `/phone/detail/update` refuses writes while a phone is
    starting.
    """
    if not isinstance(host, GeelarkHost):
        return
    try:
        from adb_bot.clients.geelark.phones import GeelarkPhoneClient
        from adb_bot.clients.geelark.tags import GeelarkTagClient

        phone_client = GeelarkPhoneClient(host.transport)
        tag_client = GeelarkTagClient(host.transport)

        names = _tags_for_outcome(out)
        by_name = tag_client.tag_ids_by_name()
        for name in names:
            if name not in by_name:
                tag_client.ensure_tag(name)
        by_name = tag_client.tag_ids_by_name(refresh=True)
        new_ids = [by_name[n] for n in names if n in by_name]
        if not new_ids:
            return

        existing_names: list[str] = []
        for row in phone_client.list_phones():
            if str(row.get("id")) == str(profile_id):
                existing_names = list(row.get("tags") or [])
                break
        existing_ids = [by_name[n] for n in existing_names if n in by_name]

        merged = list(dict.fromkeys(existing_ids + new_ids))
        phone_client.update_phone(profile_id, tag_ids=merged)
        logger.info("signup_phone: tagged %s with %s", profile_id, names)
    except Exception as exc:
        logger.warning("signup_phone: tagging %s failed (%s)", profile_id, exc)


def _write_mailbox_remark(profile_id: str, box: dict, logger) -> None:
    """Stamp the mailbox's address and password into the MLX profile's own
    Notes field, so a person looking the profile up in MultiLogin can find its
    login without going back to mailboxes.json. Written unconditionally at the
    start of every real run, not only on success -- a profile that never
    finishes signing in still needs its assigned mailbox to be findable.
    Best effort: any failure is logged and the run carries on regardless.
    """
    import os

    import requests

    token = os.environ.get("MULTILOGIN_TOKEN", "")
    if not token or not box:
        return
    remark = f"MAIL:{box.get('address', '')} | PW:{box.get('password', '')}"
    try:
        response = requests.put(
            "https://api.multilogin.com/mobile_profiles/phone/update",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            timeout=30, json={"id": profile_id, "remark": remark})
        if not response.ok:
            logger.warning("Could not write mailbox notes for %s: HTTP %s", profile_id, response.status_code)
    except Exception as exc:
        logger.warning("Could not write mailbox notes for %s: %s", profile_id, exc)


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
           # Flipped True only for a robot check: the phone stays open for a
           # human to clear it live, everything else still shuts down as before.
           "keep_open": False,
           # The object itself, not just its handle: a caller that has to write
           # the account somewhere else afterwards -- the Geelark remark, the
           # mailbox claim -- needs the password and full name too, and
           # rebuilding an identity from its username would invent a new one.
           "identity": identity}

    print(f"\n{'=' * 68}\n{name} ({profile_id})")
    print(f"  mailbox  {box['address'] if box else '(none -- SMS)'}")
    print(f"  identity {identity.summary()}")
    if args.apply and box:
        _write_mailbox_remark(profile_id, box, logger)
    if not args.apply:
        out["status"] = "dry-run"
        print("  DRY RUN -- nothing launched, no account created")
        return out

    # A retry must stay on the same proxy, not rotate onto a fresh one every
    # time (2026-08-24): the same mailboxes hit robot_check on every platform
    # tried that day, one new profile/proxy per round -- Google's own risk
    # scoring reads a rotating set of IPs on one account as suspicious by
    # itself. Refusing here costs nothing (no phone touched yet) and stops a
    # caller from burning a fresh proxy on an address that needs to sit out
    # its cooldown or come back to the one proxy it is pinned to.
    if box is not None:
        address = box["address"]
        cooldown = mailbox_robot_check.ready_at(address)
        if cooldown:
            out["status"] = "mailbox-cooldown"
            out["cooldown_until"] = cooldown
            print(f"  {address} is in robot-check cooldown until "
                  f"{cooldown:.0f} (unix) -- skipping")
            return out
        if not mailbox_robot_check.may_use_profile(address, profile_id):
            pinned = mailbox_robot_check.pinned_profile(address)
            out["status"] = "mailbox-wrong-profile"
            out["pinned_profile"] = pinned
            print(f"  {address} is pinned to {pinned} after its rotation "
                  f"budget was spent -- retry there, not on {profile_id}")
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
            # `sign_in_with_retries`, not the single-shot `sign_in`: an
            # unnamed screen or a stuck app-state glitch is retried (force-
            # stopping Play Store/GMS first) up to
            # `google_signin.DEFAULT_SIGNIN_RETRIES` times before this run
            # gives up on the mailbox -- the retry path already existed and
            # was proven live (`oukroaicha@gmail.com`, 2026-08-23) but this
            # call site never used it, so every run here only ever got the
            # single un-retried attempt. Confirmed live 2026-08-25
            # (elizabethclarkncv773@gmail.com): a genuinely retryable
            # `unknown_screen` ended the whole run instead.
            verdict = google_signin.sign_in_with_retries(
                driver, adb_client, target, box["address"], box["password"],
                box["totp_secret"], logger=logger)
            out["steps"]["google_signin"] = verdict
            print(f"  google sign-in: {verdict} "
                  f"({int(time.monotonic() - started)}s)")
            if verdict in (google_signin.RESULT_SIGNED_IN,
                          google_signin.RESULT_ALREADY):
                # A working sign-in means this proxy is not burned -- wipe
                # any streak so the next attempt on this address is free to
                # pick whichever profile suits it, not pinned to this one.
                mailbox_robot_check.clear(box["address"])
            else:
                if verdict == google_signin.RESULT_DEVICE_VERIFICATION:
                    out["keep_open"] = True
                if verdict == google_signin.RESULT_ROBOT_CHECK:
                    # No automating past this -- the recaptcha lives inside
                    # Google's own account webview with no exposed sitekey
                    # (checked 2026-08-24), so a human has to tap the "I'm
                    # not a robot" checkbox. keep_open stops the finally
                    # block below from shutting the phone down, so whoever
                    # answers this can pick the run up mid-flow.
                    out["keep_open"] = True
                    state = mailbox_robot_check.record_robot_check(
                        box["address"], profile_id)
                    cooldown_note = (
                        f"\ncooldown until {state['cooldown_until']:.0f} "
                        f"(unix)" if state.get("cooldown_until") else "")
                    TelegramNotifier().send(
                        f"\U0001f916 robot check -- {box['address']} on "
                        f"{name} ({profile_id})\nPhone is still open, "
                        f"waiting at the 'Confirm you're not a robot' "
                        f"screen. Retry on this same profile, not a fresh "
                        f"one.{cooldown_note}", logger=logger)
                out["status"] = f"mailbox-{verdict}"
                return out

        # --- 2. Instagram, and Gmail to read its code out of -------------------
        # Gmail is *not* preinstalled on these phones -- `Blank caio 2` spent a
        # whole launch on 2026-08-17 waiting for a code from an app that was
        # not there. With no mailbox there is nothing to read a code out of,
        # so Gmail is not worth the minutes and Instagram is the one the run
        # cannot proceed without.
        #
        # With a mailbox, Gmail goes first instead (2026-08-23): the code
        # Instagram emails during signup has to have somewhere to land, so
        # Gmail needs to already be there by the time Instagram asks for it,
        # not installed afterward in a race against the email arriving.
        if box is not None:
            wanted = [(GMAIL_PACKAGE, "gmail"), (INSTAGRAM_PACKAGE, "instagram")]
        else:
            wanted = [(INSTAGRAM_PACKAGE, "instagram")]
        for package, what in wanted:
            verdict = play_install.install_with_retries(
                driver, adb_client, target, package, logger=logger)
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
            left = seconds_left_for_verification(time.monotonic() - started,
                                                 host=host)
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
        if out.get("keep_open"):
            # Lock held too, not just the phone left running: two people work
            # as root on this box, and a second automated pass grabbing this
            # profile mid-solve would undo whatever the human is doing on it.
            # It self-expires (locks.DEFAULT_TTL_SECONDS) if nobody gets to it.
            logger.info("signup_phone: leaving %s (%s) running and locked "
                        "for a human to clear the robot check", name, profile_id)
        else:
            host.shutdown(profile_id, logger)
            locks.release(profile_id)
            _apply_status_tags(profile_id, host, out, logger)


def seconds_left_for_verification(elapsed: float, host=None) -> float | None:
    """How long verification may run, or None if it must not start.

    Not a fixed budget: what is left of the phone. `run_verification` defaults
    to fifteen minutes, which is longer than an MLX phone lives, so left alone
    it would still be renting numbers after the device had gone.

    `host` picks which budget applies -- a `GeelarkHost` gets
    `PHONE_LIFE_SECONDS_GEELARK`, everything else (including no host passed,
    so existing MLX call sites keep their exact prior behaviour) gets the
    original MLX-calibrated `PHONE_LIFE_SECONDS`.

    None rather than a small number, because "start and die" is the expensive
    outcome -- a rented number costs money and 45 seconds before it is even
    usable, and one abandoned mid-chain tells nobody it was wasted. An account
    left at its checkpoint can still be verified later; a burned number cannot
    be got back.
    """
    budget = (PHONE_LIFE_SECONDS_GEELARK if isinstance(host, GeelarkHost)
             else PHONE_LIFE_SECONDS)
    left = budget - elapsed
    return left if left >= MIN_VERIFY_SECONDS else None


def _download_model_photo(name: str, logger) -> str:
    """A local copy of a real photo of the model, for the verification photo
    challenge (`AdbChallengeDriver.upload_photo`). "" if there is no model to
    derive or nothing usable was found anywhere -- callers must treat that as
    "none available", never invent a path.

    Two sources, tried in order:

    1. `_va_library_photo` -- the VAs' own curated `Models/<Model>/IG/`
       folder on Drive: real, deliberately-taken face photos, dozens per
       model. The right source whenever it is reachable -- "make sure your
       face can be clearly seen" is exactly what these were taken for, which
       a random video frame is not guaranteed to satisfy.
    2. `_video_frame_photo` -- a still pulled from her own raw footage, for
       a model the library has nothing configured for yet.

    Not Geelark's Material Library: this fleet runs on MultiLogin, which has
    no such library, and Geelark's `/material/tag/search` answered every live
    call with 40011 "only for paid user" (confirmed 2026-09-24, Nikki 26: the
    photo source was never reachable, on any account, so `upload_photo` had
    refused with "no picture source is configured" on every real run since it
    existed).
    """
    model = name.split()[0] if name else ""
    if not model:
        return ""
    photo = _va_library_photo(model, logger)
    if photo:
        return photo
    return _video_frame_photo(model, logger)


def _va_library_photo(model: str, logger) -> str:
    """A real face photo of `model`, downloaded from the VAs' own
    `Models/<Model>/IG/` Drive folder. "" if the root isn't configured, no
    matching model subfolder exists, or `IG/` has nothing image-shaped in it.

    Folder names here vary (`Kathi`, but `Lea OF`, `Nadine OF`) so the match
    is "starts with", case-insensitive -- same looseness `spoof_pipeline`
    already uses for the raw-video tree, for the same reason: these are typed
    by hand, not a fixed enum.
    """
    try:
        from adb_bot.clients.gdrive import DriveClient
        from adb_bot.config import settings

        root = settings.get_saved_model_photos_folder_id()
        key_json = settings.get_saved_google_service_account_json()
        if not root or not key_json:
            return ""

        client = DriveClient(service_account_json=key_json)
        model_folder = next(
            (f for f in client.list_subfolders(root)
             if f["name"].strip().lower().startswith(model.strip().lower())),
            None)
        if not model_folder:
            return ""

        ig_folder = next(
            (f for f in client.list_subfolders(model_folder["id"])
             if f["name"].strip().lower() == "ig"),
            None)
        if not ig_folder:
            return ""

        images = [f for f in client.list_files(ig_folder["id"])
                 if Path(f["name"]).suffix.lower() in
                 (".jpg", ".jpeg", ".png", ".heic", ".heif")]
        if not images:
            return ""
        # Prefer jpg/png -- guaranteed decodable by the picker; HEIC only if
        # that is all there is.
        preferred = [f for f in images
                    if Path(f["name"]).suffix.lower() in (".jpg", ".jpeg", ".png")]
        pool = preferred or images
        # Within that, prefer filenames that say this IS a face shot --
        # confirmed live 2026-09-24, nikki 22: three random picks in a row
        # from this folder were rejected by Instagram's own face-detection
        # ("we can't accept this photo... make sure it clearly shows your
        # face"), out of a folder that also holds candid/lifestyle content
        # shots never meant to be a clean solo headshot. A name like "Insta
        # Profil" is a person's own signal that a photo was taken *for* a
        # profile picture, which is exactly this screen's bar.
        face_hinted = [f for f in pool
                      if any(w in f["name"].lower()
                             for w in ("profil", "profile", "selfie", "headshot"))]
        pool = face_hinted or pool
        # Random, not the first match every time: even within the narrowed
        # pool a pick can still be rejected, so a fixed choice means a
        # rejected photo is retried forever, identically, on every future
        # attempt for this model; a random one gives each attempt real odds
        # of landing on one that passes.
        import random
        chosen = random.choice(pool)

        dest = Path(tempfile.gettempdir()) / f"verify-photo-{model}-{chosen['id'][:8]}{Path(chosen['name']).suffix}"
        client.download(chosen["id"], str(dest))
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)
        return ""
    except Exception as exc:
        if logger:
            logger.warning("signup_phone: could not fetch %s's library photo "
                           "for the verification challenge (%s)", model, exc)
        return ""


def _video_frame_photo(model: str, logger) -> str:
    """A local copy of a still frame from the model's own raw Drive footage,
    for the verification photo challenge -- the fallback when
    `_va_library_photo` finds nothing. "" if there is no footage for her or
    the extraction fails.

    Sourced from the same Google Drive raw-video folders `spoof_pipeline`
    already reads (`01_Raw_Videos/<Model>/...`).
    """
    try:
        import subprocess

        from adb_bot.automation import spoof_pipeline
        from adb_bot.config import settings

        source = spoof_pipeline.build_source(
            raw_root=settings.get_saved_raw_videos_dir(),
            drive_folder_id=settings.get_saved_drive_folder_id(),
            service_account_json=settings.get_saved_google_service_account_json(),
            logger=logger)
        if source is None:
            return ""

        by_model = source.list_by_model()
        videos = by_model.get(model)
        if not videos:
            # The Drive folder is named after the model's raw-footage alias
            # (e.g. "Nikki I DE"), not the model itself -- match on the
            # resolved name the same way spoof_pipeline does.
            videos = next(
                (v for folder_name, v in by_model.items()
                 if spoof_pipeline.resolve_model(folder_name).strip().lower()
                 == model.strip().lower()),
                None)
        if not videos:
            return ""

        video = videos[0]
        video_path = source.resolve(video)
        if not video_path:
            return ""
        try:
            dest = Path(tempfile.gettempdir()) / f"verify-photo-{model}.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-ss", "00:00:01.000",
                 "-frames:v", "1", str(dest)],
                capture_output=True, timeout=30, check=True)
            if dest.exists() and dest.stat().st_size > 0:
                return str(dest)
            return ""
        finally:
            source.release(video, video_path)
    except Exception as exc:
        if logger:
            logger.warning("signup_phone: could not extract %s's photo for "
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
        # Same reasoning as the Google robot-check case above: this account
        # cannot be picked up on a later launch -- a restarted Instagram
        # comes back to "Join Instagram" -- so shutting the phone down here
        # is what actually loses it, not the challenge itself. Confirmed
        # live 2026-08-26: blank_4059 (verification skipped for lack of
        # time) and blank.6258 (a photo challenge with no picture source
        # configured) were both real, freshly created accounts that the
        # `finally` block's shutdown made unrecoverable a few seconds later.
        out["keep_open"] = True
        TelegramNotifier().send(
            f"\U0001f4f8 verification needs a person -- @{identity.username} "
            f"on {name} ({profile_id})\n{verdict.status}: {verdict.detail[:200]}"
            f"\nPhone is still open, waiting -- this account cannot be "
            f"resumed once it closes.", logger=logger)
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
