"""The ledger's job is to make being wrong about verification survivable.

Every test here is really asking one question: after some failure -- a crash, a
dead network, a hasty retry -- can the same clip go out to the same account
twice? If it can, the ledger has not done its job.
"""

import json
import tempfile
import time
from pathlib import Path
from unittest import TestCase

from adb_bot.automation import post_ledger
from adb_bot.automation.post_ledger import (
    PostLedger,
    STATUS_CONFIRMED,
    STATUS_DISPROVED,
    STATUS_SHARED,
    media_fingerprint,
)


class LedgerTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = PostLedger(self.root / "ledger.jsonl")
        self.clip = self.root / "clip.mp4"
        self.clip.write_bytes(b"pretend this is a reel")


class FingerprintTest(LedgerTestBase):
    def test_same_bytes_same_fingerprint(self):
        other = self.root / "renamed.mp4"
        other.write_bytes(self.clip.read_bytes())
        self.assertEqual(media_fingerprint(self.clip), media_fingerprint(other))

    def test_different_bytes_differ(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"a different reel entirely")
        self.assertNotEqual(media_fingerprint(self.clip), media_fingerprint(other))

    def test_missing_file_yields_no_opinion(self):
        self.assertEqual(media_fingerprint(self.root / "nope.mp4"), "")

    def test_unreadable_media_does_not_block_posting(self):
        # No fingerprint means no knowledge. Blocking every post because a file
        # could not be hashed would be worse than the risk it removes.
        self.assertFalse(self.ledger.already_shared("p1", self.root / "nope.mp4"))


class RecordingTest(LedgerTestBase):
    def test_a_share_blocks_the_same_clip_on_the_same_profile(self):
        self.ledger.record_share("p1", self.clip)
        self.assertTrue(self.ledger.already_shared("p1", self.clip))

    def test_other_profiles_are_unaffected(self):
        self.ledger.record_share("p1", self.clip)
        self.assertFalse(self.ledger.already_shared("p2", self.clip),
                         "the same clip on a different account is a different post")

    def test_other_clips_are_unaffected(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"a different reel entirely")
        self.ledger.record_share("p1", self.clip)
        self.assertFalse(self.ledger.already_shared("p1", other))

    def test_the_record_survives_a_new_ledger_object(self):
        # Stands in for the case that matters: the process died after Share.
        self.ledger.record_share("p1", self.clip)
        reopened = PostLedger(self.root / "ledger.jsonl")
        self.assertTrue(reopened.already_shared("p1", self.clip))

    def test_baseline_count_is_carried_for_the_recheck(self):
        from adb_bot.automation.flows.reel_verify import Count
        self.ledger.record_share("p1", self.clip, baseline_count=Count(41, True))
        record = self.ledger.lookup("p1", media_fingerprint(self.clip))
        self.assertEqual(record.baseline_count, 41)
        self.assertTrue(record.baseline_exact)

    def test_a_rounded_baseline_is_marked_inexact(self):
        from adb_bot.automation.flows.reel_verify import Count
        self.ledger.record_share("p1", self.clip, baseline_count=Count(1200, False))
        self.assertFalse(self.ledger.lookup("p1", media_fingerprint(self.clip)).baseline_exact)

    def test_no_baseline_is_recorded_as_unusable(self):
        self.ledger.record_share("p1", self.clip, baseline_count=None)
        self.assertEqual(self.ledger.lookup("p1", media_fingerprint(self.clip)).baseline_count, -1)


class ResolutionTest(LedgerTestBase):
    def _hash(self):
        return media_fingerprint(self.clip)

    def test_confirmed_still_blocks(self):
        # A confirmed post is the strongest possible reason not to send again.
        self.ledger.record_share("p1", self.clip)
        self.ledger.resolve("p1", self._hash(), STATUS_CONFIRMED, "post_count")
        self.assertTrue(self.ledger.already_shared("p1", self.clip))

    def test_only_disproof_re_opens_the_clip(self):
        self.ledger.record_share("p1", self.clip)
        self.ledger.resolve("p1", self._hash(), STATUS_DISPROVED, "error_dialog")
        self.assertFalse(self.ledger.already_shared("p1", self.clip))

    def test_an_unresolved_share_keeps_blocking(self):
        """The central rule. "We don't know" must behave like "it posted", not
        like "it failed" -- treating the two the same is what put reels on
        accounts twice."""
        self.ledger.record_share("p1", self.clip)
        record = self.ledger.lookup("p1", self._hash())
        self.assertEqual(record.status, STATUS_SHARED)
        self.assertTrue(record.blocks_repost())

    def test_resolution_preserves_the_original_share_time(self):
        self.ledger.record_share("p1", self.clip)
        original = self.ledger.lookup("p1", self._hash()).shared_at
        self.ledger.resolve("p1", self._hash(), STATUS_CONFIRMED)
        self.assertEqual(self.ledger.lookup("p1", self._hash()).shared_at, original)

    def test_last_write_wins(self):
        self.ledger.record_share("p1", self.clip)
        self.ledger.resolve("p1", self._hash(), STATUS_DISPROVED)
        self.ledger.resolve("p1", self._hash(), STATUS_CONFIRMED)
        self.assertEqual(self.ledger.lookup("p1", self._hash()).status, STATUS_CONFIRMED)


