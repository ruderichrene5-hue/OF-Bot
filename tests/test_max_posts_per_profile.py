"""The per-phone posting cap: a cleared profile must not empty its backlog.

Unflagging a parked profile makes every queue row it accumulated due at once,
and `run_profile` works through everything due on a phone sequentially on one
launch. On 2026-08-27 clearing 21 false flags would have made 257 rows due, 26
of them on `Kathi 9` -- the shape of the 08-14 `Katja 3` incident. The cap is
what turns that into a drain over days.
"""

import os
import unittest
from unittest.mock import patch

from adb_bot.config import settings


class MaxPostsPerProfileSetting(unittest.TestCase):
    def test_default_is_three(self):
        self.assertEqual(settings.DEFAULT_MAX_POSTS_PER_PROFILE, 3)
        with patch.object(settings, "load_settings", return_value={}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADBBOT_MAX_POSTS_PER_PROFILE", None)
            self.assertEqual(settings.get_saved_max_posts_per_profile(), 3)

    def test_env_override(self):
        with patch.object(settings, "load_settings", return_value={}), \
             patch.dict(os.environ, {"ADBBOT_MAX_POSTS_PER_PROFILE": "5"}):
            self.assertEqual(settings.get_saved_max_posts_per_profile(), 5)

    def test_saved_setting_wins_over_env_and_accepts_a_json_number(self):
        with patch.object(settings, "load_settings",
                          return_value={"max_posts_per_profile": 2}), \
             patch.dict(os.environ, {"ADBBOT_MAX_POSTS_PER_PROFILE": "5"}):
            self.assertEqual(settings.get_saved_max_posts_per_profile(), 2)

    def test_zero_means_uncapped_and_is_preserved(self):
        # `plan_posting_queue` treats a falsey cap as "no cap", so 0 has to
        # survive as 0 rather than being clamped up to 1 the way the live-phone
        # ceiling is -- that is the documented escape hatch back to the old
        # behaviour without a code change.
        with patch.object(settings, "load_settings",
                          return_value={"max_posts_per_profile": 0}):
            self.assertEqual(settings.get_saved_max_posts_per_profile(), 0)

    def test_nonsense_falls_back_to_the_default(self):
        with patch.object(settings, "load_settings",
                          return_value={"max_posts_per_profile": "abc"}):
            self.assertEqual(settings.get_saved_max_posts_per_profile(), 3)


if __name__ == "__main__":
    unittest.main()
