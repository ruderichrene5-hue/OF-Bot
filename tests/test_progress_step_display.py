import shlex
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_bot.automation.flows.instagram import InstagramStoryUploadFlow, InstagramWarmUpDay1Flow, _adb_verify_remote_media_matches_local, _adb_find_instagram_next_center
from adb_bot.core.models import Profile
from adb_bot.automation.workflow import get_progress_counts, should_shutdown_profile_after_flow
from adb_bot.ui.ui import WorkflowUI


class ProgressStepDisplayTests(unittest.TestCase):
    def test_progress_updates_to_step_format(self):
        ui = WorkflowUI.__new__(WorkflowUI)
        ui.root = None
        ui.profile_progress_vars = {}

        ui._set_profile_progress("profile-1", 3, 10)
        self.assertEqual(ui.profile_progress_vars["profile-1"].get(), "3/10")

        ui._set_profile_progress("profile-1", 7, 10)
        self.assertEqual(ui.profile_progress_vars["profile-1"].get(), "7/10")

    def test_progress_counts_clamp_to_actual_total(self):
        completed, total = get_progress_counts(24, 30)
        self.assertEqual((completed, total), (24, 30))

        completed, total = get_progress_counts(35, 30)
        self.assertEqual((completed, total), (30, 30))

    def test_profiles_remain_open_after_completed_flows(self):
        self.assertFalse(should_shutdown_profile_after_flow("warm_up_process"))
        self.assertFalse(should_shutdown_profile_after_flow("instagram_notifications"))
        self.assertFalse(should_shutdown_profile_after_flow("instagram_scroll"))

    def test_warmup_progress_total_uses_logical_steps(self):
        flow = InstagramWarmUpDay1Flow()
        # Warm-up flow now includes scrolling and notification actions;
        # ensure it reports at least the original logical step count.
        self.assertGreaterEqual(flow.get_progress_total_steps("dummy-target"), 7)

    def test_story_upload_progress_total_uses_logical_steps(self):
        flow = InstagramStoryUploadFlow()
        self.assertEqual(flow.get_progress_total_steps("dummy-target"), 7)

    def test_story_upload_reports_progress_at_each_logical_stage(self):
        flow = InstagramStoryUploadFlow()
        profile = Profile(id="profile-1", status="active", ip="127.0.0.1", port="5554", pwd="pwd")
        adb_client = MagicMock()
        adb_client.run_command.return_value = None

        media_path = Path(__file__).parent / "fixtures" / "story_media" / "sample.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"fake-image")

        with patch("adb_bot.automation.flows.instagram._adb_resolve_story_media_path", return_value=str(media_path)), \
             patch("adb_bot.automation.flows.instagram._adb_push_media_to_device", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_verify_remote_media_exists", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_verify_remote_media_matches_local", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_wait_for_instagram_story_composer", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_ensure_instagram_feed_visible", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_story_action_center", return_value=(100, 100)), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_next_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_share_center", return_value=None), \
             patch.object(flow, "_open_story_composer", return_value=True), \
             patch.object(flow, "_select_story_media", return_value=True), \
             patch.object(flow, "_tap_your_story", return_value=True), \
             patch.object(flow, "_verify_story_post_completed", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_home_button_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram.time.sleep", return_value=None):
            result = flow.run(profile, adb_client=adb_client, logger=MagicMock())

        self.assertFalse(result["aborted"])
        self.assertGreaterEqual(adb_client.mark_progress_step.call_count, 7)

    def test_tap_next_uses_ocr_when_ui_dump_cannot_find_next(self):
        # On the auto-playing reel preview the UI dump can't locate Next; the
        # button is read off a screenshot via OCR and tapped there.
        flow = InstagramStoryUploadFlow()
        adb_client = MagicMock()

        with patch("adb_bot.automation.flows.instagram._adb_find_instagram_next_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_dialog_action_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_ocr_find_text_center", return_value=((940, 2000), "next")):
            result = flow._tap_next("device-1", adb_client, logger=MagicMock())

        self.assertTrue(result)
        adb_client.run_command.assert_called_once_with("adb -s device-1 shell input tap 940 2000")

    def test_tap_next_fails_safe_and_never_blind_taps_the_nav_bar(self):
        # When neither the UI dump nor OCR finds Next, _tap_next must NOT tap a
        # fixed bottom position (that was hitting the Android nav bar and
        # backgrounding Instagram) -- it returns False and taps nothing.
        flow = InstagramStoryUploadFlow()
        adb_client = MagicMock()

        with patch("adb_bot.automation.flows.instagram._adb_find_instagram_next_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_dialog_action_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_ocr_find_text_center", return_value=None):
            result = flow._tap_next("device-1", adb_client, logger=MagicMock())

        self.assertFalse(result)
        adb_client.run_command.assert_not_called()

    def test_tap_next_prefers_dialog_action_button_over_fallback(self):
        flow = InstagramStoryUploadFlow()
        adb_client = MagicMock()

        with patch("adb_bot.automation.flows.instagram._adb_find_instagram_next_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_dialog_action_center", return_value=(450, 1800)), \
             patch("adb_bot.automation.flows.instagram._adb_wait_for_instagram_story_share_screen", return_value=True), \
             patch("adb_bot.automation.flows.instagram._adb_get_relative_point", return_value=(970, 2100)):
            result = flow._tap_next("device-1", adb_client, logger=MagicMock())

        self.assertTrue(result)
        adb_client.run_command.assert_called_once_with("adb -s device-1 shell input tap 450 1800")

    def test_tap_next_reuses_previous_next_center_if_available(self):
        flow = InstagramStoryUploadFlow()
        adb_client = MagicMock()
        flow._last_next_center = (550, 1900)

        with patch("adb_bot.automation.flows.instagram._adb_find_instagram_next_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_dialog_action_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_wait_for_instagram_story_share_screen", return_value=False):
            result = flow._tap_next("device-1", adb_client, logger=MagicMock())

        self.assertTrue(result)
        adb_client.run_command.assert_called_once_with("adb -s device-1 shell input tap 550 1900")
        self.assertIsNone(flow._last_next_center)

    def test_tap_next_prefers_previous_location_when_new_next_wanders(self):
        flow = InstagramStoryUploadFlow()
        adb_client = MagicMock()
        flow._last_next_center = (1027, 2439)

        with patch("adb_bot.automation.flows.instagram._adb_find_instagram_next_center", return_value=(1109, 1845)), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_dialog_action_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_wait_for_instagram_story_share_screen", return_value=False):
            result = flow._tap_next("device-1", adb_client, logger=MagicMock())

        self.assertTrue(result)
        adb_client.run_command.assert_called_once_with("adb -s device-1 shell input tap 1027 2439")
        self.assertIsNone(flow._last_next_center)

    def test_find_instagram_next_center_skips_add_location_nodes(self):
        root = ET.fromstring(
            '<hierarchy>'
            '  <node text="Add location" clickable="true" enabled="true" bounds="[100,1800][980,1900]" />'
            '  <node text="Next" clickable="true" enabled="true" bounds="[1000,2400][1080,2480]" />'
            '</hierarchy>'
        )
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=root), \
             patch("adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)):
            center = _adb_find_instagram_next_center("device-1", logger=MagicMock())

        self.assertEqual(center, (1040, 2440))

    def test_verify_remote_media_matches_local_quotes_remote_path(self):
        import hashlib

        local_path = Path(__file__).parent / "fixtures" / "story_media" / "sample (2).jpg"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"fake-image")
        remote_path = "/sdcard/Download/sample (2).jpg"
        expected_remote_arg = shlex.quote(remote_path)

        digest = hashlib.sha256()
        digest.update(b"fake-image")
        local_hash = digest.hexdigest()

        with patch("adb_bot.automation.flows.instagram.subprocess.run") as mocked_run:
            mocked_run.return_value = MagicMock(returncode=0, stdout=f"{local_hash}  {remote_path}\n", stderr="")
            result = _adb_verify_remote_media_matches_local("target-device", str(local_path), remote_path, logger=MagicMock())

        self.assertTrue(result)
        # Ensure the quoted remote path appears in one of the subprocess.run calls
        found = False
        for call_args in mocked_run.call_args_list:
            args = call_args[0][0]
            try:
                if isinstance(args, (list, tuple)) and expected_remote_arg in args:
                    found = True
                    break
            except Exception:
                continue
        self.assertTrue(found)

    def test_profile_search_rebuilds_rows_in_alphabetical_order(self):
        class FakeVar:
            def __init__(self, value="") -> None:
                self._value = value

            def get(self) -> str:
                return self._value

            def set(self, value: str) -> None:
                self._value = value

        class FakeRow:
            order_counter = 0

            def __init__(self) -> None:
                self.pack_order = []
                self.sequence = None

            def pack_forget(self) -> None:
                self.pack_order.append("forget")

            def pack(self, **kwargs) -> None:
                FakeRow.order_counter += 1
                self.sequence = FakeRow.order_counter
                self.pack_order.append(("pack", kwargs))

        class FakeSection:
            def __init__(self) -> None:
                self.pack_order = []

            def pack(self, **kwargs) -> None:
                self.pack_order.append(("pack", kwargs))

            def pack_forget(self) -> None:
                self.pack_order.append("forget")

        ui = WorkflowUI.__new__(WorkflowUI)
        ui.profile_search_var = FakeVar("")
        ui.profile_rows = {
            "profile-2": FakeRow(),
            "profile-1": FakeRow(),
            "profile-3": FakeRow(),
        }
        ui.profile_to_folder = {
            "profile-2": "folder-1",
            "profile-1": "folder-1",
            "profile-3": "folder-2",
        }
        ui.profile_labels = {
            "profile-2": "Zeta",
            "profile-1": "Alpha",
            "profile-3": "Beta",
        }
        ui.folder_sections = {
            "folder-1": FakeSection(),
            "folder-2": FakeSection(),
        }

        ui._rebuild_profile_rows_for_search("")

        ordered_rows = [
            row_id for row_id in ["profile-1", "profile-2", "profile-3"]
            if ui.profile_rows[row_id].sequence is not None
        ]
        ordered_rows.sort(key=lambda row_id: ui.profile_rows[row_id].sequence)
        self.assertEqual(ordered_rows, ["profile-1", "profile-3", "profile-2"])


if __name__ == "__main__":
    unittest.main()
