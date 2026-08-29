"""Wiring one Geelark phone session through the proxy pool.

Everything Geelark-side is faked here (no HTTP); the point of these tests is
the *sequencing* -- lease before rotate, rotate before start, release on any
failure, and the asymmetric release-on-stop rule -- not the API client, which
is already covered elsewhere.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adb_bot.clients.geelark import proxy_pool, session
from adb_bot.core.models import Profile

PHONE_WITH_PROXY = {"id": "p1", "serialName": "Test Caio 1",
                    "proxy": {"server": "162.55.84.35", "port": 54015}}
PHONE_NO_PROXY = {"id": "p2", "serialName": "Test Caio 2", "proxy": {}}

PROXIES = [{"id": "1", "server": "162.55.84.35", "port": 54015,
            "username": "u", "password": "p"}]


class FakePhoneClient:
    def __init__(self, phones):
        self._phones = phones

    def list_phones(self):
        return self._phones


class FakeProxyClient:
    def __init__(self, proxies):
        self._proxies = proxies

    def list_proxies(self):
        return self._proxies


class FakeRotator:
    """Stands in for ProxyRotator: no HTTP, just scripted answers."""

    def __init__(self, proxies, rotatable=None, result=None):
        self.proxies = proxies
        self._rotatable = rotatable if rotatable is not None else [54015]
        self._result = result or {"port": 54015, "before": "1.1.1.1",
                                  "after": "2.2.2.2", "changed": True,
                                  "seconds": 22.0, "accepted": True, "detail": "OK"}
        self.rotate_calls = []

    def rotatable_ports(self):
        return list(self._rotatable)

    def rotate_until_changed(self, port):
        self.rotate_calls.append(port)
        return dict(self._result, port=port, retries=0)


FAKE_PROFILE = Profile(id="p1", status="ready", ip="1.2.3.4", port="5555", pwd="pw")


class SessionTestCase(unittest.TestCase):
    """Real proxy_pool file locking against a throwaway lock directory, with
    every Geelark network call faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adbbot-geelark-session-")
        self.locks_dir = Path(self.tmp) / ".adb_bot" / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(proxy_pool, "lock_dir", lambda: self.locks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(proxy_pool.release_all_proxies)

    def _patch_clients(self, phones, proxies=PROXIES, rotator=None,
                       ready_profile=FAKE_PROFILE, stop_result=True):
        rotator = rotator or FakeRotator(proxies)
        patches = [
            patch.object(session, "GeelarkPhoneClient",
                        lambda transport: FakePhoneClient(phones)),
            patch.object(session, "GeelarkProxyClient",
                        lambda transport: FakeProxyClient(proxies)),
            patch.object(session, "ProxyRotator", lambda proxies: rotator),
            patch.object(session, "prepare_geelark_profile_for_adb",
                        lambda *a, **k: ready_profile),
            patch.object(session, "release_geelark_phone",
                        lambda *a, **k: stop_result),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return rotator


class StartSessionTest(SessionTestCase):
    def test_happy_path_leases_rotates_and_returns_a_ready_session(self):
        rotator = self._patch_clients([PHONE_WITH_PROXY])
        result = session.start_session("p1", transport=object())
        self.assertEqual(result.phone_id, "p1")
        self.assertEqual(result.profile, FAKE_PROFILE)
        self.assertEqual(result.lease.port, 54015)
        self.assertTrue(result.rotation["changed"])
        self.assertEqual(rotator.rotate_calls, [54015])
        # The lease is real and held -- nobody else can take this port.
        self.assertEqual(proxy_pool.held_ports(), [54015])

    def test_unknown_phone_id_raises(self):
        self._patch_clients([PHONE_WITH_PROXY])
        with self.assertRaises(session.SessionError):
            session.start_session("nonexistent", transport=object())

    def test_a_phone_with_no_proxy_assigned_raises_before_leasing_anything(self):
        self._patch_clients([PHONE_NO_PROXY])
        with self.assertRaises(session.SessionError):
            session.start_session("p2", transport=object())
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_a_port_with_no_rotation_url_configured_raises(self):
        rotator = FakeRotator(PROXIES, rotatable=[])  # 54015 not configured
        self._patch_clients([PHONE_WITH_PROXY], rotator=rotator)
        with self.assertRaises(session.SessionError):
            session.start_session("p1", transport=object())
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_a_port_already_leased_by_someone_else_refuses_to_start(self):
        self._patch_clients([PHONE_WITH_PROXY])
        proxy_pool.acquire_proxy([54015], owner="someone-else")
        with self.assertRaises(session.SessionError):
            session.start_session("p1", transport=object(), wait_for_lease_seconds=0.1)

    def test_a_refused_rotation_raises_and_releases_the_lease(self):
        refused = {"port": 54015, "before": "1.1.1.1", "after": "1.1.1.1",
                  "changed": False, "seconds": 0.1, "accepted": False,
                  "detail": "ERROR"}
        rotator = FakeRotator(PROXIES, result=refused)
        self._patch_clients([PHONE_WITH_PROXY], rotator=rotator)
        with self.assertRaises(session.SessionError):
            session.start_session("p1", transport=object())
        # The failed attempt must not strand the port for the next caller.
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_the_phone_never_becoming_adb_ready_raises_and_releases_the_lease(self):
        self._patch_clients([PHONE_WITH_PROXY], ready_profile=None)
        with self.assertRaises(session.SessionError):
            session.start_session("p1", transport=object())
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_an_unchanged_ip_after_rotation_does_not_raise(self):
        """A mobile proxy can hand back the address it just released -- a
        real outcome, not a failure to abort the whole session over."""
        unchanged = {"port": 54015, "before": "1.1.1.1", "after": "1.1.1.1",
                    "changed": False, "seconds": 5.0, "accepted": True,
                    "detail": "OK"}
        rotator = FakeRotator(PROXIES, result=unchanged)
        self._patch_clients([PHONE_WITH_PROXY], rotator=rotator)
        result = session.start_session("p1", transport=object())
        self.assertFalse(result.rotation["changed"])


class StopSessionTest(SessionTestCase):
    def test_a_confirmed_stop_releases_the_lease(self):
        self._patch_clients([PHONE_WITH_PROXY], stop_result=True)
        result = session.start_session("p1", transport=object())
        self.assertEqual(proxy_pool.held_ports(), [54015])
        stopped = session.stop_session(result)
        self.assertTrue(stopped)
        self.assertEqual(proxy_pool.held_ports(), [])

    def test_an_unconfirmed_stop_keeps_the_lease_held(self):
        """If Geelark does not confirm the phone stopped, releasing the proxy
        would let a second phone share an exit IP with one that might still
        be live -- worse than a stuck lease that expires via its TTL."""
        self._patch_clients([PHONE_WITH_PROXY], stop_result=False)
        result = session.start_session("p1", transport=object())
        stopped = session.stop_session(result)
        self.assertFalse(stopped)
        self.assertEqual(proxy_pool.held_ports(), [54015])
