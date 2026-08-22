"""Which phones get checked, and what a check actually writes back."""

import unittest
from unittest import mock

from adb_bot.automation import check_profile_readiness as c
from adb_bot.automation.flows.instagram import ProfileReadiness


def _phone(phone_id, tags):
    return {"id": phone_id, "serialName": f"p{phone_id}",
           "tags": [{"name": t} for t in tags], "group": {"name": "Nikki"}}


class PhonesToCheckTest(unittest.TestCase):
    def test_ig_connected_without_post_ready_is_included(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.GeelarkPhoneClient, "list_phones",
                               return_value=[phone]):
            out = c.phones_to_check()
        self.assertEqual([p["id"] for p in out], ["1"])

    def test_not_yet_ig_connected_is_skipped(self):
        """A `new profile` or `Signup Failed` phone has no Instagram
        account yet -- nothing to check."""
        phone = _phone("1", ["new profile"])
        with mock.patch.object(c.GeelarkPhoneClient, "list_phones",
                               return_value=[phone]):
            out = c.phones_to_check()
        self.assertEqual(out, [])

    def test_already_post_ready_is_skipped(self):
        """Re-checking a finished profile burns a launch for nothing."""
        phone = _phone("1", ["IG connected", "Bio Done", "Link Done",
                             "Post Ready"])
        with mock.patch.object(c.GeelarkPhoneClient, "list_phones",
                               return_value=[phone]):
            out = c.phones_to_check()
        self.assertEqual(out, [])


class _FakeTags:
    def __init__(self, existing):
        self.existing = dict(existing)
        self.ensured = []

    def tag_ids_by_name(self, refresh=False):
        return dict(self.existing)

    def ensure_tag(self, name, color="blue"):
        self.existing.setdefault(name, f"id-{name}")
        self.ensured.append((name, color))
        return self.existing[name]


class ApplyTagsTest(unittest.TestCase):
    def setUp(self):
        self.phones_patch = mock.patch.object(c, "GeelarkPhoneClient")
        self.fake_phones_cls = self.phones_patch.start()
        self.addCleanup(self.phones_patch.stop)

    def _updated_names(self, fake_tags):
        call = self.fake_phones_cls.return_value.update_phone.call_args
        by_id = {v: k for k, v in fake_tags.existing.items()}
        return {by_id.get(i, i) for i in (call.kwargs["tag_ids"] or [])}

    def test_bio_and_link_both_add_their_tags_without_dropping_ig_connected(self):
        phone = _phone("1", ["IG connected"])
        fake_tags = _FakeTags({"IG connected": "id-connected"})
        with mock.patch.object(c, "GeelarkTagClient",
                        return_value=fake_tags):
            c.apply_tags(phone, ProfileReadiness(bio=True, link=True),
                        transport=None, logger=mock.Mock())

        names = self._updated_names(fake_tags)
        self.assertEqual(names, {"IG connected", "Bio Done", "Link Done"})

    def test_only_bio_done_does_not_add_link_done(self):
        phone = _phone("1", ["IG connected"])
        fake_tags = _FakeTags({"IG connected": "id-connected"})
        with mock.patch.object(c, "GeelarkTagClient",
                        return_value=fake_tags):
            c.apply_tags(phone, ProfileReadiness(bio=True, link=False),
                        transport=None, logger=mock.Mock())

        names = self._updated_names(fake_tags)
        self.assertIn("Bio Done", names)
        self.assertNotIn("Link Done", names)

    def test_never_sets_post_ready_itself(self):
        """Picture detection does not exist yet -- claiming a profile is
        `Post Ready` off two of three checks would be worse than not
        tagging it at all."""
        phone = _phone("1", ["IG connected"])
        fake_tags = _FakeTags({"IG connected": "id-connected"})
        with mock.patch.object(c, "GeelarkTagClient",
                        return_value=fake_tags):
            c.apply_tags(phone, ProfileReadiness(bio=True, link=True),
                        transport=None, logger=mock.Mock())

        names = self._updated_names(fake_tags)
        self.assertNotIn("Post Ready", names)
        self.assertNotIn("Post Ready", [n for n, _ in fake_tags.ensured])


if __name__ == "__main__":
    unittest.main()
