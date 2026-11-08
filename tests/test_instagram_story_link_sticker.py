import xml.etree.ElementTree as ET
from unittest import TestCase
from unittest.mock import Mock, patch

from adb_bot.automation.flows.instagram import InstagramStoryUploadFlow

TARGET = "127.0.0.1:5555"
PATCH_DUMP = "adb_bot.automation.flows.instagram._adb_capture_ui_dump"


def _root(xml: str):
    return ET.fromstring(xml)


STICKER_BTN_SCREEN = _root(
    '<hierarchy><node content-desc="Add sticker" bounds="[900,190][1040,320]"/></hierarchy>'
)
STICKER_TRAY_SCREEN = _root(
    '<hierarchy>'
    '<node content-desc="Location Sticker" bounds="[50,750][300,900]"/>'
    '<node content-desc="Link Sticker" bounds="[400,1850][650,2010]"/>'
    '</hierarchy>'
)
URL_FIELD_SCREEN = _root(
    '<hierarchy><node class="android.widget.EditText" bounds="[50,750][1030,825]" text=""/>'
    '<node text="Customize sticker text" bounds="[120,1020][620,1085]"/></hierarchy>'
)
CUSTOMIZE_LABEL_SCREEN = _root(
    '<hierarchy><node text="Customize sticker text" bounds="[120,1020][620,1085]"/></hierarchy>'
)
TWO_EDITS_SCREEN = _root(
    '<hierarchy>'
    '<node class="android.widget.EditText" bounds="[50,750][1030,825]" text="https://link.me/nikkiofficial"/>'
    '<node class="android.widget.EditText" bounds="[50,1080][1030,1155]" text=""/>'
    '</hierarchy>'
)
DONE_SCREEN = _root('<hierarchy><node text="Done" bounds="[860,520][1080,690]"/></hierarchy>')
NO_STICKER_BTN_SCREEN = _root('<hierarchy><node text="unrelated" bounds="[0,0][10,10]"/></hierarchy>')


class AddLinkStickerTest(TestCase):
    """Covers the sequence reverse-engineered live 2026-09-12 -- see the
    docstring on InstagramStoryUploadFlow._add_link_sticker for the actual
    Instagram UI quirks these fixtures encode."""

    def test_happy_path_with_customized_sticker_text(self):
        flow = InstagramStoryUploadFlow()
        adb_client = Mock()

        with patch(PATCH_DUMP, side_effect=[
            STICKER_BTN_SCREEN,      # find the sticker-tray button
            STICKER_TRAY_SCREEN,     # find "Link Sticker" inside the tray
            URL_FIELD_SCREEN,        # find the URL EditText
            CUSTOMIZE_LABEL_SCREEN,  # find "Customize sticker text"
            TWO_EDITS_SCREEN,        # re-dump: now two EditTexts exist
            DONE_SCREEN,             # find "Done"
        ]):
            ok = flow._add_link_sticker(
                TARGET, adb_client, "https://link.me/nikkiofficial",
                sticker_text="bin jetzt online \U0001F495",
            )

        self.assertTrue(ok)
        # URL goes to the first EditText's bounds (center of [50,750][1030,825]).
        adb_client.shell_tap.assert_any_call(TARGET, 540, 787)
        adb_client.shell_write.assert_any_call(TARGET, "https://link.me/nikkiofficial")
        # Sticker text goes to the SECOND EditText's bounds, not the first --
        # this is the exact bug found live: typing into edits[0] again
        # corrupts the URL instead of setting the sticker text.
        adb_client.shell_tap.assert_any_call(TARGET, 540, 1117)
        adb_client.shell_write.assert_any_call(TARGET, "bin jetzt online \U0001F495")
        # Done tapped last.
        adb_client.shell_tap.assert_any_call(TARGET, 970, 605)

    def test_without_sticker_text_skips_the_customize_screen(self):
        flow = InstagramStoryUploadFlow()
        adb_client = Mock()

        with patch(PATCH_DUMP, side_effect=[
            STICKER_BTN_SCREEN,
            STICKER_TRAY_SCREEN,
            URL_FIELD_SCREEN,
            DONE_SCREEN,
        ]):
            ok = flow._add_link_sticker(TARGET, adb_client, "https://link.me/nikkiofficial")

        self.assertTrue(ok)
        adb_client.shell_write.assert_called_once_with(TARGET, "https://link.me/nikkiofficial")

    def test_missing_sticker_button_fails_cleanly_without_typing_anything(self):
        flow = InstagramStoryUploadFlow()
        adb_client = Mock()

        with patch(PATCH_DUMP, return_value=NO_STICKER_BTN_SCREEN):
            ok = flow._add_link_sticker(TARGET, adb_client, "https://link.me/nikkiofficial")

        self.assertFalse(ok)
        adb_client.shell_tap.assert_not_called()
        adb_client.shell_write.assert_not_called()

    def test_missing_link_sticker_in_tray_fails_without_typing(self):
        flow = InstagramStoryUploadFlow()
        adb_client = Mock()
        tray_without_link = _root(
            '<hierarchy><node content-desc="Location Sticker" bounds="[50,750][300,900]"/></hierarchy>'
        )

        with patch(PATCH_DUMP, side_effect=[STICKER_BTN_SCREEN, tray_without_link]):
            ok = flow._add_link_sticker(TARGET, adb_client, "https://link.me/nikkiofficial")

        self.assertFalse(ok)
        adb_client.shell_write.assert_not_called()

    def test_missing_second_edittext_still_posts_url_only(self):
        """The sticker-text field can fail to appear (e.g. a slow render).
        The URL sticker itself is still valid without a customized label, so
        this should still reach Done rather than aborting the whole sticker."""
        flow = InstagramStoryUploadFlow()
        adb_client = Mock()

        with patch(PATCH_DUMP, side_effect=[
            STICKER_BTN_SCREEN,
            STICKER_TRAY_SCREEN,
            URL_FIELD_SCREEN,
            CUSTOMIZE_LABEL_SCREEN,
            URL_FIELD_SCREEN,  # re-dump still shows only one EditText
            DONE_SCREEN,
        ]):
            ok = flow._add_link_sticker(
                TARGET, adb_client, "https://link.me/nikkiofficial", sticker_text="bin jetzt online"
            )

        self.assertTrue(ok)
        # Only the URL was ever written -- the sticker text was never typed
        # into the wrong (URL) field.
        adb_client.shell_write.assert_called_once_with(TARGET, "https://link.me/nikkiofficial")
