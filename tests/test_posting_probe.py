"""Who the supervised posting probe will post on.

This tool posts with the Needs Human Check gate off, so the set of profiles it
resolves is the whole safety story: a name that quietly matches the wrong
profile, or quietly matches nothing, is a post on an account nobody chose.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from adb_bot.automation import posting_probe


class FakeAirtable:
    def __init__(self, rows):
        self._rows = rows

    def posting_profiles(self):
        return self._rows


def _row(name, launch_id, needs_human=False, reason=""):
    return {"name": name, "launch_id": launch_id, "needs_human": needs_human,
            "reason": reason, "status": "Active"}


class ResolveTest(unittest.TestCase):
    def test_names_become_launch_ids(self):
        at = FakeAirtable([_row("Jil 8", "111"), _row("Laila 4", "222"),
                           _row("Nikki 12", "333")])
        ids, missing, rows = posting_probe.resolve(at, ["Jil 8", "Nikki 12"])
        self.assertEqual(sorted(ids), ["111", "333"])
        self.assertEqual(missing, [])
        self.assertEqual(len(rows), 2)

    def test_a_name_is_matched_however_it_was_typed(self):
        at = FakeAirtable([_row("Jil 8", "111")])
        ids, missing, _ = posting_probe.resolve(at, ["  jil 8 "])
        self.assertEqual(ids, ["111"])
        self.assertEqual(missing, [])

    def test_an_unmatched_name_is_reported_not_dropped(self):
        """A run that silently posts on four of the five profiles asked for is
        a run whose result cannot be read."""
        at = FakeAirtable([_row("Jil 8", "111")])
        ids, missing, _ = posting_probe.resolve(at, ["Jil 8", "Jil 99"])
        self.assertEqual(ids, ["111"])
        self.assertEqual(missing, ["Jil 99"])

    def test_a_profile_with_no_launch_id_cannot_be_posted_on(self):
        """There is nothing to launch, so it is a miss, not a target."""
        at = FakeAirtable([_row("Jil 8", "")])
        ids, missing, _ = posting_probe.resolve(at, ["Jil 8"])
        self.assertEqual(ids, [])
        self.assertEqual(missing, ["Jil 8"])

    def test_no_names_resolve_to_nothing_rather_than_everything(self):
        at = FakeAirtable([_row("Jil 8", "111"), _row("Laila 4", "222")])
        ids, missing, _ = posting_probe.resolve(at, [])
        self.assertEqual(ids, [])
        self.assertEqual(missing, [])


class ClearedListTest(unittest.TestCase):
    """`--cleared` is the default target: profiles a screen read positively
    called healthy. Anything else needs a person to type the name."""

    def test_the_newest_reviews_cleared_names_are_used(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "review-20260813-233355.json").write_text(json.dumps(
                {"cleared": [{"name": "Old One", "id": "1"}]}))
            (root / "review-20260814-120000.json").write_text(json.dumps(
                {"cleared": [{"name": "Jil 8", "id": "2"},
                             {"name": "Laila 4", "id": "3"}]}))
            with mock.patch.object(posting_probe, "REVIEW_DIR", root):
                self.assertEqual(posting_probe.latest_cleared(),
                                 ["Jil 8", "Laila 4"])

    def test_no_reviews_yet_is_an_empty_list_not_a_crash(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(posting_probe, "REVIEW_DIR", Path(tmp)):
                self.assertEqual(posting_probe.latest_cleared(), [])

    def test_a_review_that_cleared_nobody_offers_nobody(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "review-20260814-120000.json").write_text(
                json.dumps({"cleared": []}))
            with mock.patch.object(posting_probe, "REVIEW_DIR", root):
                self.assertEqual(posting_probe.latest_cleared(), [])


if __name__ == "__main__":
    unittest.main()
