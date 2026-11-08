from unittest import TestCase

from adb_bot.automation.observer_dashboard import align, account_metrics
from adb_bot.automation.ig_observer import parse_ig_number, merge_overlap


class AlignTest(TestCase):
    def test_two_new_reels_shift_the_list(self):
        prev = [100, 200, 300, 400, 500, 600]
        cur = [50, 80, 110, 210, 305, 400, 505, 600]
        k, gain = align(prev, cur)
        self.assertEqual(k, 2)
        self.assertEqual(gain, 50 + 80 + 10 + 10 + 5 + 0 + 5 + 0)

    def test_no_new_reels(self):
        k, gain = align([10, 20, 30, 40], [12, 25, 30, 41])
        self.assertEqual((k, gain), (0, 2 + 5 + 0 + 1))

    def test_unrelated_lists_do_not_align(self):
        k, _ = align([1000, 2000, 3000, 4000], [5, 6, 7, 8])
        self.assertIsNone(k)


class MetricsTest(TestCase):
    def test_gain_and_follower_delta(self):
        day = 86400
        snaps = [
            {"handle": "a", "at": 0, "status": "ok", "followers": 100, "reels": [{"pos": 1, "views": 10}, {"pos": 2, "views": 20}, {"pos": 3, "views": 30}]},
            {"handle": "a", "at": day, "status": "ok", "followers": 110, "reels": [{"pos": 1, "views": 5}, {"pos": 2, "views": 15}, {"pos": 3, "views": 25}, {"pos": 4, "views": 30}]},
        ]
        m = account_metrics(snaps, now=day + 60)
        self.assertEqual(m["f_d1"], 10)
        self.assertEqual(m["v24"][0], 5 + 5 + 5 + 0)
        self.assertEqual(m["v24"][1], 1)


class ParseTest(TestCase):
    def test_german_and_suffix_numbers(self):
        self.assertEqual(parse_ig_number("1.037")[0], 1037)
        self.assertEqual(parse_ig_number("10,6K")[0], 10600)
        self.assertEqual(parse_ig_number("View Count 32,5K.")[0], 32500)

    def test_merge_overlap_whole_rows(self):
        self.assertEqual(merge_overlap([1, 2, 3, 4, 5, 6], [4, 5, 6, 7, 8, 9]), 3)
