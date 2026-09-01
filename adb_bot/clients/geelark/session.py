"""One Geelark phone session, wired through the proxy pool.

The manual seam for testing a single phone end to end: lease its proxy
exclusively, rotate and verify a fresh exit IP, then bring the phone up over
ADB the same way `prepare_geelark_profile_for_adb` always has. Nothing here
is scheduled -- it exists so a person can run one test profile through the
whole chain (proxy -> boot -> ADB) and then check what only a human can check
from inside the phone: a DNS-leak test, whether an Instagram code arrives.

**Deliberately narrow.** This leases and rotates the ONE proxy port already
bound to the phone (read from Geelark's own `/phone/list`), not "any free
port out of four". Rotating a modem changes its exit *address*; it does not
move a phone onto a different port -- only `GeelarkPhoneClient.update_phone
(proxy_id=...)` does that, and Geelark's own docs warn against calling it
while a phone is starting. So reassigning phones across proxies on the fly is
a separate, larger feature this does not attempt. What this guarantees is the
one invariant that actually matters for a shared four-modem pool: two phones
that happen to share a physical port never run on it at the same time, and
two starts on that port a few seconds apart never trip the vendor's rotation
cooldown.

**Why this is a live session held open by one process, not two CLI calls.**
The proxy lease is a file tagged with the holding process's pid, reclaimed
automatically once that pid is gone -- exactly what you want for a crashed
loop, and exactly wrong for "start now, stop from a separate command later":
the starting process would exit immediately, the lease would look abandoned,
and the next phone sharing that port could be handed a proxy that this one is
still actively using. So `start_session` and `stop_session` are meant to
bracket one long-lived call (see `cli.py`'s `session` command), not two
independent invocations.
"""

from __future__ import annotations

from dataclasses import dataclass

from adb_bot.core.models import Profile

from . import proxy_pool
from .ip_rotation import ProxyRotator
from .phones import GeelarkPhoneClient
from .proxies import GeelarkProxyClient
from .readiness import prepare_geelark_profile_for_adb, release_geelark_phone
from .transport import GeelarkTransport


class SessionError(RuntimeError):
    pass


@dataclass
class GeelarkSession:
    """A started, ADB-ready phone plus the proxy lease that must outlive it."""

    profile: Profile
    lease: object                 # proxy_pool.ProxyLease
    rotation: dict
    phone_id: str
    transport: GeelarkTransport


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message, *args)


def phone_proxy_port(phone: dict) -> int | None:
    """The SOCKS5 port already bound to this phone's proxy, or None if it has
    none. Geelark's `/phone/list` nests it under `proxy`."""
    proxy = phone.get("proxy") or {}
    port = proxy.get("port")
    return int(port) if port else None


def _find_phone(phones: GeelarkPhoneClient, phone_id: str) -> dict:
    for row in phones.list_phones():
        if str(row.get("id")) == str(phone_id):
            return row
    raise SessionError(f"no Geelark phone with id {phone_id}")


def start_session(phone_id: str, transport: GeelarkTransport | None = None,
                  logger=None, *, wait_for_lease_seconds: float = 60.0,
                  cooldown_seconds: int = proxy_pool.ROTATE_COOLDOWN_SECONDS,
                  owner: str = "") -> GeelarkSession:
    """Lease this phone's proxy, rotate + verify it, then bring the phone up.

    Raises `SessionError` at whichever step fails, rather than returning None
    like the lower-level pieces do: this is a manual, one-off entry point, so
    the person running it wants to see exactly what went wrong. On any
    failure after the lease is taken, the lease is released before raising --
    a failed attempt must not strand a proxy nobody else can use.

    The caller must eventually pass the result to `stop_session`, normally in
    a `finally` -- see the module docstring for why this has to be one
    long-lived call rather than a separate start/stop pair of invocations.
    """
    transport = transport or GeelarkTransport()
    phones = GeelarkPhoneClient(transport)
    phone = _find_phone(phones, phone_id)

    port = phone_proxy_port(phone)
    if port is None:
        raise SessionError(
            f"{phone.get('serialName')} ({phone_id}) has no proxy assigned; "
            "cannot lease or rotate one that doesn't exist")

    rotator = ProxyRotator(GeelarkProxyClient(transport).list_proxies())
    if port not in rotator.rotatable_ports():
        raise SessionError(
            f"port {port} has no GEELARK_PROXY_REBOOT_URLS entry; cannot "
            "rotate it, so this session refuses to start on an unrotatable "
            "proxy rather than silently skip the rotation step")

    lease = proxy_pool.acquire_proxy([port], owner=owner or str(phone_id),
                                     wait_seconds=wait_for_lease_seconds)
    if lease is None:
        raise SessionError(
            f"port {port} is already leased by another running phone; wait "
            "for it to finish or raise wait_for_lease_seconds")

    try:
        proxy_pool.wait_for_cooldown(port, cooldown_seconds)
        rotation = rotator.rotate_until_changed(port)
        proxy_pool.record_rotation(port)

        if not rotation.get("accepted", True):
            raise SessionError(
                f"port {port} refused the rotation call: {rotation.get('detail')}")
        if not rotation.get("changed"):
            _emit(logger, "warning",
                  "port %s did not change IP (vendor accepted the call but "
                  "handed back the same address -- a real mobile-proxy "
                  "outcome, proceeding anyway)", port)

        profile = prepare_geelark_profile_for_adb(str(phone_id), transport, logger=logger)
        if profile is None:
            raise SessionError(f"{phone.get('serialName')} never became ADB-ready")

        return GeelarkSession(profile=profile, lease=lease, rotation=rotation,
                              phone_id=str(phone_id), transport=transport)
    except Exception:
        # Releasing the lease only frees our own internal lock -- it says
        # nothing to Geelark. If prepare_geelark_profile_for_adb got far
        # enough to actually start the phone (its default start_if_stopped
        # path) before ADB-readiness failed, that phone is left running on
        # Geelark's side with nothing here ever telling it to stop: no
        # caller ever gets a GeelarkSession back to pass to stop_session.
        # Found live 2026-09-01: two phones stuck open this way ate up the
        # account's whole 4-parallel-phone quota, refusing every other
        # launch with "balance not enough" until they were found and closed
        # by hand. Best-effort and safe to call even when the phone was
        # never started -- same call stop_session already makes.
        try:
            release_geelark_phone(str(phone_id), transport, logger=logger)
        except Exception as exc:
            _emit(logger, "warning",
                 "cleanup: could not confirm %s is stopped after a failed "
                 "start_session (%s)", phone_id, exc)
        proxy_pool.release_proxy(lease)
        raise


def stop_session(session: GeelarkSession, logger=None) -> bool:
    """Stop the phone and release its proxy lease. Always safe to call.

    The lease is released only if the phone actually confirmed stopping. If
    it did not, the lease is kept held rather than risk a second phone being
    handed the same exit IP while this one might still be running -- it will
    still expire via the lease TTL if never freed explicitly. Geelark's own
    batch endpoints have been seen reporting success while failing, but a
    reported *failure* here is exactly the case worth trusting cautiously.
    """
    stopped = release_geelark_phone(session.phone_id, session.transport, logger=logger)
    if stopped:
        proxy_pool.release_proxy(session.lease)
    else:
        _emit(logger, "warning",
              "Geelark phone %s did not confirm stopping; keeping its proxy "
              "lease held on port %s rather than risk a second phone sharing "
              "the same exit IP while it might still be running (expires "
              "after %ss if never freed explicitly)",
              session.phone_id, session.lease.port if session.lease else "?",
              proxy_pool.DEFAULT_LEASE_TTL_SECONDS)
    return stopped
