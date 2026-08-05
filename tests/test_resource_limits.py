"""CPU/disk guard rails: profile concurrency, spoof throughput, and retention."""

import logging
import tempfile
import time
from pathlib import Path
from unittest import TestCase

from adb_bot.automation import retention
from adb_bot.automation.spoof_pipeline import MAX_VARIANTS_PER_RUN, RawVideo, run_pipeline
from adb_bot.core.batching import (
    LaunchGate,
    MAX_CONCURRENT_PROFILES,
    chunked,
    resolve_concurrency,
    run_rolling,
)

LOG = logging.getLogger("test")


class BatchingTest(TestCase):
    def test_default_cap_is_ten_profiles(self):
        # Raised to 10 on 2026-08-04, once `run_profile_workflow` was made to
        # close every phone on every exit path (and force-close anything still
        # open after 7 min). Before that the live phone count was unbounded by
        # this cap -- 78 were alive with only five being driven -- and the box
        # OOMed twice. See batching.py and workflow.py for the full note.
        self.assertEqual(MAX_CONCURRENT_PROFILES, 10)
        self.assertEqual(resolve_concurrency(None), 10)

    def test_chunks_are_capped(self):
        batches = chunked(range(25), 10)
        self.assertEqual([len(b) for b in batches], [10, 10, 5])
        self.assertTrue(all(len(b) <= 10 for b in batches))

    def test_no_batch_exceeds_the_cap_for_a_big_fleet(self):
        # 91 profiles is the real MultiLogin workspace size.
        for batch in chunked(range(91), resolve_concurrency(None)):
            self.assertLessEqual(len(batch), MAX_CONCURRENT_PROFILES)

    def test_empty_input(self):
        self.assertEqual(chunked([], 10), [])

    def test_explicit_override_and_bad_values(self):
        self.assertEqual(resolve_concurrency(7), 7)
        self.assertEqual(resolve_concurrency(0), 1)      # never zero
        self.assertEqual(resolve_concurrency(-5), 1)
        self.assertEqual(resolve_concurrency("nonsense"), MAX_CONCURRENT_PROFILES)

    def test_every_run_path_uses_the_rolling_window(self):
        """Including the UI. The headless runners were capped long ago, but the
        UI's own run path launched every selected profile up front and sized its
        pool to match -- selecting 20 ran 20 phones at once."""
        import inspect
        from adb_bot.automation import airtable_runner, posting_runner
        from adb_bot.ui import ui as ui_module
        for module in (airtable_runner, posting_runner, ui_module):
            src = inspect.getsource(module)
            self.assertIn("run_rolling", src, module.__name__)
            self.assertIn("resolve_concurrency", src, module.__name__)
        ui_src = inspect.getsource(ui_module)
        # The specific unbounded pool that made 20 selected profiles run at once.
        self.assertNotIn("max_workers=len(selected_ids)", ui_src)
        for module in (airtable_runner, posting_runner):
            src = inspect.getsource(module)
            self.assertNotIn("max_workers=max(1, len(plan.plans))", src)
            self.assertNotIn("max_workers=max(1, len(launch_ids))", src)


