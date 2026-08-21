"""Bring a Geelark phone to the point where the bot's flows can drive it.

This is the Geelark sibling of `workflow.prepare_profile_for_adb`. It is a
separate function rather than a branch inside that one on purpose: the
MultiLogin path is what the whole fleet runs on today, and Geelark is under
evaluation. Nothing here can change how an MLX profile behaves.

It returns the same `Profile` the MultiLogin path returns, so everything above
this line -- `ADBClient.connect`, `authenticate`, `connect_with_retries`, and
every Instagram flow -- works unchanged. That is not a coincidence: MultiLogin's
mobile profiles *are* Geelark cloud phones underneath, which is why the on-device
auth shim (`adb shell glogin <pwd>`) is identical on both.

The order is fixed by the API and is not negotiable:

1. The phone must be **started**. A stopped phone answers `42002 phone is not
   running` to every ADB call, and enabling ADB on it does nothing.
2. Starting is slow -- about 45s to 90s observed -- and reports `status: 1`
   (starting) throughout. `status: 0` means started; the enum reads backwards.
3. ADB must be **switched on per phone**; it is off by default and stays off.
4. Enabling is **asynchronous**: the port is not readable for a few seconds
   after the call returns.
5. Even once Geelark reports an ip/port, the bridge may refuse the first
   `adb connect` -- a freshly created phone refused it and accepted ~10s later.
   That retry belongs to the caller (`connect_with_retries`), not here.
"""

from __future__ import annotations

import time

from adb_bot.core.models import Profile

from .adb_enable import GeelarkAdbEnableClient
from .api import GeelarkApiClient
from .launcher import GeelarkLauncherClient
from .phones import STATUS_STARTED, GeelarkPhoneClient, status_label
from .transport import GeelarkTransport

# Geelark's own reference client allows 180s for a phone to boot and 60s for the
# ADB bridge; these mirror that.
START_TIMEOUT_SECONDS = 180
START_POLL_SECONDS = 5
ADB_TIMEOUT_SECONDS = 60
ADB_POLL_SECONDS = 3


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message, *args)


def phone_is_started(phones: GeelarkPhoneClient, profile_id: str) -> bool:
    outcome = phones.phone_status([profile_id])
    for detail in outcome.success_details:
        if str(detail.get("id")) == str(profile_id):
            return int(detail.get("status", -1)) == STATUS_STARTED
    return False


def wait_until_started(
    profile_id: str,
    transport: GeelarkTransport,
    logger=None,
    timeout_seconds: int = START_TIMEOUT_SECONDS,
) -> bool:
    """Start the phone if needed and wait for it to report `started`."""
    phones = GeelarkPhoneClient(transport)

    if phone_is_started(phones, profile_id):
        _emit(logger, "info", "Geelark phone %s is already started", profile_id)
        return True

    outcome = GeelarkLauncherClient(transport).start_profiles([profile_id])
    if not outcome.ok:
        # The envelope says "success" even here -- only the details know.
        _emit(logger, "warning", "Geelark refused to start %s: %s",
              profile_id, outcome.failures())
        return False

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if phone_is_started(phones, profile_id):
            _emit(logger, "info", "Geelark phone %s reached 'started'", profile_id)
            return True
        time.sleep(START_POLL_SECONDS)

    _emit(logger, "warning",
          "Geelark phone %s never reached 'started' within %ss",
          profile_id, timeout_seconds)
    return False


def prepare_geelark_profile_for_adb(
    profile_id: str,
    transport: GeelarkTransport | None = None,
    logger=None,
    *,
    start_if_stopped: bool = True,
    timeout_seconds: int = ADB_TIMEOUT_SECONDS,
    caption: str | None = None,
    bio: str | None = None,
    picture: str | None = None,
    media_path: str | None = None,
    queue_id: str | None = None,
    target_handle: str | None = None,
) -> Profile | None:
    """Return a drivable `Profile` for a Geelark phone, or None.

    `start_if_stopped` defaults to True because a stopped phone cannot be made
    ready any other way -- but starting one begins billing minutes, so a caller
    that only wants to use phones already up should pass False.
    """
    transport = transport or GeelarkTransport()

    if start_if_stopped:
        if not wait_until_started(profile_id, transport, logger=logger):
            return None
    else:
        phones = GeelarkPhoneClient(transport)
        if not phone_is_started(phones, profile_id):
            _emit(logger, "info",
                  "Geelark phone %s is not started and start_if_stopped is off",
                  profile_id)
            return None

    enabler = GeelarkAdbEnableClient(transport)
    outcome = enabler.enable_adb([profile_id])
    if not outcome.ok:
        _emit(logger, "warning", "Could not enable ADB on Geelark phone %s: %s",
              profile_id, outcome.failures())
        return None

    api = GeelarkApiClient(transport)
    deadline = time.time() + timeout_seconds
    last_status = "unknown"

    while time.time() < deadline:
        profiles = GeelarkApiClient.parse_profiles(
            api.fetch_adb_credentials([profile_id]))
        profile = next((p for p in profiles if p.id == str(profile_id)), None)

        if profile is not None and profile.is_ready:
            _emit(logger, "info", "Geelark phone %s is ADB-ready at %s",
                  profile_id, profile.target)
            # Carry the per-run fields the flows expect, exactly as the
            # MultiLogin path does.
            profile.caption = caption
            profile.bio = bio
            profile.picture = picture
            profile.media_path = media_path
            profile.queue_id = queue_id
            profile.target_handle = target_handle
            return profile

        last_status = profile.status if profile else "missing"
        time.sleep(ADB_POLL_SECONDS)

    _emit(logger, "warning",
          "Geelark phone %s never became ADB-ready (last state: %s)",
          profile_id, last_status)
    return None


def release_geelark_phone(profile_id: str,
                          transport: GeelarkTransport | None = None,
                          logger=None) -> bool:
    """Stop the phone, freeing its parallel slot and ending per-minute billing.

    Worth calling in a `finally`: a phone left running after a run is pure
    spend, and it also holds a parallel slot that would otherwise let the next
    phone run for free.
    """
    from .shutdown import GeelarkShutdownClient

    transport = transport or GeelarkTransport()
    outcome = GeelarkShutdownClient(transport).shutdown_profiles([profile_id])
    if not outcome.ok:
        _emit(logger, "warning", "Could not stop Geelark phone %s: %s",
              profile_id, outcome.failures())
        return False
    _emit(logger, "info", "Stopped Geelark phone %s", profile_id)
    return True
