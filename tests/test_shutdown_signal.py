"""The cleanup that runs when the process is killed rather than finishing.

`systemctl stop adbbot-posting` gave neither guarantee a chance to run on
2026-08-04: the profile locks stayed on disk until their 45-minute TTL, so the
next posting run found every profile "busy in another loop" and did nothing for
~17 minutes; and every open phone stayed open. These cover the SIGTERM handler
that fixes both (TODO 3.2 / 3.3).

Nothing here touches a real phone or a real MultiLogin agent -- the shutdown
clients are recorders, and the signal handler is called directly with the
re-raise stubbed out, because a genuine SIGTERM would take the test runner with
it.
"""

import os
import signal
import threading
import time
import unittest
from unittest.mock import patch

from adb_bot.automation import workflow
from adb_bot.core import locks, shutdown


class L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def exception(self, *a, **k): pass


class Rec:
    """A shutdown client that records instead of closing a phone."""

    def __init__(self):
        self.calls = []

    def shutdown_profiles(self, ids):
        self.calls.append(list(ids))
        return {"status": "ok"}


def _stamp() -> str:
    return f"{os.getpid()}_{time.time_ns()}"


class ShutdownBase(unittest.TestCase):
    def setUp(self):
        shutdown.reset(restore_handlers=True)
        self.addCleanup(shutdown.reset, True)


class LockReleaseTest(ShutdownBase):
    def test_sigterm_releases_the_locks_this_process_holds(self):
        stamp = _stamp()
        mine, theirs = f"sig_mine_{stamp}", f"sig_theirs_{stamp}"
        # Another loop (a different process) already holds `theirs`.
        locks.acquire(theirs, owner="warmup")
        self.addCleanup(locks.release, theirs)
        self.addCleanup(locks.release, mine)

        holder = locks.ProfileLocks(owner="posting")
        with holder:
            self.assertEqual(holder.acquire_all([mine, theirs]), [mine])
            self.assertTrue(locks.is_locked(mine))

            shutdown.run_cleanup(reason="test", logger=L())

            self.assertFalse(locks.is_locked(mine), "our lock survived the SIGTERM handler")
            self.assertTrue(locks.is_locked(theirs),
                            "the handler released a lock another loop holds")

    def test_context_manager_registers_and_unregisters(self):
        holder = locks.ProfileLocks(owner="posting")
        self.assertEqual(shutdown._lock_holders, [])
        with holder:
            self.assertIn(holder, shutdown._lock_holders)
        # A run that finished normally must not leave itself in the register.
        self.assertEqual(shutdown._lock_holders, [])

    def test_a_holder_that_raises_does_not_stop_the_rest(self):
        stamp = _stamp()
        name = f"sig_after_boom_{stamp}"
        self.addCleanup(locks.release, name)

        class Boom:
            held = ["never-released"]

            def release_all(self):
                raise RuntimeError("lock dir is gone")

        shutdown.register_locks(Boom())
        good = locks.ProfileLocks(owner="posting")
        shutdown.register_locks(good)
        good.acquire_all([name])

        self.assertTrue(shutdown.run_cleanup(reason="test", logger=L()))
        self.assertFalse(locks.is_locked(name))


class PhoneCloseTest(ShutdownBase):
    def test_an_open_phone_is_closed(self):
        client = Rec()
        shutdown.register_open_profile("profile-1",
                                       lambda: client.shutdown_profiles(["profile-1"]))
        shutdown.run_cleanup(reason="test", logger=L())
        self.assertEqual(client.calls, [["profile-1"]])

    def test_one_failing_close_does_not_block_the_others(self):
        client = Rec()

        def boom():
            raise RuntimeError("MultiLogin said 500")

        shutdown.register_open_profile("bad", boom)
        shutdown.register_open_profile("ok-1", lambda: client.shutdown_profiles(["ok-1"]))
        shutdown.register_open_profile("ok-2", lambda: client.shutdown_profiles(["ok-2"]))

        self.assertTrue(shutdown.run_cleanup(reason="test", logger=L()))
        self.assertEqual(sorted(client.calls), [["ok-1"], ["ok-2"]])

    def test_a_hung_close_is_bounded_by_the_budget(self):
        # `systemctl stop` escalates to SIGKILL on a timeout, so a close that
        # never returns must not take the whole handler with it.
        started = threading.Event()
        client = Rec()

        def hangs():
            started.set()
            time.sleep(30)

        shutdown.register_open_profile("hung", hangs)
        shutdown.register_open_profile("ok", lambda: client.shutdown_profiles(["ok"]))

        begin = time.monotonic()
        shutdown.run_cleanup(reason="test", logger=L(), budget_seconds=0.3)
        elapsed = time.monotonic() - begin

        self.assertTrue(started.wait(1), "the hung close never started")
        self.assertLess(elapsed, 5, "a hung close ate the whole stop timeout")
        self.assertEqual(client.calls, [["ok"]],
                         "the healthy phone was not closed alongside the hung one")

    def test_locks_are_released_before_phones_are_closed(self):
        # Ordering matters: releasing a lock is a local unlink and cannot block,
        # while closing a phone is an HTTP call that demonstrably can. Locks
        # first means a hung close never costs the next run its 45 minutes.
        stamp = _stamp()
        name = f"sig_order_{stamp}"
        self.addCleanup(locks.release, name)
        holder = locks.ProfileLocks(owner="posting")
        shutdown.register_locks(holder)
        holder.acquire_all([name])

        seen = {}
        shutdown.register_open_profile(
            "profile-1", lambda: seen.setdefault("lock_still_held", locks.is_locked(name)))

        shutdown.run_cleanup(reason="test", logger=L(), budget_seconds=5)
        self.assertIs(seen.get("lock_still_held"), False,
                      "phones were closed before the locks were released")


