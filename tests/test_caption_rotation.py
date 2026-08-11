"""The rotation has one obvious job and one that actually matters.

Obvious: walk the pool so an account does not repeat a caption. The one that
matters: keep accounts *out of step*. All ~270 accounts post on the same slot
grid, so a rotation that starts everyone at CAP-001 publishes one identical
sentence across the whole fleet within minutes -- a much louder pattern than the
captionless posts this replaces.
"""

import tempfile
from collections import Counter
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.caption_rotation import CaptionRotation, _stable_offset


def pool(n=500):
    return [{"record_id": f"rec{i:03d}", "caption_id": f"CAP-{i:03d}", "text": f"text {i}"}
            for i in range(1, n + 1)]


class RotationTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rotation.json"
        self.rotation = CaptionRotation(self.path)
        self.pool = pool()


class WalkingThePoolTest(RotationTestBase):
    def test_consecutive_calls_advance_by_one(self):
        first = self.rotation.next_for("a", self.pool)
        second = self.rotation.next_for("a", self.pool)
        i = self.pool.index(first)
        self.assertEqual(second, self.pool[(i + 1) % len(self.pool)])

    def test_the_whole_pool_is_used_before_anything_repeats(self):
        seen = [self.rotation.next_for("a", self.pool)["caption_id"]
                for _ in range(len(self.pool))]
        self.assertEqual(len(set(seen)), len(self.pool))

    def test_it_wraps_instead_of_running_out(self):
        seen = [self.rotation.next_for("a", self.pool)["caption_id"]
                for _ in range(len(self.pool) + 1)]
        self.assertEqual(seen[0], seen[-1])

    def test_peek_does_not_advance(self):
        peeked = self.rotation.peek_for("a", self.pool)
        self.assertEqual(peeked, self.rotation.peek_for("a", self.pool))
        self.assertEqual(peeked, self.rotation.next_for("a", self.pool))


class StaggerTest(RotationTestBase):
    def test_the_fleet_does_not_post_the_same_caption_at_once(self):
        # The test this module exists for.
        keys = [f"account:rec{i}" for i in range(270)]
        first_round = [self.rotation.next_for(k, self.pool)["caption_id"] for k in keys]
        worst = Counter(first_round).most_common(1)[0][1]
        self.assertLess(worst, 10,
                        "too many accounts share a caption in the same slot")
        self.assertGreater(len(set(first_round)), 200)

    def test_the_offset_is_stable_across_processes(self):
        # hashlib, not hash(): PYTHONHASHSEED would otherwise move every
        # account's caption on every restart.
        self.assertEqual(_stable_offset("account:recA", 500),
                         _stable_offset("account:recA", 500))

    def test_different_keys_start_in_different_places(self):
        offsets = {_stable_offset(f"k{i}", 500) for i in range(50)}
        self.assertGreater(len(offsets), 40)


class EdgeCaseTest(RotationTestBase):
    def test_an_empty_pool_yields_no_caption(self):
        self.assertIsNone(self.rotation.next_for("a", []))

    def test_a_blank_key_yields_no_caption(self):
        self.assertIsNone(self.rotation.next_for("", self.pool))

    def test_a_single_caption_pool_keeps_working(self):
        one = pool(1)
        self.assertEqual(self.rotation.next_for("a", one)["caption_id"], "CAP-001")
        self.assertEqual(self.rotation.next_for("a", one)["caption_id"], "CAP-001")

    def test_state_survives_a_restart(self):
        first = self.rotation.next_for("a", self.pool)
        self.rotation.save()
        second = CaptionRotation(self.path).next_for("a", self.pool)
        self.assertNotEqual(first["caption_id"], second["caption_id"])

    def test_a_corrupt_state_file_does_not_stop_the_queue(self):
        self.path.write_text("not json at all", encoding="utf-8")
        self.assertIsNotNone(CaptionRotation(self.path).next_for("a", self.pool))

    def test_an_unwritable_path_is_survivable(self):
        rotation = CaptionRotation(Path("/proc/nonexistent-dir/rot.json"))
        self.assertIsNotNone(rotation.next_for("a", self.pool))
        self.assertFalse(rotation.save())
