import os
import time
from unittest import TestCase

from adb_bot.core import locks


class LockTest(TestCase):
    def setUp(self):
        # Unique name per test so runs never collide in the shared lock dir.
        self.name = f"test_profile_{os.getpid()}_{time.time_ns()}"
        self.addCleanup(locks.release, self.name)

    def test_acquire_then_second_acquire_fails(self):
        self.assertTrue(locks.acquire(self.name))
        self.assertFalse(locks.acquire(self.name))   # someone already holds it

    def test_release_allows_reacquire(self):
        self.assertTrue(locks.acquire(self.name))
        locks.release(self.name)
        self.assertTrue(locks.acquire(self.name))

    def test_is_locked(self):
        self.assertFalse(locks.is_locked(self.name))
        locks.acquire(self.name)
        self.assertTrue(locks.is_locked(self.name))
        locks.release(self.name)
        self.assertFalse(locks.is_locked(self.name))

    def test_stale_lock_is_stolen(self):
        # A crashed run leaves its lock behind; a lock older than the TTL must
        # not wedge the profile forever.
        self.assertTrue(locks.acquire(self.name))
        self.assertFalse(locks.acquire(self.name, ttl_seconds=3600))
        self.assertTrue(locks.acquire(self.name, ttl_seconds=0))   # everything is stale
        self.assertTrue(locks.is_locked(self.name))                 # and we now hold it

    def test_release_of_unheld_lock_is_safe(self):
        locks.release(self.name)   # must not raise

    def test_name_is_sanitized_into_one_file(self):
        weird = "id/with\\separators:and spaces"
        self.assertTrue(locks.acquire(weird))
        self.addCleanup(locks.release, weird)
        self.assertTrue(locks.is_locked(weird))
        self.assertTrue(locks._lock_path(weird).is_file())


class ProfileLocksTest(TestCase):
    def setUp(self):
        stamp = f"{os.getpid()}_{time.time_ns()}"
        self.a, self.b = f"pA_{stamp}", f"pB_{stamp}"
        for n in (self.a, self.b):
            self.addCleanup(locks.release, n)

    def test_acquires_free_and_reports_busy(self):
        locks.acquire(self.b, owner="posting")          # b already taken elsewhere
        with locks.ProfileLocks(owner="warmup") as pl:
            got = pl.acquire_all([self.a, self.b])
            self.assertEqual(got, [self.a])
            self.assertEqual(pl.busy, [self.b])

    def test_context_manager_releases_on_exit(self):
        with locks.ProfileLocks(owner="posting") as pl:
            pl.acquire_all([self.a])
            self.assertTrue(locks.is_locked(self.a))
        self.assertFalse(locks.is_locked(self.a))

    def test_releases_even_when_body_raises(self):
        with self.assertRaises(RuntimeError):
            with locks.ProfileLocks(owner="posting") as pl:
                pl.acquire_all([self.a])
                raise RuntimeError("boom")
        self.assertFalse(locks.is_locked(self.a))   # not wedged by the crash

    def test_two_loops_cannot_hold_the_same_profile(self):
        with locks.ProfileLocks(owner="posting") as posting:
            posting.acquire_all([self.a])
            with locks.ProfileLocks(owner="warmup") as warmup:
                self.assertEqual(warmup.acquire_all([self.a]), [])
                self.assertEqual(warmup.busy, [self.a])
