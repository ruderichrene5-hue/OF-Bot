"""Exclusive leasing of Geelark's four proxy ports, and their rotation cooldown.

Everything runs against a temporary lock directory -- the live one on this box
belongs to a fleet that may actually be running (see test_live_profile_ceiling.py,
which this mirrors for the MLX slot mechanism).
"""

import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from adb_bot.clients.geelark import proxy_pool


class PoolTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adbbot-proxy-pool-")
        self.locks_dir = Path(self.tmp) / ".adb_bot" / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(proxy_pool, "lock_dir", lambda: self.locks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(proxy_pool.release_all_proxies)


class LeasingTest(PoolTestCase):
    def test_each_port_can_only_be_leased_once(self):
        ports = [54015, 54018, 54021, 54024]
        first = proxy_pool.acquire_proxy(ports, owner="a")
        self.assertIsNotNone(first)
        # The same port must not be handed out again while held.
        second_round = [proxy_pool.acquire_proxy([first.port], owner="b")
                        for _ in range(3)]
        self.assertTrue(all(lease is None for lease in second_round))

    def test_a_lease_is_taken_from_the_pool_of_four_and_the_fifth_caller_is_denied(self):
        ports = [54015, 54018, 54021, 54024]
        leases = [proxy_pool.acquire_proxy(ports, owner=f"phone-{i}") for i in range(4)]
        self.assertTrue(all(leases))
        self.assertEqual(len({lease.port for lease in leases}), 4)
        fifth = proxy_pool.acquire_proxy(ports, owner="phone-4")
        self.assertIsNone(fifth)

    def test_releasing_frees_the_port_for_the_next_caller(self):
        ports = [54015]
        first = proxy_pool.acquire_proxy(ports, owner="a")
        self.assertIsNone(proxy_pool.acquire_proxy(ports, owner="b"))
        proxy_pool.release_proxy(first)
        second = proxy_pool.acquire_proxy(ports, owner="b")
        self.assertIsNotNone(second)

    def test_release_is_safe_to_call_twice(self):
        lease = proxy_pool.acquire_proxy([54015], owner="a")
        proxy_pool.release_proxy(lease)
        proxy_pool.release_proxy(lease)  # must not raise

    def test_no_ports_configured_means_nothing_to_lease(self):
        self.assertIsNone(proxy_pool.acquire_proxy([], owner="a"))

    def test_a_stale_lease_from_a_dead_pid_is_reclaimed(self):
        lease = proxy_pool.acquire_proxy([54015], owner="a")
        # Simulate the holder having died: rewrite the file with an
        # impossible pid, bypassing our own registry.
        lease.path.write_text("pid=999999999 token=stale owner=a at=x\n")
        with_dead_owner = proxy_pool.acquire_proxy([54015], owner="b")
        self.assertIsNotNone(with_dead_owner)
        self.assertEqual(with_dead_owner.port, 54015)

    def test_wait_seconds_blocks_until_a_port_frees_up(self):
        lease = proxy_pool.acquire_proxy([54015], owner="a")

        def release_soon():
            time.sleep(0.2)
            proxy_pool.release_proxy(lease)

        import threading
        threading.Thread(target=release_soon).start()
        second = proxy_pool.acquire_proxy([54015], owner="b", wait_seconds=2.0,
                                          poll_seconds=0.05)
        self.assertIsNotNone(second)

    def test_held_ports_reports_only_this_processs_leases(self):
        proxy_pool.acquire_proxy([54015], owner="a")
        proxy_pool.acquire_proxy([54018], owner="a")
        self.assertEqual(sorted(proxy_pool.held_ports()), [54015, 54018])

    def test_release_all_frees_every_lease_this_process_holds(self):
        proxy_pool.acquire_proxy([54015], owner="a")
        proxy_pool.acquire_proxy([54018], owner="a")
        freed = proxy_pool.release_all_proxies()
        self.assertEqual(freed, 2)
        self.assertEqual(proxy_pool.held_ports(), [])


class CooldownTest(PoolTestCase):
    def test_an_unrotated_port_is_immediately_rotatable(self):
        self.assertEqual(proxy_pool.seconds_until_rotatable(54015), 0.0)

    def test_a_just_rotated_port_must_wait_out_the_cooldown(self):
        proxy_pool.record_rotation(54015)
        remaining = proxy_pool.seconds_until_rotatable(54015, cooldown_seconds=65)
        self.assertGreater(remaining, 60)
        self.assertLessEqual(remaining, 65)

    def test_an_old_rotation_has_already_cleared(self):
        proxy_pool.record_rotation(54015, when=time.time() - 120)
        self.assertEqual(proxy_pool.seconds_until_rotatable(54015, cooldown_seconds=65), 0.0)

    def test_wait_for_cooldown_returns_immediately_when_already_clear(self):
        started = time.monotonic()
        waited = proxy_pool.wait_for_cooldown(54015, cooldown_seconds=65, poll_seconds=0.05)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(waited, 0.0)

    def test_wait_for_cooldown_blocks_for_the_remaining_time(self):
        proxy_pool.record_rotation(54015, when=time.time() - 0.3)
        waited = proxy_pool.wait_for_cooldown(54015, cooldown_seconds=0.5, poll_seconds=0.05)
        self.assertGreater(waited, 0.0)
        self.assertEqual(proxy_pool.seconds_until_rotatable(54015, cooldown_seconds=0.5), 0.0)

    def test_a_corrupt_cooldown_file_reads_as_never_rotated_not_an_error(self):
        proxy_pool._cooldown_path().write_text("{not json")
        self.assertEqual(proxy_pool.seconds_until_rotatable(54015), 0.0)


class FakeRotator:
    """Stands in for ProxyRotator: records which port was rotated and returns
    a canned rotate_and_verify result, without any real HTTP calls."""

    def __init__(self, ports):
        self._ports = ports
        self.rotated = []

    def rotatable_ports(self):
        return list(self._ports)

    def rotate_and_verify(self, port):
        self.rotated.append(port)
        return {"port": port, "before": "1.1.1.1", "after": "2.2.2.2",
                "changed": True, "seconds": 22.0, "accepted": True, "detail": "OK"}


class PrepareProxyForSessionTest(PoolTestCase):
    def test_leases_a_port_rotates_it_and_records_the_cooldown(self):
        rotator = FakeRotator([54015, 54018])
        lease, rotation = proxy_pool.prepare_proxy_for_session(rotator, owner="phone-1")
        self.assertIsNotNone(lease)
        self.assertIn(lease.port, [54015, 54018])
        self.assertEqual(rotation["port"], lease.port)
        self.assertTrue(rotation["changed"])
        self.assertEqual(rotator.rotated, [lease.port])
        # The cooldown clock is now running for the port that was used.
        self.assertGreater(proxy_pool.seconds_until_rotatable(lease.port), 0)

    def test_returns_none_none_when_every_port_is_already_leased(self):
        rotator = FakeRotator([54015])
        proxy_pool.acquire_proxy([54015], owner="someone-else")
        lease, rotation = proxy_pool.prepare_proxy_for_session(
            rotator, owner="phone-1", wait_for_lease_seconds=0.1)
        self.assertIsNone(lease)
        self.assertIsNone(rotation)
        self.assertEqual(rotator.rotated, [])

    def test_waits_out_an_existing_cooldown_before_rotating_again(self):
        rotator = FakeRotator([54015])
        proxy_pool.record_rotation(54015, when=time.time() - 0.3)
        started = time.monotonic()
        lease, rotation = proxy_pool.prepare_proxy_for_session(
            rotator, owner="phone-1", cooldown_seconds=0.5)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.15)  # actually waited, didn't rotate immediately
        self.assertEqual(rotator.rotated, [54015])
