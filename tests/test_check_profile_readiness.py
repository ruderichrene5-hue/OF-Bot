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


FULL_CONFIG = {"geelark_tag": "Nikki", "link_url": "https://x.example/go",
              "bio_pool": ["hey ⬇️", "check below ⬇️"]}


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

    def test_no_link_url_in_airtable_skips_without_triggering_anything(self):
        phone = _phone("1", ["IG connected"])
        config = {**FULL_CONFIG, "link_url": ""}

        with mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, config, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "no-link-configured")
        trigger.assert_not_called()

    def test_no_bio_pool_in_airtable_skips_without_triggering_anything(self):
        phone = _phone("1", ["IG connected"])
        config = {**FULL_CONFIG, "bio_pool": []}

        with mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, config, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "no-bio-pool-configured")
        trigger.assert_not_called()

    def test_an_empty_geelark_tag_falls_back_to_the_model_name(self):
        """"Every model's GeeLark tag matches her name" (2026-08-22) -- the
        Airtable field is only for the rare exception, so blank means "use
        the model name", not "not set up"."""
        phone = _phone("1", ["IG connected"], model="Nikki")
        config = {**FULL_CONFIG, "geelark_tag": ""}

        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg") as lookup, \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value=""):
            c.run_one(phone, config, Args(), mock.Mock(), transport=None)

        lookup.assert_called_once_with("Nikki", transport=None)

    def test_a_phone_with_no_model_folder_at_all_still_skips(self):
        phone = _phone("1", ["IG connected"], model="")
        config = {**FULL_CONFIG, "geelark_tag": ""}

        with mock.patch.object(c.library, "picture_url_for_tag") as lookup, \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, config, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "no-geelark-tag-configured")
        lookup.assert_not_called()
        trigger.assert_not_called()

    def test_a_tag_with_no_picture_in_the_library_skips(self):
        """The model is fully set up in Airtable, but nobody has uploaded
        her picture to the GeeLark Library under that tag yet."""
        phone = _phone("1", ["IG connected"])

        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value=""), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "no-picture-in-library")
        trigger.assert_not_called()

    def test_a_completed_task_marks_post_ready(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_COMPLETED}), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "post-ready")
        mark.assert_called_once()

    def test_a_failed_task_never_marks_post_ready(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_FAILED,
                                            "failDesc": "no such user"}), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "failed")
        mark.assert_not_called()

    def test_a_task_still_running_when_we_gave_up_never_marks_post_ready(self):
        """Timing out is not the same as Geelark saying it failed, but it is
        just as much a reason not to claim the profile is ready."""
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_IN_PROGRESS}), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertTrue(out["status"].startswith("unfinished"))
        mark.assert_not_called()

    def test_a_dry_run_never_triggers_a_real_task(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile") as trigger:
            out = c.run_one(phone, FULL_CONFIG, Args(apply=False), mock.Mock(),
                           transport=None)

        self.assertEqual(out["status"], "dry-run")
        trigger.assert_not_called()

    def test_the_bio_is_drawn_from_the_models_own_pool(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="") as trigger:
            c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertIn(trigger.call_args.kwargs["biography"],
                     FULL_CONFIG["bio_pool"])

    def test_nickname_and_username_share_one_generated_handle(self):
        phone = _phone("1", ["IG connected"], model="Nikki")
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="") as trigger:
            c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        kwargs = trigger.call_args.kwargs
        self.assertTrue(kwargs["nickname"])
        self.assertEqual(kwargs["nickname"], kwargs["username"])


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
