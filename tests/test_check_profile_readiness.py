"""Which phones get checked, and when `Post Ready` actually gets applied.

`Post Ready` means Bio, Link AND Profile Picture all done together via one
Geelark `instagramEdit` task -- never off a partial request, and never
before Geelark itself reports the task Completed.
"""

import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from adb_bot.automation import check_profile_readiness as c
from adb_bot.clients.geelark import rpa


def _fake_root(screen_text: str) -> ET.Element:
    return ET.fromstring(
        f'<hierarchy><node text="{screen_text}" bounds="[0,0][1,1]"/></hierarchy>')


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

    def test_a_completed_task_verified_on_device_marks_post_ready(self):
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_COMPLETED}), \
             mock.patch.object(c, "verify_setup_on_device",
                               return_value=(True, "ok")), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "post-ready")
        mark.assert_called_once()

    def test_a_completed_task_that_fails_on_device_verification_never_marks_post_ready(self):
        """Geelark's own two real accounts (2026-08-23) got a "Completed"
        task result while the real device sat behind a human-verification
        checkpoint -- this is the case that finding demands a test for."""
        phone = _phone("1", ["IG connected"])
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="t1"), \
             mock.patch.object(c.rpa, "wait_for_task",
                               return_value={"status": rpa.STATUS_COMPLETED}), \
             mock.patch.object(c, "verify_setup_on_device",
                               return_value=(False, "blocked-human_verification")), \
             mock.patch.object(c, "mark_post_ready") as mark:
            out = c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        self.assertEqual(out["status"], "verify-failed-blocked-human_verification")
        mark.assert_not_called()

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

    def test_nickname_and_username_are_never_sent(self):
        """Changing either on an account already signed in put two real
        accounts behind Instagram's own human-verification checkpoint
        (`frida.sturm90`, `hanna.falk30`, 2026-08-23) even though Geelark
        reported the task Completed both times."""
        phone = _phone("1", ["IG connected"], model="Nikki")
        with mock.patch.object(c.library, "picture_url_for_tag",
                               return_value="https://x/nikki.jpg"), \
             mock.patch.object(c.rpa, "trigger_instagram_edit_profile",
                               return_value="") as trigger:
            c.run_one(phone, FULL_CONFIG, Args(), mock.Mock(), transport=None)

        kwargs = trigger.call_args.kwargs
        self.assertNotIn("nickname", kwargs)
        self.assertNotIn("username", kwargs)


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


class VerifySetupOnDeviceTest(unittest.TestCase):
    """Geelark's own "Completed" status was wrong twice in a row
    (2026-08-23): two real accounts ended up behind a human-verification
    checkpoint with nothing actually changed. This is the real check.
    """

    def setUp(self):
        self.host_patch = mock.patch.object(c, "GeelarkHost")
        self.fake_host_cls = self.host_patch.start()
        self.addCleanup(self.host_patch.stop)
        self.fake_host = self.fake_host_cls.return_value
        self.fake_host.launch.return_value = {"target": "fake-profile"}

        self.adb_patch = mock.patch.object(c, "ADBClient")
        self.adb_patch.start()
        self.addCleanup(self.adb_patch.stop)

        self.connect_patch = mock.patch.object(c, "connect_with_retries",
                                               return_value="host:1")
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)

    def test_a_block_screen_fails_verification(self):
        with mock.patch.object(
                c, "_adb_capture_ui_dump",
                return_value=_fake_root("Confirm you're human to use your account")):
            ok, reason = c.verify_setup_on_device("1")

        self.assertFalse(ok)
        self.assertTrue(reason.startswith("blocked-"))

    def test_a_missing_bio_fails_verification(self):
        with mock.patch.object(c, "_adb_capture_ui_dump",
                               return_value=_fake_root("some normal feed text")), \
             mock.patch.object(c, "InstagramUpdateBioFlow") as flow_cls, \
             mock.patch.object(c, "_adb_read_bio_field_value", return_value=""):
            flow_cls.return_value._open_edit_profile.return_value = "ok"
            ok, reason = c.verify_setup_on_device("1")

        self.assertFalse(ok)
        self.assertEqual(reason, "bio-not-set")

    def test_bio_present_and_no_block_screen_verifies(self):
        with mock.patch.object(c, "_adb_capture_ui_dump",
                               return_value=_fake_root("some normal feed text")), \
             mock.patch.object(c, "InstagramUpdateBioFlow") as flow_cls, \
             mock.patch.object(c, "_adb_read_bio_field_value",
                               return_value="Klick unten rein"):
            flow_cls.return_value._open_edit_profile.return_value = "ok"
            ok, reason = c.verify_setup_on_device("1")

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_a_phone_that_never_comes_up_fails_cleanly(self):
        self.fake_host.launch.return_value = None

        ok, reason = c.verify_setup_on_device("1")

        self.assertFalse(ok)
        self.assertEqual(reason, "phone-not-ready")

    def test_the_phone_is_always_shut_down_even_on_failure(self):
        self.fake_host.launch.return_value = None

        c.verify_setup_on_device("1")

        self.fake_host.shutdown.assert_called_once()

    def test_the_proxy_port_is_passed_through_to_geelarkhost(self):
        """This batch runs at the same concurrency as the signup pipeline
        and hits the same four-modem collision risk (2026-08-23) -- the
        lease has to actually reach `GeelarkHost`, not just exist."""
        with mock.patch.object(
                c, "_adb_capture_ui_dump",
                return_value=_fake_root("some normal feed text")), \
             mock.patch.object(c, "InstagramUpdateBioFlow") as flow_cls, \
             mock.patch.object(c, "_adb_read_bio_field_value",
                               return_value="a real bio"):
            flow_cls.return_value._open_edit_profile.return_value = "ok"
            c.verify_setup_on_device("1", proxy_port=54018)

        self.assertEqual(self.fake_host_cls.call_args.kwargs.get("proxy_port"),
                         54018)


class PhoneProxyPortTest(unittest.TestCase):
    """Same extraction as `signup_geelark._phone_proxy_port` -- the phone
    dict already carries its proxy from the same `list_phones()` call
    `phones_to_check()` uses to build the worklist."""

    def test_a_phones_own_port_is_read_from_its_proxy_field(self):
        phone = {"id": "1", "proxy": {"type": "socks5",
                                      "server": "162.55.84.35", "port": 54018}}

        self.assertEqual(c._phone_proxy_port(phone), 54018)

    def test_no_proxy_field_is_none_not_zero(self):
        self.assertIsNone(c._phone_proxy_port({"id": "1"}))


if __name__ == "__main__":
    unittest.main()
