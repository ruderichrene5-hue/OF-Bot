"""Leasing one of the shared proxies for an MLX profile.

Everything is faked here (no HTTP); the point is the *sequencing* -- lease
before writing the proxy, write before starting, release on any failure, and
the asymmetric release-on-stop rule -- mirroring `test_geelark_session.py`,
since this reuses the exact same lock directory Geelark's pool does.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adb_bot.automation import mlx_proxy_session as mps
from adb_bot.clients.geelark import proxy_pool
from adb_bot.core.models import Profile

ENV = {
    "ADBBOT_SHARED_PROXY_SERVER": "162.55.84.35",
    "ADBBOT_SHARED_PROXY_USERNAME": "u",
    "ADBBOT_SHARED_PROXY_PASSWORD": "p",
    "ADBBOT_SHARED_PROXY_PORTS": "54015,54018",
}

FAKE_PROFILE = Profile(id="p1", status="ready", ip="1.2.3.4", port="5555", pwd="pw")


class FakeLauncher:
    def __init__(self):
        self.started = []

    def start_profiles(self, ids):
        self.started.extend(ids)


class FakeShutdown:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def shutdown_profiles(self, ids):
        self.calls.extend(ids)
        if self.result:
            return {"data": {"success_amount": len(ids), "fail_amount": 0}}
        return {"data": {"success_amount": 0, "fail_amount": len(ids)}}


class FakeClients:
    def __init__(self, stop_result=True):
        self.api = object()
        self.adb_enable = object()
        self.launcher = FakeLauncher()
        self.shutdown = FakeShutdown(stop_result)


class SharedPoolProxiesTest(unittest.TestCase):
    def test_reads_server_and_expands_each_port(self):
        result = mps.shared_pool_proxies(ENV)
        self.assertEqual({p["port"] for p in result}, {54015, 54018})
        self.assertTrue(all(p["server"] == "162.55.84.35" for p in result))
        self.assertTrue(all(p["username"] == "u" for p in result))

    def test_missing_config_returns_empty_not_an_error(self):
        self.assertEqual(mps.shared_pool_proxies({}), [])

    def test_a_server_with_no_ports_returns_empty(self):
        env = dict(ENV, ADBBOT_SHARED_PROXY_PORTS="")
        self.assertEqual(mps.shared_pool_proxies(env), [])

    def test_no_geelark_dependency_is_imported(self):
        """The whole point of this config path: it must work with zero
        Geelark account/API involvement."""
        import inspect
        source = inspect.getsource(mps.shared_pool_proxies)
        self.assertNotIn("Geelark", source)
        self.assertNotIn("geelark", source)


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adbbot-mlx-proxy-session-")
        self.locks_dir = Path(self.tmp) / ".adb_bot" / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(proxy_pool, "lock_dir", lambda: self.locks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(proxy_pool.release_all_proxies)

    def _patch(self, env=ENV, ready_profile=FAKE_PROFILE,
              stop_result=True, set_proxy_calls=None):
        clients = FakeClients(stop_result=stop_result)
        set_proxy_calls = set_proxy_calls if set_proxy_calls is not None else []

        class FakeProxyWriter:
            def __init__(self, token):
                self.token = token

            def set_proxy(self, profile_id, server, port, username, password):
                set_proxy_calls.append((profile_id, server, port))

        patches = [
            patch.object(mps.os, "environ", env),
            patch.object(mps, "MultiloginProxyClient", FakeProxyWriter),
            patch.object(mps, "build_mlx_clients", lambda token: clients),
            patch.object(mps, "prepare_profile_for_adb",
                        lambda *a, **k: ready_profile),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return clients, set_proxy_calls


class StartSessionTest(SessionTestCase):
    def test_happy_path_leases_writes_the_proxy_and_returns_a_ready_session(self):
        clients, calls = self._patch()
        result = mps.start_session("p1", "tok")
        self.assertEqual(result.profile_id, "p1")
        self.assertEqual(result.profile, FAKE_PROFILE)
        self.assertIn(result.lease.port, (54015, 54018))
        self.assertEqual(calls, [("p1", "162.55.84.35", result.lease.port)])
        self.assertEqual(clients.launcher.started, ["p1"])
        # The lease is real and held -- nobody else can take this port.
        self.assertEqual(proxy_pool.held_ports(), [result.lease.port])

    def test_no_proxies_configured_raises(self):
        self._patch(env={})
        with self.assertRaises(mps.SessionError):
            mps.start_session("p1", "tok")

    def test_every_port_already_leased_refuses_to_start(self):
        self._patch()
        proxy_pool.acquire_proxy([54015], owner="someone-else")
        proxy_pool.acquire_proxy([54018], owner="someone-else-2")
        with self.assertRaises(mps.SessionError):
            mps.start_session("p1", "tok", wait_for_lease_seconds=0.1)

    def test_never_becoming_adb_ready_raises_and_releases_the_lease(self):
        self._patch(ready_profile=None)
        with self.assertRaises(mps.SessionError):
            mps.start_session("p1", "tok")
        self.assertEqual(proxy_pool.held_ports(), [])


class StopSessionTest(SessionTestCase):
    def test_a_confirmed_stop_releases_the_lease(self):
        self._patch(stop_result=True)
        result = mps.start_session("p1", "tok")
        self.assertEqual(len(proxy_pool.held_ports()), 1)
        stopped = mps.stop_session(result)
        self.assertTrue(stopped)
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_an_unconfirmed_stop_keeps_the_lease_held(self):
        """If MLX does not confirm the profile stopped, releasing the proxy
        would let a second phone share an exit IP with one that might still
        be live -- worse than a stuck lease that expires via its TTL."""
        self._patch(stop_result=False)
        result = mps.start_session("p1", "tok")
        stopped = mps.stop_session(result)
        self.assertFalse(stopped)
        self.assertEqual(len(proxy_pool.held_ports()), 1)


class MlxSharedProxyHostTest(SessionTestCase):
    """The `run_phone`-shaped host adapter: launch/shutdown must never raise,
    matching `MlxHost`/`GeelarkHost`'s own contract."""

    def test_launch_returns_the_profile_and_holds_the_lease(self):
        self._patch()
        host = mps.MlxSharedProxyHost("tok")
        profile = host.launch("p1", logger=None)
        self.assertEqual(profile, FAKE_PROFILE)
        self.assertEqual(len(proxy_pool.held_ports()), 1)

    def test_launch_failure_returns_none_instead_of_raising(self):
        """`run_phone` reads a None return as `status = "not-ready"` -- it
        does not catch exceptions out of `host.launch`."""
        self._patch(ready_profile=None)
        host = mps.MlxSharedProxyHost("tok")
        profile = host.launch("p1", logger=None)
        self.assertIsNone(profile)
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_shutdown_after_a_failed_launch_is_a_safe_no_op(self):
        self._patch(ready_profile=None)
        host = mps.MlxSharedProxyHost("tok")
        host.launch("p1", logger=None)
        host.shutdown("p1", logger=None)  # must not raise

    def test_shutdown_releases_the_lease_it_holds(self):
        self._patch(stop_result=True)
        host = mps.MlxSharedProxyHost("tok")
        host.launch("p1", logger=None)
        self.assertEqual(len(proxy_pool.held_ports()), 1)
        host.shutdown("p1", logger=None)
        self.assertEqual(proxy_pool.held_ports(), [])
