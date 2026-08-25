"""Lease one of the shared mobile proxies and point an MLX profile at it --
the MLX-side analogue of `adb_bot.clients.geelark.session`.

**Why this reuses Geelark's proxy pool locking rather than having its own.**
The shared physical proxies are one resource, not two -- CLAUDE.md is
explicit that a rotation link "cannot be used concurrently", and that was
true before either platform's code existed. If Geelark leased a port for a
live Cloe phone while this pool handed the same port to an MLX profile,
both devices would share one exit IP at the same time regardless of which
platform's lock file said what. So `start_session`/`stop_session` here call
straight into `adb_bot.clients.geelark.proxy_pool` for the file-lock
mechanics -- same lock directory, same lease files -- which is the only way
a lease taken by one platform is visible to the other. That is a real
cross-platform invariant, not code reuse for its own sake, and it costs
nothing: `proxy_pool` is pure local file locking, no Geelark API call, no
Geelark credentials.

**The proxies' server/credentials come from this bot's own config, not
Geelark's account.** `ADBBOT_SHARED_PROXY_*` in `/etc/adbbot/env` --
deliberately not `GeelarkProxyClient.list_proxies()`, even though that
endpoint happens to know about the same proxies too: this pool must keep
working with zero dependency on the Geelark account or its API being
reachable at all.

Unlike Geelark's phones, an MLX profile is not permanently bound to one of
these ports -- many profiles can round-robin through the same ones over
time, one profile at a time per port. `stop_session` therefore only
releases the *lease*; it deliberately leaves the profile's `proxy_config`
as last set -- whether that should later revert to a dedicated MLX proxy is
a decision for whoever calls this, not something guessed here.

This lives in `automation/`, not `clients/multilogin/`, because it composes
`prepare_profile_for_adb` (`automation/workflow.py`) the same way
`signup_phone.py`'s `MlxHost`/`GeelarkHost` already combine client-layer
pieces at this layer -- Geelark has an equivalent readiness helper inside
its own client package (`clients/geelark/readiness.py`); MLX does not, so
this cannot live purely in the client layer without inventing one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from adb_bot.automation.bootstrap import build_mlx_clients
from adb_bot.automation.workflow import prepare_profile_for_adb
from adb_bot.clients.geelark import proxy_pool
from adb_bot.clients.multilogin.proxy import MultiloginProxyClient
from adb_bot.core.models import Profile

# `ADBBOT_SHARED_PROXY_SERVER`, `_USERNAME`, `_PASSWORD`: one server and one
# credential pair shared by every port. `ADBBOT_SHARED_PROXY_PORTS`:
# comma-separated port list -- each port is its own exit IP on the same
# server, which is what actually makes them four separate proxies rather
# than one.
ENV_SERVER = "ADBBOT_SHARED_PROXY_SERVER"
ENV_USERNAME = "ADBBOT_SHARED_PROXY_USERNAME"
ENV_PASSWORD = "ADBBOT_SHARED_PROXY_PASSWORD"
ENV_PORTS = "ADBBOT_SHARED_PROXY_PORTS"


class SessionError(RuntimeError):
    pass


@dataclass
class MlxProxySession:
    """A started, ADB-ready MLX profile plus the shared proxy lease that
    must outlive it."""

    profile: Profile
    lease: object                 # proxy_pool.ProxyLease
    proxy: dict                   # {server, port, username, password}
    profile_id: str
    bearer_token: str


def shared_pool_proxies(env: dict | None = None) -> list[dict]:
    """The shared pool's proxies, from this bot's own config.

    Each entry: `{server, port, username, password}`. Empty if the four
    `ADBBOT_SHARED_PROXY_*` variables are not set -- callers must treat
    that the same as "nothing free", not crash. `env` defaults to
    `os.environ`; a caller (tests) can pass a plain dict instead.
    """
    env = os.environ if env is None else env
    server = str(env.get(ENV_SERVER, "")).strip()
    username = str(env.get(ENV_USERNAME, ""))
    password = str(env.get(ENV_PASSWORD, ""))
    ports_raw = str(env.get(ENV_PORTS, "")).strip()
    if not server or not ports_raw:
        return []
    ports = []
    for chunk in ports_raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ports.append(int(chunk))
    return [{"server": server, "port": port, "username": username,
             "password": password} for port in ports]


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message, *args)


def start_session(profile_id: str, bearer_token: str, *,
                  wait_for_lease_seconds: float = 60.0,
                  owner: str = "", logger=None,
                  readiness_attempts: int = 10,
                  readiness_wait: int = 15) -> MlxProxySession:
    """Lease a free port from the shared pool, point `profile_id` at it,
    then bring the profile up over ADB.

    Raises `SessionError` at whichever step fails -- this is a manual,
    one-off entry point, so the caller wants to see exactly what went
    wrong, not a silent None. The lease is released before raising on any
    failure after it was taken, so a failed attempt never strands a port
    nobody else can use.

    The caller must eventually pass the result to `stop_session`, normally
    in a `finally`.
    """
    proxies = shared_pool_proxies()
    if not proxies:
        raise SessionError(
            f"no shared proxies configured -- set {ENV_SERVER}, "
            f"{ENV_USERNAME}, {ENV_PASSWORD}, {ENV_PORTS} in /etc/adbbot/env")
    ports = [p["port"] for p in proxies]
    by_port = {p["port"]: p for p in proxies}

    lease = proxy_pool.acquire_proxy(ports, owner=owner or str(profile_id),
                                     wait_seconds=wait_for_lease_seconds)
    if lease is None:
        raise SessionError(
            f"every shared-pool port is in use right now ({ports}); wait "
            "for one to free up or raise wait_for_lease_seconds")

    proxy = by_port[lease.port]
    try:
        MultiloginProxyClient(bearer_token).set_proxy(
            profile_id, proxy["server"], proxy["port"],
            proxy.get("username", ""), proxy.get("password", ""))

        clients = build_mlx_clients(bearer_token)
        clients.launcher.start_profiles([profile_id])
        profile = prepare_profile_for_adb(
            profile_id, clients.api, clients.adb_enable, logger,
            max_attempts=readiness_attempts, wait_seconds=readiness_wait,
            launcher_client=clients.launcher)
        if profile is None:
            raise SessionError(f"{profile_id} never became ADB-ready on "
                               f"port {lease.port}")

        return MlxProxySession(profile=profile, lease=lease, proxy=proxy,
                               profile_id=str(profile_id),
                               bearer_token=bearer_token)
    except Exception:
        proxy_pool.release_proxy(lease)
        raise


def stop_session(session: MlxProxySession, logger=None) -> bool:
    """Stop the profile and release its shared-pool lease. Always safe to
    call.

    Mirrors `geelark.session.stop_session`: the lease is released only once
    shutdown is confirmed, so a phone that might still be running never has
    its port handed to someone else in the meantime -- it still expires via
    the lease TTL if this is never called.
    """
    clients = build_mlx_clients(session.bearer_token)
    try:
        outcome = (clients.shutdown.shutdown_profiles([session.profile_id])
                  .get("data") or {})
        stopped = (int(outcome.get("success_amount") or 0) >= 1
                  and int(outcome.get("fail_amount") or 0) == 0)
    except Exception as exc:
        _emit(logger, "warning", "shutdown failed for %s (%s)",
              session.profile_id, exc)
        stopped = False

    if stopped:
        proxy_pool.release_proxy(session.lease)
    else:
        _emit(logger, "warning",
              "MLX profile %s did not confirm stopping; keeping its shared "
              "proxy lease held on port %s rather than risk a second phone "
              "sharing the same exit IP while it might still be running",
              session.profile_id, session.lease.port if session.lease else "?")
    return stopped


class MlxSharedProxyHost:
    """A `host` for `signup_phone.run_phone` that launches an MLX profile on
    a leased shared-pool proxy, instead of whatever proxy the profile
    already carries.

    Matches the duck-typed interface `MlxHost`/`GeelarkHost` already give
    `run_phone`: `.launch(profile_id, logger) -> Profile | None` and
    `.shutdown(profile_id, logger) -> None`. `launch` never raises --
    `SessionError` is caught and logged, returning None instead, the same
    "not-ready" contract `MlxHost.launch` already has -- `run_phone` reads
    a None return as `status = "not-ready"` and stops, it does not expect
    an exception out of this call.

    One host instance is good for one profile's session: it holds the lease
    between `launch` and `shutdown`, the same way a `GeelarkHost` holds its
    proxy_pool lease.
    """

    def __init__(self, bearer_token: str, wait_for_lease_seconds: float = 60.0,
                readiness_attempts: int = 10, readiness_wait: int = 15) -> None:
        self.bearer_token = bearer_token
        self.wait_for_lease_seconds = wait_for_lease_seconds
        self.readiness_attempts = readiness_attempts
        self.readiness_wait = readiness_wait
        self._session: MlxProxySession | None = None

    def launch(self, profile_id: str, logger):
        try:
            self._session = start_session(
                profile_id, self.bearer_token,
                wait_for_lease_seconds=self.wait_for_lease_seconds,
                owner=str(profile_id), logger=logger,
                readiness_attempts=self.readiness_attempts,
                readiness_wait=self.readiness_wait)
        except SessionError as exc:
            _emit(logger, "warning",
                  "mlx_proxy_session: %s could not start on the shared "
                  "pool (%s)", profile_id, exc)
            self._session = None
            return None
        return self._session.profile

    def shutdown(self, profile_id: str, logger) -> None:
        if self._session is None:
            return
        stop_session(self._session, logger=logger)
        self._session = None
