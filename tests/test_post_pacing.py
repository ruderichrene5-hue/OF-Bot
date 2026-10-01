"""The minimum-gap throttle between consecutive posts (post_pacing.py).

Rene, 2026-10-01: the Nikki backlog should drain one post at a time, at least
90 minutes apart, instead of every due row firing back to back because the
posting loop reruns every few minutes.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from adb_bot.automation import post_pacing


class GapMinutesTest(unittest.TestCase):
    def test_default_is_90(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADBBOT_POST_MIN_GAP_MINUTES", None)
            self.assertEqual(post_pacing.min_gap_minutes(), 90.0)

    def test_env_override(self):
        with patch.dict(os.environ, {"ADBBOT_POST_MIN_GAP_MINUTES": "45"}):
            self.assertEqual(post_pacing.min_gap_minutes(), 45.0)

    def test_bad_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"ADBBOT_POST_MIN_GAP_MINUTES": "nope"}):
            self.assertEqual(post_pacing.min_gap_minutes(), 90.0)


class GapElapsedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        os.unlink(self.path)  # start from "never posted"

    def tearDown(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def test_never_posted_is_elapsed(self):
        self.assertTrue(post_pacing.gap_elapsed(now=1_000_000, gap_minutes=90, path=self.path))

    def test_just_posted_is_not_elapsed(self):
        post_pacing.record_post(now=1_000_000, path=self.path)
        self.assertFalse(post_pacing.gap_elapsed(now=1_000_000 + 60, gap_minutes=90, path=self.path))

    def test_elapsed_after_the_full_gap(self):
        post_pacing.record_post(now=1_000_000, path=self.path)
        self.assertFalse(post_pacing.gap_elapsed(now=1_000_000 + 89 * 60, gap_minutes=90, path=self.path))
        self.assertTrue(post_pacing.gap_elapsed(now=1_000_000 + 90 * 60, gap_minutes=90, path=self.path))

    def test_zero_gap_is_always_elapsed(self):
        post_pacing.record_post(now=1_000_000, path=self.path)
        self.assertTrue(post_pacing.gap_elapsed(now=1_000_000 + 1, gap_minutes=0, path=self.path))


if __name__ == "__main__":
    unittest.main()