class IdempotencyTest(ShutdownBase):
    def test_a_second_signal_does_not_re_run_the_drain(self):
        client = Rec()
        shutdown.register_open_profile("profile-1",
                                       lambda: client.shutdown_profiles(["profile-1"]))

        self.assertTrue(shutdown.run_cleanup(reason="first", logger=L()))
        self.assertFalse(shutdown.run_cleanup(reason="second", logger=L()))
        self.assertEqual(client.calls, [["profile-1"]], "the phone was closed twice")

    def test_handler_signalled_twice_closes_once_and_still_exits(self):
        client = Rec()
        shutdown.register_open_profile("profile-1",
                                       lambda: client.shutdown_profiles(["profile-1"]))
        reraised = []
        with patch.object(shutdown, "_reraise", reraised.append):
            shutdown.handle_signal(signal.SIGTERM, None, logger=L())
            shutdown.handle_signal(signal.SIGTERM, None, logger=L())

        self.assertEqual(client.calls, [["profile-1"]])
        # Both signals must still terminate: an impatient second Ctrl-C that
        # returned into the loop would be worse than the leak.
        self.assertEqual(reraised, [signal.SIGTERM, signal.SIGTERM])


class HandlerInstallTest(ShutdownBase):
    def test_sigterm_and_sigint_are_installed_and_drain(self):
        client = Rec()
        installed = shutdown.install_signal_handlers(L())
        self.assertIn(int(signal.SIGTERM), installed)
        self.assertIn(int(signal.SIGINT), installed)

        shutdown.register_open_profile("profile-1",
                                       lambda: client.shutdown_profiles(["profile-1"]))
        reraised = []
        with patch.object(shutdown, "_reraise", reraised.append):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        self.assertEqual(client.calls, [["profile-1"]])
        self.assertEqual(reraised, [signal.SIGTERM])

    def test_install_off_the_main_thread_is_a_no_op_not_an_error(self):
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(installed=shutdown.install_signal_handlers(L())))
        thread.start()
        thread.join(5)
        self.assertEqual(result.get("installed"), [])

    def test_run_loop_installs_the_handler(self):
        from adb_bot.automation import run_loop
        with patch.object(run_loop.shutdown, "install_signal_handlers") as install, \
             patch.dict(run_loop._DISPATCH, {"doctor": lambda args, logger: 0}):
            run_loop.main(["doctor"])
        install.assert_called_once()


class WorkflowRegistrationTest(ShutdownBase):
    """The wrapper that guarantees a phone is closed must also hand it to the
    signal handler, which is the one exit path the wrapper cannot see."""

    def _wrapped(self):
        def inner(profile_id, shutdown_client, logger, gate):
            gate.wait(5)
        return workflow._guarantee_profile_closed(inner)

    def test_a_phone_open_mid_workflow_is_closed_by_the_handler(self):
        client = Rec()
        gate = threading.Event()
        wrapped = self._wrapped()
        thread = threading.Thread(
            target=wrapped, args=("profile-1", client, L(), gate),
            kwargs={"max_open_seconds": 0}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while "profile-1" not in shutdown.open_profile_ids() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("profile-1", shutdown.open_profile_ids(),
                          "the running workflow never registered its phone")

            shutdown.run_cleanup(reason="test", logger=L(), budget_seconds=5)
            self.assertEqual(client.calls, [["profile-1"]])
        finally:
            gate.set()
            thread.join(5)

        # And the workflow's own `finally` must not send a second close.
        self.assertEqual(client.calls, [["profile-1"]])

    def test_a_finished_workflow_leaves_nothing_registered(self):
        client = Rec()
        gate = threading.Event()
        gate.set()
        self._wrapped()("profile-1", client, L(), gate, max_open_seconds=0)

        self.assertEqual(shutdown.open_profile_ids(), [])
        self.assertEqual(client.calls, [["profile-1"]])
        # A drain after the run is over must not re-close it.
        shutdown.run_cleanup(reason="test", logger=L())
        self.assertEqual(client.calls, [["profile-1"]])


if __name__ == "__main__":
    unittest.main()
