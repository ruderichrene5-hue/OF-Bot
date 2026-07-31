import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from adb_bot.automation.flows.instagram import (
    InstagramReelUploadFlow,
    InstagramStoryUploadFlow,
    _resolve_tesseract_executable,
)
from adb_bot.automation.flows.instagram_story import _adb_find_instagram_story_row_plus_center
from adb_bot.automation.flows.instagram import (
    _adb_find_instagram_dialog_action_center,
    _adb_find_instagram_media_selection_center,
    _adb_find_instagram_reel_create_center,
    _adb_find_instagram_reel_media_thumbnail_center,
    _adb_find_instagram_reel_caption_center,
    _adb_find_instagram_reel_option_center,
    _adb_find_instagram_share_center,
    _adb_find_instagram_start_new_video_center,
    _adb_is_instagram_reel_composer_visible,
    _adb_is_instagram_story_share_screen_visible,
)
from adb_bot.ui.helpers import get_available_flows


class ReelUploadFlowTest(TestCase):
    def test_reel_flow_is_available_in_ui_options(self):
        # uiautomator2 is the standard reel flow now; it is offered under the
        # plain name and the older screen-dump variant is no longer listed.
        flow_options = get_available_flows()
        self.assertIn(
            {"value": "instagram_reel_upload_u2", "label": "Instagram Reels Upload (BETA)"},
            flow_options,
        )
        self.assertNotIn("instagram_reel_upload", [flow["value"] for flow in flow_options])

    def test_resolve_tesseract_executable_uses_homebrew_prefix(self):
        with patch("adb_bot.automation.flows.instagram.shutil.which", return_value=None), patch(
            "adb_bot.automation.flows.instagram.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="/opt/homebrew\n", stderr=""),
        ), patch(
            "adb_bot.automation.flows.instagram.os.path.exists",
            side_effect=lambda path: path == "/opt/homebrew/opt/tesseract/bin/tesseract",
        ):
            resolved = _resolve_tesseract_executable()

        self.assertEqual(resolved, "/opt/homebrew/opt/tesseract/bin/tesseract")

    def test_reel_flow_has_expected_identifier(self):
        flow = InstagramReelUploadFlow()
        self.assertEqual(flow.name, "instagram_reel_upload")

    def test_prefers_top_left_reel_create_button_over_story_style_plus(self):
        fake_root = ET.fromstring(
            '<hierarchy><node bounds="[0,0][100,100]" text="+"/><node bounds="[100,500][200,600]" text="+"/></hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            self.assertEqual(_adb_find_instagram_reel_create_center("device-1"), (50, 50))

    def test_reel_composer_fails_when_reel_option_is_not_detected(self):
        fake_adb_client = Mock()

        with patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_reel_create_center",
            return_value=(50, 50),
        ), patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_reel_option_center",
            return_value=None,
        ), patch("adb_bot.automation.flows.instagram.time.sleep"):
            flow = InstagramReelUploadFlow()
            opened = flow._open_reel_composer("device-1", fake_adb_client, logger=None)

        self.assertFalse(opened)
        fake_adb_client.run_command.assert_called_once()

    def test_reel_option_locator_returns_none_when_missing(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[100,2200][300,2350]" text="POST"/>'
            '<node bounds="[400,2200][700,2350]" text="STORY"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_reel_option_center("device-1")

        self.assertIsNone(result)

    def test_reel_option_locator_detects_bottom_reel_button(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[300,1500][500,1600]" text="REEL"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_reel_option_center("device-1")

        self.assertEqual(result, (400, 1550))

    def test_reel_upload_prefers_bottom_reel_text_when_requested(self):
        fake_adb_client = Mock()
        flow = InstagramReelUploadFlow()

        with patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_reel_option_center",
            return_value=(400, 1550),
        ), patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_reel_media_thumbnail_center",
            return_value=(100, 100),
        ), patch("adb_bot.automation.flows.instagram._adb_tap") as tap_mock:
            result = flow._select_story_media("device-1", fake_adb_client, logger=None, prefer_reel_text=True)

        self.assertTrue(result)
        self.assertEqual(tap_mock.call_count, 2)
        tap_mock.assert_any_call(
            "device-1",
            400,
            1550,
            fake_adb_client,
            logger=None,
            description="Tapping REEL option text",
        )
        tap_mock.assert_any_call(
            "device-1",
            100,
            100,
            fake_adb_client,
            logger=None,
            description="Tapping reel media target",
        )

    def test_reel_media_thumbnail_locator_avoids_top_draft_buttons(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[0,0][1080,300]" text="Drafts"/>'
            '<node bounds="[20,900][520,1400]" content-desc="Photo thumbnail"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_reel_media_thumbnail_center("device-1")

        self.assertEqual(result, (270, 1150))

    def test_reel_media_thumbnail_locator_returns_none_for_top_rows(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[10,10][510,210]" text="Draft"/>'
            '<node bounds="[20,220][520,420]" content-desc="Gallery"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_reel_media_thumbnail_center("device-1")

        self.assertIsNone(result)

    def test_reel_caption_field_clicks_left_of_text_center(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[100,1100][980,1230]" text="Write a caption" class="android.widget.EditText"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_reel_caption_center("device-1")

        self.assertEqual(result, (188, 1165))

    def test_media_selection_prefers_upper_middle_region(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[100,700][980,1100]" content-desc="Photo thumbnail"/>'
            '<node bounds="[100,1200][980,1800]" content-desc="Photo thumbnail"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_media_selection_center("device-1")

        self.assertEqual(result, (540, 900))

    def test_story_flow_prefers_gallery_selection_over_reel_thumbnail_logic(self):
        fake_adb_client = Mock()
        flow = InstagramStoryUploadFlow()

        with patch("adb_bot.automation.flows.instagram._adb_is_instagram_story_composer_visible", return_value=True), patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_gallery_center", return_value=(300, 1000)
        ), patch("adb_bot.automation.flows.instagram._adb_tap") as tap_mock:
            result = flow._select_story_media("device-1", fake_adb_client, logger=None)

        self.assertTrue(result)
        tap_mock.assert_called_once_with(
            "device-1",
            300,
            1000,
            fake_adb_client,
            logger=None,
            description="Tapping story gallery selection",
        )

    def test_story_row_plus_favors_larger_story_button_over_tiny_toolbar_plus(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[0,250][100,350]" text="+"/>'
            '<node bounds="[100,300][260,420]" text="+"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_story_row_plus_center("device-1")

        self.assertEqual(result, (180, 360))

    def test_story_row_plus_searches_left_top_region_for_phone_specific_plus_location(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[220,470][280,530]" text="+"/>'
            '<node bounds="[10,10][40,40]" text="+"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_get_screen_size", return_value=(1080, 2340)
        ):
            result = _adb_find_instagram_story_row_plus_center("device-1")

        self.assertEqual(result, (250, 500))

    def test_reel_composer_visibility_detects_reel_ui(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[0,0][100,100]" text="REEL"/>'
            '<node bounds="[0,100][100,200]" text="POST"/>'
            '</hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root):
            self.assertTrue(_adb_is_instagram_reel_composer_visible("device-1"))

    def test_start_new_video_dialog_selects_new_video_before_reel(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[0,0][100,100]" text="Start new video"/>'
            '<node bounds="[300,1500][500,1600]" text="REEL"/>'
            '</hierarchy>'
        )

        fake_adb_client = Mock()
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_reel_create_center",
            return_value=(50, 50),
        ), patch("adb_bot.automation.flows.instagram.time.sleep"):
            flow = InstagramReelUploadFlow()
            opened = flow._open_reel_composer("device-1", fake_adb_client, logger=None)

        self.assertTrue(opened)
        self.assertEqual(fake_adb_client.run_command.call_count, 2)
        fake_adb_client.run_command.assert_any_call("adb -s device-1 shell input tap 50 50")
        fake_adb_client.run_command.assert_any_call("adb -s device-1 shell input tap 50 50")

    def test_reel_composer_opens_after_start_new_video_without_reel_option(self):
        fake_root = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[0,0][100,100]" text="Start new video"/>'
            '</hierarchy>'
        )

        fake_adb_client = Mock()
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_find_instagram_reel_create_center",
            return_value=(50, 50),
        ), patch(
            "adb_bot.automation.flows.instagram._adb_wait_for_instagram_reel_composer",
            return_value=True,
        ), patch("adb_bot.automation.flows.instagram.time.sleep"):
            flow = InstagramReelUploadFlow()
            opened = flow._open_reel_composer("device-1", fake_adb_client, logger=None)

        self.assertTrue(opened)
        self.assertEqual(fake_adb_client.run_command.call_count, 2)
        fake_adb_client.run_command.assert_any_call("adb -s device-1 shell input tap 50 50")
        fake_adb_client.run_command.assert_any_call("adb -s device-1 shell input tap 50 50")

    def test_tap_share_story_skips_when_share_button_is_missing(self):
        fake_adb_client = Mock()
        flow = InstagramReelUploadFlow()

        with patch("adb_bot.automation.flows.instagram._adb_find_instagram_share_center", return_value=None), \
             patch("adb_bot.automation.flows.instagram._adb_find_instagram_dialog_action_center", return_value=(900, 2200)), \
             patch("adb_bot.automation.flows.instagram._adb_wait_for_instagram_story_share_screen", return_value=True):
            result = flow._tap_share_story("device-1", fake_adb_client, logger=Mock())

        self.assertFalse(result)
        fake_adb_client.run_command.assert_not_called()

    def test_share_button_requires_visible_share_text(self):
        fake_root = ET.fromstring('<hierarchy><node bounds="[100,2200][980,2340]" text="POST" /></hierarchy>')

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root):
            self.assertIsNone(_adb_find_instagram_share_center("device-1"))

    def test_share_button_skips_non_blue_share_button_when_color_check_available(self):
        fake_root = ET.fromstring(
            '<hierarchy><node bounds="[100,2200][980,2340]" text="Share" clickable="true" enabled="true" /></hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_is_instagram_share_button_blue",
            return_value=False,
        ):
            self.assertIsNone(_adb_find_instagram_share_center("device-1"))

    def test_share_button_detects_blue_share_button_when_color_check_available(self):
        fake_root = ET.fromstring(
            '<hierarchy><node bounds="[100,2200][980,2340]" text="Share" clickable="true" enabled="true" /></hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root), patch(
            "adb_bot.automation.flows.instagram._adb_is_instagram_share_button_blue",
            return_value=True,
        ):
            self.assertEqual(_adb_find_instagram_share_center("device-1"), (540, 2270))

    def test_story_share_screen_visible_when_dialog_action_present(self):
        fake_root = ET.fromstring(
            '<hierarchy><node clickable="true" enabled="true" bounds="[100,2200][980,2340]" text="Done" /></hierarchy>'
        )

        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=fake_root):
            self.assertTrue(_adb_is_instagram_story_share_screen_visible("device-1"))
