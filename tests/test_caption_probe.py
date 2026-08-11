"""The caption experiment is only worth running if it can be trusted to fire for
the right reason.

Every test here asks one of two questions: does the probe open when -- and only
when -- a caption is genuinely implicated, and does it close again cleanly so the
fleet does not drift into posting bare forever. The failure this guards against
is subtle and expensive: MultiLogin flakiness looks exactly like "posting keeps
failing", and a probe that counts it would strip captions off healthy accounts
and report a caption problem that never existed.
"""

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.caption_probe import (
    CaptionProbe,
    ProbeState,
    CAPTION_RELEVANT_FAILURES,
    target_key,
)


class ProbeTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "caption_probe.json"
        self.probe = CaptionProbe(self.path)
        self.key = "handle:someone"

    def fail_with_caption(self, times=1, status="failed"):
        for _ in range(times):
            self.probe.record(self.key, status, had_caption=True)


class TargetKeyTest(TestCase):
    def test_handle_wins_so_two_accounts_on_one_phone_stay_apart(self):
        # The whole point: both accounts share a launch id. Keyed on the phone
        # they would be one streak and the probe would strip captions off the
        # account that never failed.
        first = target_key(account_id="", target_handle="jiji.ll12", launch_id="L1")
        second = target_key(account_id="", target_handle="nikki_697", launch_id="L1")
        self.assertNotEqual(first, second)

    def test_handle_is_normalised(self):
        self.assertEqual(target_key(target_handle="@Someone "),
                         target_key(target_handle="someone"))

    def test_falls_back_through_account_then_profile(self):
        self.assertEqual(target_key(account_id="recA"), "account:recA")
        self.assertEqual(target_key(launch_id="L9"), "profile:L9")
        self.assertEqual(target_key(), "")

    def test_no_identity_means_no_opinion(self):
        probe = CaptionProbe(Path(tempfile.mkdtemp()) / "s.json")
        self.assertFalse(probe.should_drop_caption(""))


class OpeningTheProbeTest(ProbeTestBase):
    def test_three_captioned_failures_open_it(self):
        self.assertFalse(self.probe.should_drop_caption(self.key))
        self.fail_with_caption(2)
        self.assertFalse(self.probe.should_drop_caption(self.key),
                         "two failures must not be enough")
        self.fail_with_caption(1)
        self.assertTrue(self.probe.should_drop_caption(self.key))

    def test_action_block_counts_because_a_caption_can_cause_one(self):
        self.fail_with_caption(3, status="action_block")
        self.assertTrue(self.probe.should_drop_caption(self.key))

    def test_a_captioned_success_resets_the_streak(self):
        self.fail_with_caption(2)
        self.probe.record(self.key, "done", had_caption=True)
        self.fail_with_caption(2)
        self.assertFalse(self.probe.should_drop_caption(self.key))

    def test_infrastructure_failures_never_open_it(self):
        # The load-bearing test. These are the failures this fleet actually has.
        for status in ("adb_connect_failed", "heartbeat_lost", "already_shared",
                       "uncertain", "human_verification", "banned"):
            probe = CaptionProbe(Path(self.tmp.name) / f"{status}.json")
            for _ in range(10):
                probe.record(self.key, status, had_caption=True)
            self.assertFalse(probe.should_drop_caption(self.key),
                             f"{status} must not implicate the caption")

    def test_a_failure_with_no_caption_proves_nothing(self):
        for _ in range(5):
            self.probe.record(self.key, "failed", had_caption=False)
        self.assertFalse(self.probe.should_drop_caption(self.key))

    def test_streak_must_be_consecutive(self):
        self.fail_with_caption(2)
        self.probe.record(self.key, "done", had_caption=True)
        self.fail_with_caption(2)
        self.probe.record(self.key, "done", had_caption=True)
        self.fail_with_caption(2)
        self.assertFalse(self.probe.should_drop_caption(self.key))

    def test_accounts_are_tracked_independently(self):
        self.fail_with_caption(3)
        self.assertTrue(self.probe.should_drop_caption(self.key))
        self.assertFalse(self.probe.should_drop_caption("handle:someone-else"))


