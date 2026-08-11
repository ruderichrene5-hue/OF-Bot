"""The probe is only real if it reaches the two places that decide a post.

The unit tests prove the state machine. These prove it is actually consulted
when the plan is built, and actually fed when the result is written -- and that
switching it off leaves posting byte-for-byte as it was.
"""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase

from adb_bot.clients import airtable as at
from adb_bot.automation.caption_probe import CaptionProbe
from adb_bot.automation.posting_planner import plan_posting_queue
from adb_bot.automation.posting_runner import apply_post_result

from tests.test_posting_queue import queue_row, base_lookups, FakePostClient, NOW


class WiringTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.probe = CaptionProbe(Path(self.tmp.name) / "probe.json")
        self.key = "account:recAcc1"

    def plan(self, rows=None, probe=None):
        d = base_lookups()
        return plan_posting_queue(
            rows if rows is not None else [queue_row()],
            d["accounts"], d["profiles"], d["variants"], d["captions"],
            now=NOW, caption_probe=probe)


class PlannerConsultsTheProbeTest(WiringTestBase):
    def test_caption_is_kept_when_nothing_has_failed(self):
        item = self.plan(probe=self.probe).to_post[0]
        self.assertEqual(item.caption, "hello world")
        self.assertIsNone(item.caption_withheld)

    def test_caption_is_withheld_once_the_probe_opens(self):
        for _ in range(3):
            self.probe.record(self.key, "failed", had_caption=True)
        item = self.plan(probe=self.probe).to_post[0]
        self.assertIsNone(item.caption, "the flow must not be able to type it")
        self.assertEqual(item.caption_withheld, "hello world",
                         "but we must still know which caption was skipped")

    def test_no_probe_means_the_old_behaviour_exactly(self):
        for _ in range(10):
            self.probe.record(self.key, "failed", had_caption=True)
        item = self.plan(probe=None).to_post[0]
        self.assertEqual(item.caption, "hello world")

    def test_a_row_with_no_caption_is_untouched(self):
        item = self.plan([queue_row(caption=None)], probe=self.probe).to_post[0]
        self.assertIsNone(item.caption)
        self.assertIsNone(item.caption_withheld)


class RunnerFeedsTheProbeTest(WiringTestBase):
    def _apply(self, status, probe=None, rows=None):
        airtable = FakePostClient()
        item = self.plan(rows, probe=probe).to_post[0]
        apply_post_result(airtable, item, status, caption_probe=probe)
        return airtable, item

    def test_a_captioned_failure_is_counted(self):
        for _ in range(3):
            self._apply("failed", probe=self.probe)
        self.assertTrue(self.probe.should_drop_caption(self.key))

    def test_an_mlx_failure_is_not_counted(self):
        for _ in range(10):
            self._apply("adb_connect_failed", probe=self.probe)
        self.assertFalse(self.probe.should_drop_caption(self.key))

    def test_the_run_log_says_why_the_caption_vanished(self):
        for _ in range(2):
            self._apply("failed", probe=self.probe)
        airtable, _ = self._apply("failed", probe=self.probe)
        note = airtable.run_logs[-1][3]
        self.assertIn("caption probe opened", note)
        self.assertIn("3 captioned failures", note)

    def test_the_run_log_marks_each_bare_attempt(self):
        for _ in range(3):
            self._apply("failed", probe=self.probe)
        airtable, item = self._apply("done", probe=self.probe)
        self.assertIsNone(item.caption)
        self.assertIn("WITHOUT caption", airtable.run_logs[-1][3])

    def test_the_run_log_reports_the_verdict_at_the_end(self):
        for _ in range(3):
            self._apply("failed", probe=self.probe)
        self._apply("done", probe=self.probe)
        airtable, _ = self._apply("done", probe=self.probe)
        note = airtable.run_logs[-1][3]
        self.assertIn("caption probe complete", note)
        self.assertIn("2/2", note)
        self.assertIn("captions resume", note)

    def test_captions_come_back_after_the_two_attempts(self):
        for _ in range(3):
            self._apply("failed", probe=self.probe)
        self._apply("failed", probe=self.probe)
        self._apply("failed", probe=self.probe)
        self.assertEqual(self.plan(probe=self.probe).to_post[0].caption, "hello world")

    def test_the_normal_write_back_is_unchanged(self):
        # A retryable failure must still bump Retry Count with the probe wired.
        airtable, _ = self._apply("failed", probe=self.probe)
        self.assertEqual(airtable.post_marks[-1][0], "recQ1")
        self.assertEqual(airtable.post_marks[-1][1], at.POST_STATUS_FAILED)

    def test_a_broken_probe_never_breaks_the_write_back(self):
        class Exploding:
            failures_before_probe = 3
            probe_attempts = 2

            def state_for(self, key):
                raise RuntimeError("boom")

            def should_drop_caption(self, key):
                return False

            def record(self, *a, **k):
                raise RuntimeError("boom")

        airtable = FakePostClient()
        item = self.plan(probe=None).to_post[0]
        self.assertTrue(apply_post_result(airtable, item, "done", caption_probe=Exploding()))
        self.assertEqual(airtable.post_marks[-1][1], at.POST_STATUS_POSTED)
