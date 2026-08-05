"""The cross-loop ceiling on live phones (TODO 2026-08-05 3.4).

`MAX_CONCURRENT_PROFILES` is applied by each loop on its own, so posting (10) +
warmup (10) + recheck (1) could have 21 phones open at once. These tests pin the
three properties that make `locks.acquire_slot` a real ceiling instead of a
hopeful one: it holds across processes, a slot whose owner was killed comes back,
and releasing frees a place.

Everything runs against a temporary lock directory -- the live one on this box
belongs to loops that are actually running.
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from adb_bot.config import settings
from adb_bot.core import locks, shutdown

REPO_ROOT = Path(__file__).resolve().parents[1]

# Acquire two slots, report how many were granted, then hold them until the
# parent closes our stdin. Run with HOME pointed at the test's temp dir, so the
# child computes the same lock directory the parent patched in.
_HOLDER_SOURCE = """
import sys
from adb_bot.core import locks
ceiling = int(sys.argv[1])
wanted = int(sys.argv[2])
granted = [s for s in (locks.acquire_slot(owner="child", ceiling=ceiling)
                       for _ in range(wanted)) if s is not None]
print(len(granted), flush=True)
sys.stdin.readline()
"""


class SlotTestCase(TestCase):
    """Every test gets its own lock directory and a clean slot registry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adbbot-slots-")
        self.home = Path(self.tmp)
        self.locks_dir = self.home / ".adb_bot" / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(locks, "lock_dir", lambda: self.locks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(locks.release_all_slots)

    def slot_files(self) -> list:
        return sorted(p.name for p in (self.locks_dir / "slots").glob("*.slot"))

    def start_holder(self, ceiling: int, wanted: int) -> subprocess.Popen:
        """A real second process holding real slots in the same directory."""
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(REPO_ROOT))
        env.pop("ADBBOT_MAX_LIVE_PROFILES", None)
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_SOURCE, str(ceiling), str(wanted)],
            cwd=str(REPO_ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_holder, child)
        granted = int((child.stdout.readline() or "0").strip())
        self.assertEqual(granted, wanted, "the child could not take the slots it needed")
        return child

    def _stop_holder(self, child) -> None:
        try:
            child.kill()
            child.wait(timeout=5)
        except Exception:
            pass