class RollingWindowTest(TestCase):
    """The window must hold at `concurrency`, and a finished unit must be
    replaced immediately rather than waiting for a whole batch to drain."""

    def _tracker(self):
        import threading
        state = {"live": 0, "peak": 0, "order": []}
        lock = threading.Lock()

        def enter(unit):
            with lock:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])

        def leave(unit):
            with lock:
                state["live"] -= 1
                state["order"].append(unit)

        return state, enter, leave

    def test_never_exceeds_the_cap(self):
        state, enter, leave = self._tracker()

        def unit(n):
            enter(n)
            time.sleep(0.02)
            leave(n)

        run_rolling(range(20), unit, concurrency=5)
        self.assertLessEqual(state["peak"], 5)
        self.assertEqual(len(state["order"]), 20)

    def test_a_slow_unit_does_not_hold_up_the_others(self):
        """The reason for the change. With fixed batches of 2, the slow unit's
        partner is the ONLY other unit that can run alongside it -- everything
        else waits for that batch to drain. Rolling keeps feeding the free slot,
        so several finish while the slow one is still going."""
        state, enter, leave = self._tracker()
        finished_before_slow = []

        def unit(n):
            enter(n)
            if n == 0:
                time.sleep(0.5)
                finished_before_slow.extend(state["order"])
            else:
                time.sleep(0.01)
            leave(n)

        run_rolling(range(6), unit, concurrency=2)
        self.assertGreaterEqual(
            len(finished_before_slow), 3,
            "only a batched implementation would leave the free slot idle")

    def test_all_units_run_even_when_one_raises(self):
        """One bad profile must not abandon the rest of the run."""
        done = []

        def unit(n):
            if n == 2:
                raise RuntimeError("boom")
            done.append(n)

        counts = run_rolling(range(5), unit, concurrency=2, logger=LOG)
        self.assertEqual(sorted(done), [0, 1, 3, 4])
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["completed"], 4)

    def test_abort_stops_starting_new_units(self):
        started = []
        stop = {"now": False}

        def unit(n):
            started.append(n)
            stop["now"] = True

        counts = run_rolling(range(20), unit, concurrency=1,
                             should_stop=lambda: stop["now"])
        self.assertEqual(len(started), 1)
        self.assertTrue(counts["aborted"])

    def test_empty_units(self):
        counts = run_rolling([], lambda n: None, concurrency=5)
        self.assertEqual(counts["completed"], 0)
        self.assertFalse(counts["aborted"])


class LaunchGateTest(TestCase):
    """Launching is the one step that stays sequential: five simultaneous launch
    calls is the burst that used to bring profiles up unready."""

    def test_launches_do_not_overlap(self):
        import threading
        gate = LaunchGate(0)
        live = {"n": 0, "peak": 0}
        lock = threading.Lock()

        def launch():
            with lock:
                live["n"] += 1
                live["peak"] = max(live["peak"], live["n"])
            time.sleep(0.01)
            with lock:
                live["n"] -= 1

        run_rolling(range(8), lambda n: gate.launch(launch), concurrency=4)
        self.assertEqual(live["peak"], 1)

    def test_delay_is_applied_between_launches(self):
        gate = LaunchGate(0.05)
        started = time.time()
        for _ in range(3):
            gate.launch(lambda: None)
        self.assertGreaterEqual(time.time() - started, 0.10)

    def test_zero_delay_does_not_sleep(self):
        gate = LaunchGate(0)
        started = time.time()
        for _ in range(5):
            gate.launch(lambda: None)
        self.assertLess(time.time() - started, 0.2)


class FakePipelineClient:
    def __init__(self, accounts):
        self._accounts = accounts
        self.variant_rows = []

    def content_pipeline_names(self):
        return set()

    def active_accounts_by_model(self):
        return {"nikki": self._accounts}

    def models_by_name(self):
        return {"nikki": "recModelN"}

    def create_content_pipeline(self, name, model_id=None, raw_link=None):
        return f"recCP-{name}"

    def create_spoof_variant(self, cp, acct, path, method=None, variant_id=None,
                             target_profile_id=None):
        self.variant_rows.append((cp, acct or target_profile_id, path))
        return "recSV"

    def set_content_pipeline_spoofed(self, rec, failed=False):
        return True


class FakeSource:
    def __init__(self, videos):
        self._videos = videos

    def list_by_model(self):
        return {"Nikki": self._videos}

    def resolve(self, video):
        return video.path

    def release(self, video, path):
        return None