class DurabilityTest(LedgerTestBase):
    def test_a_torn_line_does_not_take_the_ledger_down(self):
        # Append-only means a crash mid-write costs one line. The rest must
        # still be readable, because the entries around it are what stop a
        # double post.
        self.ledger.record_share("p1", self.clip)
        with (self.root / "ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"profile_id": "p2", "media_ha\n')
        self.assertTrue(self.ledger.already_shared("p1", self.clip))

    def test_unknown_fields_do_not_crash_the_reader(self):
        with (self.root / "ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"profile_id": "p9", "media_hash": "abc",
                                     "from_a_future_version": True}) + "\n")
        # Skipped rather than fatal -- an old build must still read a new file.
        self.assertIsNone(self.ledger.lookup("p9", "abc"))

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(PostLedger(self.root / "absent.jsonl").load(), {})

    def test_write_failure_does_not_raise(self):
        # A post is already in flight by the time we write; a full disk must not
        # turn that into an exception halfway through the flow.
        blocked = PostLedger(self.root / "clip.mp4" / "nested" / "ledger.jsonl")
        self.assertIsNotNone(blocked.record_share("p1", self.clip))


class PendingAndPruneTest(LedgerTestBase):
    def test_pending_lists_only_unresolved_shares(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"second clip")
        self.ledger.record_share("p1", self.clip)
        self.ledger.record_share("p1", other)
        self.ledger.resolve("p1", media_fingerprint(other), STATUS_CONFIRMED)
        pending = self.ledger.pending()
        self.assertEqual([r.media_hash for r in pending], [media_fingerprint(self.clip)])

    def test_pending_can_skip_shares_that_are_too_fresh(self):
        self.ledger.record_share("p1", self.clip)
        self.assertEqual(self.ledger.pending(older_than_seconds=900), [])

    def test_prune_drops_old_records_and_collapses_history(self):
        self.ledger.record_share("p1", self.clip)
        self.ledger.resolve("p1", media_fingerprint(self.clip), STATUS_CONFIRMED)
        # Two appends, one key.
        raw = (self.root / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(raw), 2)
        self.ledger.prune(max_age_days=365)
        collapsed = (self.root / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(collapsed), 1)
        self.assertTrue(self.ledger.already_shared("p1", self.clip))

    def test_prune_removes_records_past_the_cutoff(self):
        self.ledger.record_share("p1", self.clip)
        aged = self.ledger.lookup("p1", media_fingerprint(self.clip))
        aged.shared_at = time.time() - (60 * 86400)
        self.ledger._append(aged)
        dropped = self.ledger.prune(max_age_days=30)
        self.assertEqual(dropped, 1)
        self.assertFalse(self.ledger.already_shared("p1", self.clip))


class FlowWiringTest(TestCase):
    """The ledger only works if the flow writes it at the right moment."""

    def test_share_is_recorded_before_verification_runs(self):
        import inspect
        from adb_bot.automation.flows import instagram_reel
        src = inspect.getsource(instagram_reel.InstagramReelUploadU2Flow.run)
        recorded_at = src.index("ledger.record_share(")
        verified_at = src.index("reel_verify.verify_reel_posted(")
        self.assertLess(recorded_at, verified_at,
                        "a crash during verification would leave no record that Share was tapped")

    def test_the_flow_checks_the_ledger_before_pushing_media(self):
        import inspect
        from adb_bot.automation.flows import instagram_reel
        src = inspect.getsource(instagram_reel.InstagramReelUploadU2Flow.run)
        checked_at = src.index("prior.blocks_repost()")
        pushed_at = src.index("_adb_push_media_to_device(")
        self.assertLess(checked_at, pushed_at,
                        "checking after the push wastes a 20MB upload on a post we must refuse")