class RunningTheProbeTest(ProbeTestBase):
    def setUp(self):
        super().setUp()
        self.fail_with_caption(3)

    def test_exactly_two_bare_attempts_then_captions_return(self):
        self.assertTrue(self.probe.should_drop_caption(self.key))
        self.probe.record(self.key, "failed", had_caption=False)
        self.assertTrue(self.probe.should_drop_caption(self.key),
                        "the second bare attempt is still owed")
        self.probe.record(self.key, "failed", had_caption=False)
        self.assertFalse(self.probe.should_drop_caption(self.key),
                         "captions must come back after two")

    def test_the_streak_is_cleared_when_the_probe_closes(self):
        # Without this the next single captioned failure re-opens the probe on
        # a streak that was already spent, and the account never posts with a
        # caption again.
        self.probe.record(self.key, "failed", had_caption=False)
        self.probe.record(self.key, "failed", had_caption=False)
        self.probe.record(self.key, "failed", had_caption=True)
        self.assertFalse(self.probe.should_drop_caption(self.key))
        self.assertEqual(self.probe.state_for(self.key).streak, 1)

    def test_bare_results_are_kept_for_the_comparison(self):
        self.probe.record(self.key, "done", had_caption=False)
        state = self.probe.record(self.key, "done", had_caption=False)
        self.assertEqual(state.probe_results, ["done", "done"])
        self.assertEqual(state.probe_opened_at_streak, 3)

    def test_a_bare_success_does_not_secretly_forgive_the_caption(self):
        # A post that went out with no text says nothing about the text.
        probe = CaptionProbe(Path(self.tmp.name) / "bare.json")
        probe.record("k", "failed", had_caption=True)
        probe.record("k", "done", had_caption=False)
        self.assertEqual(probe.state_for("k").streak, 1)


class SummaryTest(ProbeTestBase):
    def test_reports_the_verdict_the_experiment_was_run_for(self):
        self.fail_with_caption(3)
        self.probe.record(self.key, "done", had_caption=False)
        self.probe.record(self.key, "done", had_caption=False)
        summary = self.probe.summary()
        self.assertEqual(summary["probing_now"], 0)
        finished = summary["finished"][self.key]
        self.assertEqual(finished["failed_with_caption"], 3)
        self.assertEqual(finished["bare_succeeded"], 2)

    def test_lists_accounts_currently_running_bare(self):
        self.fail_with_caption(3)
        summary = self.probe.summary()
        self.assertEqual(summary["probing_now"], 1)
        self.assertIn(self.key, summary["probing_keys"])


class DurabilityTest(ProbeTestBase):
    def test_state_survives_a_restart(self):
        self.fail_with_caption(3)
        self.assertTrue(CaptionProbe(self.path).should_drop_caption(self.key))

    def test_a_corrupt_file_does_not_stop_posting(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        probe = CaptionProbe(self.path)
        self.assertFalse(probe.should_drop_caption(self.key))
        probe.record(self.key, "failed", had_caption=True)  # must not raise

    def test_an_unwritable_path_is_survivable(self):
        # Instrumentation must never be the reason a post fails.
        probe = CaptionProbe(Path("/proc/nonexistent-dir/state.json"))
        probe.record(self.key, "failed", had_caption=True)
        self.assertFalse(probe.should_drop_caption(self.key))

    def test_thresholds_are_configurable(self):
        probe = CaptionProbe(Path(self.tmp.name) / "cfg.json",
                             failures_before_probe=2, probe_attempts=1)
        probe.record(self.key, "failed", had_caption=True)
        probe.record(self.key, "failed", had_caption=True)
        self.assertTrue(probe.should_drop_caption(self.key))
        probe.record(self.key, "failed", had_caption=False)
        self.assertFalse(probe.should_drop_caption(self.key))