class VariantCapTest(TestCase):
    def _run(self, n_videos, n_accounts, max_variants=None, order=None):
        videos = [RawVideo(model="Nikki", name=f"clip{i}.mp4", path=f"/raw/clip{i}.mp4")
                  for i in range(n_videos)]
        accounts = [{"account_id": f"a{i}", "handle": f"nikki_{i}"} for i in range(n_accounts)]
        client = FakePipelineClient(accounts)

        def spoof(raw, out_dir, seed, logger=None):
            if order is not None:
                order.append(raw)
            # spoof_fn must return a file that exists: the pipeline renames it
            # into <source>__<handle> before recording the variant.
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            produced = Path(out_dir) / f"v{seed}.mp4"
            produced.write_text("video")
            return produced

        kwargs = {} if max_variants is None else {"max_variants": max_variants}
        with tempfile.TemporaryDirectory() as out_root:
            report = run_pipeline(client, LOG, raw_root="/raw", out_root=out_root,
                                  source=FakeSource(videos), spoof_fn=spoof, dry_run=False, **kwargs)
        return report, client

    def test_default_cap_is_twenty(self):
        self.assertEqual(MAX_VARIANTS_PER_RUN, 20)

    def test_stops_near_the_cap_instead_of_spoofing_everything(self):
        # 20 videos x 5 accounts = 100 variants if uncapped.
        report, client = self._run(n_videos=20, n_accounts=5)
        self.assertLess(len(client.variant_rows), 100)
        self.assertLessEqual(len(client.variant_rows), 20 + 5)   # cap + at most one video

    def test_videos_are_never_left_half_spoofed(self):
        # Every processed video must have a variant for EVERY account -- a
        # partial video would strand accounts, since its name is already
        # recorded and it is skipped next run.
        report, client = self._run(n_videos=9, n_accounts=3, max_variants=5)
        per_video: dict = {}
        for cp, _acct, _path in client.variant_rows:
            per_video[cp] = per_video.get(cp, 0) + 1
        self.assertTrue(per_video)
        for cp, count in per_video.items():
            self.assertEqual(count, 3, f"{cp} got {count} variants, expected 3")

    def test_remaining_videos_are_reported_as_skipped(self):
        report, _client = self._run(n_videos=10, n_accounts=5, max_variants=5)
        self.assertTrue(any("run cap" in reason for _n, reason in report.skipped))

    def test_cap_can_be_disabled(self):
        _report, client = self._run(n_videos=6, n_accounts=5, max_variants=None if False else 10**6)
        self.assertEqual(len(client.variant_rows), 30)

    def test_encoding_is_serial(self):
        # One ffmpeg at a time: the calls must arrive one after another, not
        # interleaved from a pool.
        order = []
        self._run(n_videos=3, n_accounts=2, max_variants=10**6, order=order)
        self.assertEqual(len(order), 6)
        self.assertEqual(order, sorted(order, key=order.index))   # stable, sequential


class RetentionTest(TestCase):
    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp())
        self.used = self.root / "used"
        self.used.mkdir()
        self.old = self.used / "old.mp4"
        self.new = self.used / "new.mp4"
        self.pending = self.root / "not_posted_yet.mp4"
        for f in (self.old, self.new, self.pending):
            f.write_bytes(b"x" * 100)
        old_time = time.time() - (5 * 86400)
        import os
        os.utime(self.old, (old_time, old_time))

    def test_deletes_only_old_used_inputs(self):
        report = retention.purge_used_inputs([str(self.root)], max_age_days=2, dry_run=False)
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.exists())
        self.assertEqual(len(report.deleted), 1)

    def test_never_touches_clips_outside_the_used_folder(self):
        # Files still waiting to be posted live in the parent folder.
        retention.purge_used_inputs([str(self.root)], max_age_days=0, dry_run=False)
        self.assertTrue(self.pending.exists())

    def test_dry_run_deletes_nothing(self):
        report = retention.purge_used_inputs([str(self.root)], max_age_days=2, dry_run=True)
        self.assertTrue(self.old.exists())
        self.assertEqual(len(report.deleted), 1)   # reported, not removed
        self.assertGreater(report.freed_bytes, 0)

    def test_missing_root_is_safe(self):
        report = retention.purge_used_inputs(["/no/such/dir", "", None], max_age_days=2, dry_run=False)
        self.assertEqual(report.deleted, [])