class CeilingHoldsTest(SlotTestCase):
    def test_only_ceiling_many_slots_exist_at_once(self):
        got = [locks.acquire_slot(owner="posting", ceiling=3) for _ in range(5)]
        self.assertEqual(sum(1 for s in got if s is not None), 3)
        self.assertEqual(len(self.slot_files()), 3)

    def test_two_loops_asking_concurrently_never_exceed_the_ceiling(self):
        """Posting and warmup racing on the same box, from many threads at once:
        the count of *simultaneously held* slots must never pass the ceiling."""
        ceiling = 4
        live = {"n": 0, "peak": 0}
        counter_lock = threading.Lock()
        start = threading.Barrier(12)

        def loop(owner):
            start.wait()
            for _ in range(6):
                slot = locks.acquire_slot(owner=owner, ceiling=ceiling)
                if slot is None:
                    continue
                with counter_lock:
                    live["n"] += 1
                    live["peak"] = max(live["peak"], live["n"])
                time.sleep(0.005)
                with counter_lock:
                    live["n"] -= 1
                locks.release_slot(slot)

        threads = [threading.Thread(target=loop, args=("posting" if i % 2 else "warmup",))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(live["peak"], ceiling)
        self.assertEqual(live["n"], 0)
        self.assertEqual(locks.held_slots(), [])

    def test_the_ceiling_holds_across_processes(self):
        """The property the whole design exists for: two *processes* (a warmup
        run and a posting run are never the same process) share one ceiling."""
        ceiling = 3
        self.start_holder(ceiling, 2)          # another loop holds 2 of 3

        mine = [locks.acquire_slot(owner="posting", ceiling=ceiling) for _ in range(3)]
        self.assertEqual(sum(1 for s in mine if s is not None), 1)
        self.assertEqual(locks.live_profile_count(), 3)

    def test_a_second_process_gets_the_slot_we_release(self):
        ceiling = 2
        held = [locks.acquire_slot(owner="posting", ceiling=ceiling) for _ in range(2)]
        self.assertTrue(all(held))
        locks.release_slot(held[0])
        self.start_holder(ceiling, 1)          # only possible because we let go
        self.assertEqual(locks.live_profile_count(), 2)


class SelfHealingTest(SlotTestCase):
    def test_a_slot_held_by_a_dead_pid_is_reclaimed(self):
        """One OOM kill must not shrink the ceiling for 45 minutes."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()                            # its pid is now gone
        slot_dir = self.locks_dir / "slots"
        slot_dir.mkdir(parents=True, exist_ok=True)
        orphan = slot_dir / "slot_000.slot"
        orphan.write_text(f"pid={dead.pid} token=ghost owner=posting at=now\n")

        self.assertEqual(locks.live_profile_count(), 0)   # not counted as live
        slot = locks.acquire_slot(owner="warmup", ceiling=1)
        self.assertIsNotNone(slot)
        self.assertIn(f"pid={os.getpid()}", orphan.read_text())

    def test_a_killed_process_frees_its_slots_for_the_next_run(self):
        """The real shape of the OOM: a live process holding slots is SIGKILLed
        (no handler runs, nothing is released) and the ceiling recovers anyway."""
        ceiling = 2
        child = self.start_holder(ceiling, 2)
        self.assertIsNone(locks.acquire_slot(owner="posting", ceiling=ceiling))

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)

        self.assertEqual(locks.live_profile_count(), 0)
        recovered = [locks.acquire_slot(owner="posting", ceiling=ceiling) for _ in range(2)]
        self.assertEqual(sum(1 for s in recovered if s is not None), 2)

    def test_an_expired_slot_is_reclaimed_even_if_its_pid_is_alive(self):
        slot = locks.acquire_slot(owner="posting", ceiling=1)
        self.assertIsNotNone(slot)
        self.assertIsNone(locks.acquire_slot(owner="warmup", ceiling=1))
        old = time.time() - 3600
        os.utime(slot.path, (old, old))
        self.assertIsNotNone(locks.acquire_slot(owner="warmup", ceiling=1, ttl_seconds=60))

    def test_release_after_being_reclaimed_does_not_free_the_new_holder(self):
        """The stolen-slot case: our release must not delete the file that now
        belongs to somebody else, or the ceiling silently gains a place."""
        stale = locks.acquire_slot(owner="posting", ceiling=1)
        old = time.time() - 3600
        os.utime(stale.path, (old, old))
        fresh = locks.acquire_slot(owner="warmup", ceiling=1, ttl_seconds=60)
        self.assertIsNotNone(fresh)

        locks.release_slot(stale)              # the old owner finally exits
        self.assertTrue(fresh.path.exists())
        self.assertIn("owner=warmup", fresh.path.read_text())


class ReleaseTest(SlotTestCase):
    def test_releasing_frees_a_slot(self):
        first = locks.acquire_slot(owner="posting", ceiling=1)
        self.assertIsNotNone(first)
        self.assertIsNone(locks.acquire_slot(owner="warmup", ceiling=1))

        locks.release_slot(first)
        self.assertEqual(self.slot_files(), [])
        self.assertIsNotNone(locks.acquire_slot(owner="warmup", ceiling=1))

    def test_double_release_and_release_of_none_are_safe(self):
        slot = locks.acquire_slot(owner="posting", ceiling=1)
        locks.release_slot(slot)
        locks.release_slot(slot)               # must not raise
        locks.release_slot(None)
        self.assertEqual(self.slot_files(), [])

    def test_context_manager_releases_on_success_and_on_error(self):
        with locks.live_profile_slot(owner="posting", ceiling=1) as slot:
            self.assertIsNotNone(slot)
        self.assertEqual(self.slot_files(), [])

        with self.assertRaises(RuntimeError):
            with locks.live_profile_slot(owner="posting", ceiling=1) as slot:
                self.assertIsNotNone(slot)
                raise RuntimeError("boom")
        self.assertEqual(self.slot_files(), [])

    def test_context_manager_yields_none_over_the_ceiling(self):
        with locks.live_profile_slot(owner="posting", ceiling=1):
            with locks.live_profile_slot(owner="warmup", ceiling=1) as second:
                self.assertIsNone(second)

    def test_release_all_slots_frees_everything_this_process_holds(self):
        for _ in range(3):
            locks.acquire_slot(owner="posting", ceiling=3)
        self.assertEqual(len(self.slot_files()), 3)

        self.assertEqual(locks.release_all_slots(), 3)
        self.assertEqual(self.slot_files(), [])
        self.assertEqual(locks.release_all_slots(), 0)     # idempotent

    def test_the_shutdown_drain_releases_slots(self):
        """The SIGTERM handler (TODO 3.2/3.3) must free slots as well as profile
        locks, or a stopped unit shrinks the ceiling until the TTL expires."""
        shutdown.reset()
        self.addCleanup(shutdown.reset)
        locks.acquire_slot(owner="posting", ceiling=2)
        locks.acquire_slot(owner="posting", ceiling=2)

        self.assertTrue(shutdown.run_cleanup(reason="test"))
        self.assertEqual(self.slot_files(), [])
        self.assertEqual(locks.held_slots(), [])


class FailSafeTest(SlotTestCase):
    def test_an_unusable_slot_directory_denies_the_launch(self):
        """Unreadable shared state must block a launch, not wave it through."""
        with patch.object(locks, "slot_dir", side_effect=OSError("no such dir")):
            self.assertIsNone(locks.acquire_slot(owner="posting", ceiling=5))
            self.assertEqual(locks.live_profile_count(), 0)

    def test_a_slot_file_with_no_pid_counts_as_held_until_its_ttl(self):
        """A claim caught between its O_EXCL create and its write has no pid yet.
        Treating that as free would put two phones on one slot."""
        slot_dir = self.locks_dir / "slots"
        slot_dir.mkdir(parents=True, exist_ok=True)
        half_written = slot_dir / "slot_000.slot"
        half_written.write_text("")

        self.assertIsNone(locks.acquire_slot(owner="posting", ceiling=1))
        self.assertEqual(locks.live_profile_count(), 1)

        old = time.time() - 3600
        os.utime(half_written, (old, old))
        self.assertIsNotNone(locks.acquire_slot(owner="posting", ceiling=1, ttl_seconds=60))

    def test_waiting_is_bounded_so_a_loop_can_never_deadlock(self):
        locks.acquire_slot(owner="warmup", ceiling=1)
        started = time.monotonic()
        denied = locks.acquire_slot(owner="posting", ceiling=1,
                                    wait_seconds=0.3, poll_seconds=0.05)
        elapsed = time.monotonic() - started
        self.assertIsNone(denied)
        self.assertLess(elapsed, 5.0)

    def test_waiting_succeeds_once_a_slot_is_freed(self):
        held = locks.acquire_slot(owner="warmup", ceiling=1)
        threading.Timer(0.1, lambda: locks.release_slot(held)).start()
        slot = locks.acquire_slot(owner="ui", ceiling=1, wait_seconds=5, poll_seconds=0.05)
        self.assertIsNotNone(slot)


class CeilingConfigTest(TestCase):
    def test_default_is_twelve(self):
        # 12 x ~215 MB of live phone is ~2.6 GB on a 15.2 GiB box whose baseline
        # is ~4.5 GB, so even a doubled tail of not-yet-dead phones stays in RAM
        # and leaves the 8 GB of swap as a backstop. It is deliberately above any
        # single loop's cap (10) so normal posting is never throttled by it.
        self.assertEqual(settings.DEFAULT_MAX_LIVE_PROFILES, 12)
        self.assertGreater(settings.DEFAULT_MAX_LIVE_PROFILES, 10)
        with patch.object(settings, "load_settings", return_value={}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADBBOT_MAX_LIVE_PROFILES", None)
            self.assertEqual(settings.get_saved_max_live_profiles(), 12)

    def test_env_override(self):
        with patch.object(settings, "load_settings", return_value={}), \
             patch.dict(os.environ, {"ADBBOT_MAX_LIVE_PROFILES": "6"}):
            self.assertEqual(settings.get_saved_max_live_profiles(), 6)

    def test_saved_setting_wins_over_env_and_accepts_a_json_number(self):
        with patch.object(settings, "load_settings", return_value={"max_live_profiles": 4}), \
             patch.dict(os.environ, {"ADBBOT_MAX_LIVE_PROFILES": "6"}):
            self.assertEqual(settings.get_saved_max_live_profiles(), 4)

    def test_nonsense_falls_back_to_the_default_and_never_goes_below_one(self):
        with patch.object(settings, "load_settings", return_value={"max_live_profiles": "abc"}):
            self.assertEqual(settings.get_saved_max_live_profiles(), 12)
        with patch.object(settings, "load_settings", return_value={"max_live_profiles": 0}):
            self.assertEqual(settings.get_saved_max_live_profiles(), 1)

    def test_max_live_profiles_resolves_like_the_other_caps(self):
        with patch.object(locks, "get_saved_max_live_profiles", return_value=7):
            self.assertEqual(locks.max_live_profiles(), 7)
        self.assertEqual(locks.max_live_profiles(3), 3)
        self.assertEqual(locks.max_live_profiles(0), 1)
        self.assertEqual(locks.max_live_profiles("nonsense"), settings.DEFAULT_MAX_LIVE_PROFILES)

    def test_a_broken_config_read_does_not_deny_every_launch(self):
        with patch.object(locks, "get_saved_max_live_profiles", side_effect=OSError("boom")):
            self.assertEqual(locks.max_live_profiles(), settings.DEFAULT_MAX_LIVE_PROFILES)


class RunnerRespectsTheCeilingTest(SlotTestCase):
    """Through the real runners with fakes: a loop that cannot get a slot must
    skip the profile and launch nothing, leaving it for the next tick."""

    def _plan(self, launch_ids):
        from unittest.mock import MagicMock
        items = [MagicMock(launch_id=lid, account_id=f"acc-{lid}", account_name=f"Acct {lid}",
                           queue_id=f"q-{lid}", caption="c", video_path="/v.mp4",
                           variant_id="var", retry_count=0) for lid in launch_ids]
        return MagicMock(to_post=items, skipped=[])

    def _run_posting(self, launch_ids, ceiling):
        import logging
        from unittest.mock import MagicMock
        from adb_bot.automation import posting_runner

        launched, ran = [], []
        launcher = MagicMock()
        launcher.start_profiles.side_effect = lambda ids: launched.extend(ids) or {"status": "ok"}

        with patch.object(locks, "get_saved_max_live_profiles", return_value=ceiling), \
             patch.object(posting_runner, "run_profile_workflow",
                          side_effect=lambda lid, *a, **k: ran.append(lid)), \
             patch.object(posting_runner, "apply_post_result"):
            result = posting_runner._launch_and_post(
                self._plan(launch_ids), list(launch_ids), MagicMock(), launcher, MagicMock(),
                MagicMock(), MagicMock(), MagicMock(), logging.getLogger("test"),
                0, 1, 0, None, None, None, "flow", max_concurrent_profiles=5,
            )
        return result, launched, ran

    def test_posting_defers_profiles_when_another_loop_holds_every_slot(self):
        ceiling = 2
        held = [locks.acquire_slot(owner="warmup", ceiling=ceiling) for _ in range(ceiling)]
        self.assertTrue(all(held))

        result, launched, ran = self._run_posting(["p1", "p2", "p3"], ceiling)

        self.assertEqual(launched, [])          # no phone was opened past the cap
        self.assertEqual(ran, [])
        self.assertEqual(result["no_slot"], 3)
        self.assertEqual(result["processed"], 0)

    def test_posting_runs_normally_when_there_is_room(self):
        result, launched, ran = self._run_posting(["p1", "p2"], 12)
        self.assertEqual(sorted(launched), ["p1", "p2"])
        self.assertEqual(sorted(ran), ["p1", "p2"])
        self.assertEqual(result.get("no_slot"), 0)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(self.slot_files(), [])    # and every slot was given back

    def test_warmup_defers_profiles_when_posting_holds_every_slot(self):
        import logging
        from unittest.mock import MagicMock
        from adb_bot.automation import airtable_runner

        ceiling = 1
        self.assertIsNotNone(locks.acquire_slot(owner="posting", ceiling=ceiling))

        account_plan = MagicMock(launch_id="p1", account_name="Acct", runs=[MagicMock()])
        plan = MagicMock(plans=[account_plan], skipped=[])
        launched = []
        launcher = MagicMock()
        launcher.start_profiles.side_effect = lambda ids: launched.extend(ids) or {"status": "ok"}

        with patch.object(locks, "get_saved_max_live_profiles", return_value=ceiling), \
             patch.object(airtable_runner, "run_profile_workflow") as workflow:
            result = airtable_runner._launch_and_run_flows(
                plan, ["p1"], MagicMock(), launcher, MagicMock(), MagicMock(), MagicMock(),
                MagicMock(), logging.getLogger("test"), 0, 1, 0, None, None, None, False,
                max_concurrent_profiles=5,
            )

        self.assertEqual(launched, [])
        workflow.assert_not_called()
        self.assertEqual(result["no_slot"], 1)
        self.assertEqual(result["processed"], 0)

    def test_recheck_probe_skips_rather_than_opening_an_extra_phone(self):
        """The recheck loop is only ever one profile, but one more phone on a
        full box is exactly what the ceiling is for. It returns None, which
        `decide_recheck` reads as 'unknown' and re-parks for the next pass."""
        import logging
        from unittest.mock import MagicMock
        from adb_bot.automation import recheck_runner, run_loop

        ceiling = 1
        self.assertIsNotNone(locks.acquire_slot(owner="posting", ceiling=ceiling))

        args = MagicMock(apply=True, base_id=None, airtable_token=None, mlx_token=None)
        airtable = MagicMock()
        airtable.list_posts_awaiting_recheck.return_value = []
        probes = []

        with patch.object(locks, "get_saved_max_live_profiles", return_value=ceiling), \
             patch.object(run_loop, "_airtable", return_value=airtable), \
             patch("adb_bot.automation.bootstrap.build_mlx_clients", return_value=MagicMock()), \
             patch("adb_bot.automation.bootstrap.build_automation", return_value=MagicMock()), \
             patch("adb_bot.automation.workflow.run_profile_workflow",
                   side_effect=lambda *a, **k: probes.append(a)), \
             patch.object(recheck_runner, "recheck_pending_posts") as recheck:
            recheck.return_value = {}
            run_loop._run_recheck(args, logging.getLogger("test"))
            read_post_count = recheck.call_args[0][1]
            self.assertIsNone(read_post_count("123456789", {}))

        self.assertEqual(probes, [])            # no phone was opened
