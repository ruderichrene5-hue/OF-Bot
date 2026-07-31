"""Media is only consumed when the post actually went out.

Pushing a clip to the phone is not the same as posting it. The flows used to
move the file into `used/` right after the adb push, so any later failure
silently ate the video -- it could never be retried. These pin the new contract:
`mark_used` happens on the success path only.
"""

import inspect
import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.flows import instagram, instagram_reel, instagram_story
from adb_bot.automation.flows.story_media import StoryMediaQueueManager


class QueueBehaviourTest(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.clip = self.root / "clip1.mp4"
        self.clip.write_bytes(b"video")
        self.queue = StoryMediaQueueManager(self.root)

    def test_unused_clip_stays_put(self):
        media = self.queue.get_next_media()
        self.assertEqual(media, self.clip)
        # Not marking it used must leave the file exactly where it was, so the
        # next run picks it up again.
        self.assertTrue(self.clip.exists())
        self.assertFalse((self.root / "used" / "clip1.mp4").exists())

    def test_marking_used_moves_it(self):
        media = self.queue.get_next_media()
        self.assertTrue(self.queue.mark_used(media))
        self.assertFalse(self.clip.exists())
        self.assertTrue((self.root / "used" / "clip1.mp4").exists())

    def test_a_fresh_queue_reoffers_an_unconsumed_clip(self):
        # What a retry after a failed run looks like.
        self.assertEqual(self.queue.get_next_media(), self.clip)
        self.assertEqual(StoryMediaQueueManager(self.root).get_next_media(), self.clip)

    def test_a_fresh_queue_skips_a_consumed_clip(self):
        self.queue.mark_used(self.queue.get_next_media())
        self.assertIsNone(StoryMediaQueueManager(self.root).get_next_media())


class CommitOnSuccessOnlyTest(TestCase):
    """Structural checks over the flow sources: every `mark_used` must sit inside
    a deferred `commit_media_used`, and each flow must have both a commit and a
    keep-for-retry path."""

    FLOWS = (
        ("instagram.py", instagram),
        ("instagram_reel.py", instagram_reel),
        ("instagram_story.py", instagram_story),
    )

    def _source(self, module) -> str:
        return inspect.getsource(module)

    def test_mark_used_is_never_called_directly_on_the_push_path(self):
        for name, module in self.FLOWS:
            src = self._source(module)
            for line_no, line in enumerate(src.splitlines(), 1):
                if "media_queue.mark_used(" not in line:
                    continue
                # Walk back to the nearest enclosing def; it must be the
                # deferred commit helper, not the push routine.
                indent = len(line) - len(line.lstrip())
                enclosing = None
                for prev in reversed(src.splitlines()[:line_no - 1]):
                    if prev.strip().startswith("def ") and (len(prev) - len(prev.lstrip())) < indent:
                        enclosing = prev.strip()
                        break
                self.assertIsNotNone(enclosing, f"{name}:{line_no}")
                self.assertTrue(
                    enclosing.startswith("def commit_media_used"),
                    f"{name}:{line_no} calls mark_used inside {enclosing!r}, "
                    "not the deferred commit helper",
                )

    def test_every_flow_has_a_commit_and_a_retry_path(self):
        for name, module in self.FLOWS:
            src = self._source(module)
            if "media_queue.mark_used(" not in src:
                continue
            self.assertIn("commit_media_used()", src, name)
            self.assertIn("keep_media_for_retry(", src, name)

    def test_commit_count_matches_helper_count(self):
        # One commit call site per flow that defines the helper -- a helper that
        # is defined but never invoked would silently never consume media.
        for name, module in self.FLOWS:
            lines = [line.strip() for line in self._source(module).splitlines()]
            defined = sum(1 for line in lines if line.startswith("def commit_media_used("))
            called = sum(1 for line in lines if line == "commit_media_used()")
            self.assertEqual(defined, called, f"{name}: {defined} helper(s), {called} call(s)")