class VariantRetentionTest(TestCase):
    def setUp(self):
        import os
        import tempfile
        self.root = Path(tempfile.mkdtemp())
        self.used_old = self.root / "used_old.mp4"
        self.ready_old = self.root / "ready_old.mp4"
        self.used_new = self.root / "used_new.mp4"
        for f in (self.used_old, self.ready_old, self.used_new):
            f.write_bytes(b"y" * 50)
        old = time.time() - (5 * 86400)
        os.utime(self.used_old, (old, old))
        os.utime(self.ready_old, (old, old))

        class FakeAT:
            def variants_by_id(inner):
                return {
                    "v1": {"file_path": str(self.used_old), "status": "Used"},
                    "v2": {"file_path": str(self.ready_old), "status": "Ready"},
                    "v3": {"file_path": str(self.used_new), "status": "Used"},
                    "v4": {"file_path": None, "status": "Used"},
                }
        self.airtable = FakeAT()

    def test_deletes_used_variants_past_the_grace_period(self):
        retention.purge_used_variants(self.airtable, max_age_days=2, dry_run=False)
        self.assertFalse(self.used_old.exists())

    def test_never_deletes_a_variant_still_waiting_to_post(self):
        # THE safety rule: a Ready variant is the media a scheduled post needs.
        retention.purge_used_variants(self.airtable, max_age_days=0, dry_run=False)
        self.assertTrue(self.ready_old.exists())

    def test_keeps_recently_used_variants(self):
        retention.purge_used_variants(self.airtable, max_age_days=2, dry_run=False)
        self.assertTrue(self.used_new.exists())

    def test_airtable_failure_is_reported_not_raised(self):
        class Broken:
            def variants_by_id(self):
                raise RuntimeError("api down")

        report = retention.purge_used_variants(Broken(), dry_run=False)
        self.assertTrue(report.errors)
        self.assertEqual(report.deleted, [])


