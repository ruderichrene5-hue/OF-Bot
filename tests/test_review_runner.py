"""Who the review recheck looks at, and what it does with a clean screen."""

import unittest

from adb_bot.automation import review_runner


def _profile(name, tags=(), pid="1"):
    return {"serial_name": name, "id": pid, "tags": list(tags)}


class SelectionTest(unittest.TestCase):
    def test_review_tagged_model_profiles_are_selected(self):
        to_check = review_runner.select([
            _profile("Nikki 21", ["review", "Human Verification"]),
            _profile("Nikki 22", ["Issue"]),
        ])
        self.assertEqual([p["serial_name"] for p in to_check], ["Nikki 21"])

    def test_staging_phones_are_not_model_profiles(self):
        to_check = review_runner.select([
            _profile("Blank (12)", ["review"]),
            _profile("Nikki 21", ["review"]),
        ])
        self.assertEqual([p["serial_name"] for p in to_check], ["Nikki 21"])

    def test_untagged_profiles_are_not_selected(self):
        self.assertEqual(review_runner.select([_profile("Nikki 21", [])]), [])


class FakeTags:
    def __init__(self, known=("review", "Human Verification", "Setup", "Active / Posting")):
        self.known = {n.lower(): f"id-{n}" for n in known}
        self.unassigned = []
        self.assigned = []

    def tag_ids_by_name(self, refresh=False):
        return dict(self.known)

    def unassign(self, profile_id, tag_ids):
        self.unassigned.append((profile_id, tuple(tag_ids)))
        return True

    def assign(self, profile_id, tag_ids):
        self.assigned.append((profile_id, tuple(tag_ids)))
        return True


class _Logger:
    def info(self, *a):
        pass
    warning = error = info


class RetagTest(unittest.TestCase):
    """A clean screen means the challenge is over -- what happens next
    depends on whether the account was ever finished."""

    def test_a_set_up_account_resumes_active_posting(self):
        item = _profile("Nikki 21", ["review", "Human Verification", "Setup"], pid="L1")
        outcome = {"id": "L1", "name": "Nikki 21", "verdict": "clean"}
        tags = FakeTags()
        review_runner._retag(tags, _Logger(), item, outcome)
        self.assertEqual(outcome["verdict"], "clean-resumed")
        self.assertIn(("L1", ("id-review",)), tags.unassigned)
        self.assertIn(("L1", ("id-Human Verification",)), tags.unassigned)
        self.assertIn(("L1", ("id-Active / Posting",)), tags.assigned)

    def test_an_unfinished_account_is_not_marked_active(self):
        """No `Setup` tag means the account was never finished -- `review`
        still comes off (the challenge is over), but it must not look like a
        working, postable account when it isn't one."""
        item = _profile("Nikki 21", ["review", "Human Verification"], pid="L1")
        outcome = {"id": "L1", "name": "Nikki 21", "verdict": "clean"}
        tags = FakeTags()
        review_runner._retag(tags, _Logger(), item, outcome)
        self.assertEqual(outcome["verdict"], "needs-setup")
        self.assertIn(("L1", ("id-review",)), tags.unassigned)
        self.assertEqual(tags.assigned, [])


if __name__ == "__main__":
    unittest.main()
