"""Which phones get checked, and when `Post Ready` actually gets applied.

`Post Ready` means Bio, Link AND Profile Picture all done together via one
Geelark `instagramEdit` task -- never off a partial request, and never
before Geelark itself reports the task Completed.
"""

import unittest
from unittest import mock

from adb_bot.automation import check_profile_readiness as c
from adb_bot.clients.geelark import rpa


def _phone(phone_id, tags, model="Nikki"):
    return {"id": phone_id, "serialName": f"p{phone_id}",
           "tags": [{"name": t} for t in tags], "group": {"name": model}}


class PhonesToCheckTest(unittest.TestCase):
    def test_ig_connected_without_post_ready_is_included(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.GeelarkPhoneClient, "list_phones",
                               return_value=[phone]):
            out = c.phones_to_check()
        self.assertEqual([p["id"] for p in out], ["1"])

    def test_not_yet_ig_connected_is_skipped(self):
        phone = _phone("1", ["new profile"])
        with mock.patch.object(c.GeelarkPhoneClient, "list_phones",
                               return_value=[phone]):
            out = c.phones_to_check()
        self.assertEqual(out, [])

    def test_already_post_ready_is_skipped(self):
        phone = _phone("1", ["IG connected", "Post Ready"])
        with mock.patch.object(c.GeelarkPhoneClient, "list_phones",
                               return_value=[phone]):
            out = c.phones_to_check()
        self.assertEqual(out, [])


class Args:
    def __init__(self, apply=True, task_timeout=300):
        self.apply = apply
        self.task_timeout = task_timeout


class RunOneTest(unittest.TestCase):
    def setUp(self):
        self.phones_patch = mock.patch.object(c, "GeelarkPhoneClient")
        self.fake_phones_cls = self.phones_patch.start()
        self.addCleanup(self.phones_patch.stop)

        self.link_patch = mock.patch.object(c, "LINK_URL", "https://x.example/go")
        self.link_patch.start()
        self.addCleanup(self.link_patch.stop)

        self.picture_patch = mock.patch.object(
            c.model_media, "picture_url_for",
            return_value="https://example.com/nikki.jpg")
        self.picture_patch.start()
        self.addCleanup(self.picture_patch.stop)

    def test_no_picture_configured_skips_without_triggering_anything(self):
        """A model with no picture yet must never get a two-of-three
        instagramEdit request -- the field is simply not ready to check."""
        phone = _phone("1", ["IG connected"])

        with mock.patch.object(c.model_media, "picture_url_for",
                               return_value=""), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "no-picture-configured")
        trigger.assert_not_called()

    def test_no_link_url_configured_skips_without_triggering_anything(self):
        phone = _phone("1", ["IG connected"])

        with mock.patch.object(c, "LINK_URL", ""), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "no-link-configured")
        trigger.assert_not_called()

    def test_a_completed_task_marks_post_ready(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_COMPLETED}), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "post-ready")
        mark.assert_called_once()

    def test_a_failed_task_never_marks_post_ready(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_FAILED,
                                            "failDesc": "no such user"}), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "failed")
        mark.assert_not_called()

    def test_a_task_still_running_when_we_gave_up_never_marks_post_ready(self):
        """Timing out is not the same as Geelark saying it failed, but it is
        just as much a reason not to claim the profile is ready."""
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_IN_PROGRESS}), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, Args(), mock.Mock(), transport=None)

        self.assertTrue(out["status"].startswith("unfinished"))
        mark.assert_not_called()

    def test_a_dry_run_never_triggers_a_real_task(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, Args(apply=False), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "dry-run")
        trigger.assert_not_called()


class _FakeTags:
    def __init__(self, existing):
        self.existing = dict(existing)

    def tag_ids_by_name(self, refresh=False):
        return dict(self.existing)

    def ensure_tag(self, name, color="blue"):
        self.existing.setdefault(name, f"id-{name}")
        return self.existing[name]


class MarkPostReadyTest(unittest.TestCase):
    def test_keeps_existing_tags_and_adds_post_ready(self):
        phone = _phone("1", ["IG connected"])
        fake_tags = _FakeTags({"IG connected": "id-connected"})

        with mock.patch.object(c, "GeelarkPhoneClient") as fake_phones_cls, \
             mock.patch.object(c, "GeelarkTagClient", return_value=fake_tags):
            c.mark_post_ready(phone, transport=None, logger=mock.Mock())

        call = fake_phones_cls.return_value.update_phone.call_args
        by_id = {v: k for k, v in fake_tags.existing.items()}
        names = {by_id.get(i, i) for i in (call.kwargs["tag_ids"] or [])}
        self.assertEqual(names, {"IG connected", "Post Ready"})


if __name__ == "__main__":
    unittest.main()