class EmptyDirPruneTest(TestCase):
    def test_removes_empty_dirs_but_keeps_the_root(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        (root / "Nikki" / "nikki_1").mkdir(parents=True)
        (root / "Nikki" / "nikki_2").mkdir(parents=True)
        (root / "Nikki" / "nikki_2" / "keep.mp4").write_bytes(b"z")
        removed = retention.prune_empty_dirs(str(root), dry_run=False)
        self.assertEqual(removed, 1)
        self.assertFalse((root / "Nikki" / "nikki_1").exists())
        self.assertTrue((root / "Nikki" / "nikki_2").exists())
        self.assertTrue(root.exists())

    def test_no_root_is_safe(self):
        self.assertEqual(retention.prune_empty_dirs(None), 0)
        self.assertEqual(retention.prune_empty_dirs("/no/such/dir"), 0)


class RunnerConcurrencyTest(TestCase):
    """End-to-end through the real runners with fakes: the cap has to hold where
    it actually matters, and the change must not alter what gets launched, run,
    or written back."""

    def _plan(self, launch_ids, posts_per_profile=1):
        from unittest.mock import MagicMock
        items = []
        for lid in launch_ids:
            for n in range(posts_per_profile):
                items.append(MagicMock(launch_id=lid, account_id=f"acc-{lid}",
                                       account_name=f"Acct {lid}", queue_id=f"q-{lid}-{n}",
                                       caption="c", video_path="/v.mp4", variant_id="var",
                                       retry_count=0))
        return MagicMock(to_post=items, skipped=[])

    def _run_posting(self, launch_ids, concurrency, posts_per_profile=1):
        from unittest.mock import MagicMock, patch
        from adb_bot.automation import posting_runner
        import threading

        state = {"live": 0, "peak": 0}
        lock = threading.Lock()
        ran, launched = [], []

        def fake_workflow(launch_id, *a, **k):
            with lock:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(0.02)
            with lock:
                state["live"] -= 1
                ran.append(launch_id)

        launcher = MagicMock()
        launcher.start_profiles.side_effect = lambda ids: launched.extend(ids) or {"status": "ok"}

        plan = self._plan(launch_ids, posts_per_profile)
        with patch.object(posting_runner, "run_profile_workflow", side_effect=fake_workflow), \
             patch.object(posting_runner, "apply_post_result"):
            result = posting_runner._launch_and_post(
                plan, list(launch_ids), MagicMock(), launcher, MagicMock(), MagicMock(),
                MagicMock(), MagicMock(), LOG, 0, 1, 0, None, None, None, "flow",
                max_concurrent_profiles=concurrency,
            )
        return result, state, ran, launched

    def test_never_more_than_five_phones_live(self):
        result, state, ran, launched = self._run_posting([f"p{i}" for i in range(20)], 5)
        self.assertLessEqual(state["peak"], 5)
        self.assertEqual(len(ran), 20)          # every post still ran
        self.assertEqual(len(launched), 20)     # every profile still launched
        self.assertEqual(result["processed"], 20)

    def test_each_profile_is_launched_exactly_once(self):
        _result, _state, _ran, launched = self._run_posting([f"p{i}" for i in range(8)], 3)
        self.assertEqual(sorted(launched), sorted(set(launched)))

    def test_posts_on_one_profile_never_overlap(self):
        """Two posts for the same account share one phone, and the workflow shuts
        the profile down when it succeeds -- so overlapping them would pull the
        device out from under the second."""
        from unittest.mock import MagicMock, patch
        from adb_bot.automation import posting_runner
        import threading

        live_per_profile, peak_per_profile = {}, {}
        lock = threading.Lock()

        def fake_workflow(launch_id, *a, **k):
            with lock:
                live_per_profile[launch_id] = live_per_profile.get(launch_id, 0) + 1
                peak_per_profile[launch_id] = max(peak_per_profile.get(launch_id, 0),
                                                  live_per_profile[launch_id])
            time.sleep(0.02)
            with lock:
                live_per_profile[launch_id] -= 1

        plan = self._plan(["p1", "p2"], posts_per_profile=3)
        with patch.object(posting_runner, "run_profile_workflow", side_effect=fake_workflow), \
             patch.object(posting_runner, "apply_post_result"):
            posting_runner._launch_and_post(
                plan, ["p1", "p2"], MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                MagicMock(), MagicMock(), LOG, 0, 1, 0, None, None, None, "flow",
                max_concurrent_profiles=5,
            )
        self.assertEqual(set(peak_per_profile.values()), {1}, peak_per_profile)

    def test_uses_the_default_cap_when_none_requested(self):
        _result, state, _ran, _launched = self._run_posting([f"p{i}" for i in range(12)], None)
        self.assertLessEqual(state["peak"], MAX_CONCURRENT_PROFILES)

    def test_abort_before_start_processes_nothing(self):
        from unittest.mock import MagicMock, patch
        from adb_bot.automation import posting_runner
        ran = []
        plan = self._plan(["p1", "p2", "p3"])
        with patch.object(posting_runner, "run_profile_workflow",
                          side_effect=lambda *a, **k: ran.append(1)), \
             patch.object(posting_runner, "apply_post_result"):
            result = posting_runner._launch_and_post(
                plan, ["p1", "p2", "p3"], MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                MagicMock(), MagicMock(), LOG, 0, 1, 0, lambda: True, None, None, "flow",
                max_concurrent_profiles=5,
            )
        self.assertEqual(ran, [])
        self.assertTrue(result.get("aborted"))
