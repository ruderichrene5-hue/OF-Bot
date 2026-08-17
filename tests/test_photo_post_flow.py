"""Tests for the feed photo-post flow.

The flow inherits almost everything from `InstagramReelUploadU2Flow`, so these
cover only what it actually changes -- the mode it selects, the cell it picks,
the way it walks to the caption screen, and how it resolves which still to
post. The inherited half is already covered by the reel flow's own tests, and
re-testing it here would just pin the parent's implementation twice.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from adb_bot.automation.bootstrap import build_automation
from adb_bot.automation.flows.instagram_photo import InstagramPhotoPostU2Flow
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
from adb_bot.ui.helpers import get_available_flows


def _profile(**kwargs):
    base = dict(id="p1", status="active", ip="1.2.3.4", port="5555", pwd="x")
    base.update(kwargs)
    return SimpleNamespace(**base, target="1.2.3.4:5555")


class RegistrationTest(TestCase):
    def test_flow_is_registered_under_its_name(self):
        self.assertIn("instagram_photo_post_u2", build_automation().flows)

    def test_flow_is_offered_in_the_ui(self):
        self.assertIn(
            {"value": "instagram_photo_post_u2", "label": "Instagram Photo Post (BETA)"},
            get_available_flows(),
        )

    def test_it_reuses_the_reel_flow_rather_than_copying_it(self):
        # If this ever stops being true, every fix to the account switcher, the
        # popup sweep, the ledger and the verification pass has to be made twice.
        self.assertTrue(issubclass(InstagramPhotoPostU2Flow, InstagramReelUploadU2Flow))


class ComposerModeTest(TestCase):
    """The single most dangerous thing this flow can get wrong is picking a
    thumbnail while the composer is in REEL mode -- that posts a reel."""

    def setUp(self):
        self.flow = InstagramPhotoPostU2Flow()

    def test_it_targets_post_mode(self):
        wanted = {list(s.values())[0] for s in self.flow._reel_tab_selectors()}
        self.assertIn("POST", wanted)

    def test_reel_is_a_mode_it_refuses_to_post_from(self):
        rejected = {list(s.values())[0] for s in self.flow._other_mode_selectors()}
        self.assertIn("REEL", rejected)
        self.assertIn("STORY", rejected)

    def test_post_is_not_in_its_own_reject_list(self):
        # A mode in both lists would make `_select_reel_mode_u2` decide it is
        # simultaneously right and wrong, and refuse to ever pick media.
        wanted = {list(s.values())[0] for s in self.flow._reel_tab_selectors()}
        rejected = {list(s.values())[0] for s in self.flow._other_mode_selectors()}
        self.assertEqual(wanted & rejected, set())

    def test_it_will_not_select_media_when_the_mode_is_wrong(self):
        flow = InstagramPhotoPostU2Flow()
        with patch.object(flow, "_select_mode_u2", return_value=False):
            self.assertFalse(flow._select_media_u2(object(), "t", lambda *a: None))

    def test_it_picks_a_photo_cell_and_never_falls_back_to_video(self):
        # A Video fallback is how a "photo post" silently becomes a feed video
        # on a phone whose gallery holds both.
        flow = InstagramPhotoPostU2Flow()
        seen = {}

        def fake_click(d, selectors, **kwargs):
            seen["selectors"] = selectors
            return True

        with patch.object(flow, "_select_mode_u2", return_value=True), \
                patch("adb_bot.automation.flows.instagram_photo.waits.settle"), \
                patch("adb_bot.automation.flows.instagram_photo._u2_click", fake_click):
            self.assertTrue(flow._select_media_u2(object(), "t", lambda *a: None))

        blob = repr(seen["selectors"])
        self.assertIn("Photo", blob)
        self.assertNotIn("Video", blob)


class AdvanceToCaptionTest(TestCase):
    """A photo passes through crop/filter screens a reel does not. Counting
    them would break on the next Instagram build; this asks instead."""

    def _run(self, screens):
        """`screens` is the sequence of answers to 'is the caption/share screen
        up?', one per check. Returns (reached, taps)."""
        flow = InstagramPhotoPostU2Flow()
        answers = list(screens)
        taps = []

        def fake_any_exists(d, *selectors):
            return answers.pop(0) if answers else True

        def fake_advance(d, target, emit, logger=None, selectors=None, purpose="x"):
            taps.append(purpose)
            return True

        with patch("adb_bot.automation.flows.instagram_photo.waits.any_exists", fake_any_exists), \
                patch("adb_bot.automation.flows.instagram_photo.waits.settle"), \
                patch("adb_bot.automation.flows.instagram_photo.waits.u2_ready"), \
                patch.object(flow, "_tap_advance_u2", fake_advance):
            reached = flow._advance_to_caption_u2(object(), "t", lambda *a: None)
        return reached, taps

    def test_already_on_the_caption_screen_taps_nothing(self):
        reached, taps = self._run([True])
        self.assertTrue(reached)
        self.assertEqual(taps, [])

    def test_one_intermediate_screen_takes_one_next(self):
        reached, taps = self._run([False, True])
        self.assertTrue(reached)
        self.assertEqual(len(taps), 1)

    def test_crop_then_filter_takes_two_nexts(self):
        reached, taps = self._run([False, False, True])
        self.assertTrue(reached)
        self.assertEqual(len(taps), 2)

    def test_it_gives_up_rather_than_tapping_forever(self):
        flow = InstagramPhotoPostU2Flow()
        taps = []

        def fake_advance(d, target, emit, logger=None, selectors=None, purpose="x"):
            taps.append(purpose)
            return True

        with patch("adb_bot.automation.flows.instagram_photo.waits.any_exists", return_value=False), \
                patch("adb_bot.automation.flows.instagram_photo.waits.settle"), \
                patch("adb_bot.automation.flows.instagram_photo.waits.u2_ready"), \
                patch.object(flow, "_tap_advance_u2", fake_advance):
            reached = flow._advance_to_caption_u2(object(), "t", lambda *a: None)

        self.assertFalse(reached)
        self.assertEqual(len(taps), flow.MAX_ADVANCE_STEPS)

    def test_a_missing_next_button_stops_the_walk(self):
        flow = InstagramPhotoPostU2Flow()
        with patch("adb_bot.automation.flows.instagram_photo.waits.any_exists", return_value=False), \
                patch("adb_bot.automation.flows.instagram_photo.waits.settle"), \
                patch("adb_bot.automation.flows.instagram_photo.waits.u2_ready"), \
                patch.object(flow, "_tap_advance_u2", return_value=False) as advance:
            self.assertFalse(flow._advance_to_caption_u2(object(), "t", lambda *a: None))
        self.assertEqual(advance.call_count, 1)


class PhotoResolutionTest(TestCase):
    def setUp(self):
        self.flow = InstagramPhotoPostU2Flow()
        self.notes = []

    def _emit(self, level, message, *args):
        self.notes.append(message % args if args else message)

    def test_media_path_file_wins(self):
        with TemporaryDirectory() as tmp:
            pic = Path(tmp) / "a.jpg"
            pic.write_bytes(b"x")
            path, selected, queue = self.flow._resolve_photo(
                _profile(media_path=str(pic), picture=None), self._emit)
        self.assertEqual(path, str(pic))
        self.assertIsNone(queue)

    def test_picture_field_is_used_when_media_path_is_empty(self):
        # So a caller that already knows the still does not have to learn a
        # second field name.
        with TemporaryDirectory() as tmp:
            pic = Path(tmp) / "b.png"
            pic.write_bytes(b"x")
            path, _, _ = self.flow._resolve_photo(
                _profile(media_path=None, picture=str(pic)), self._emit)
        self.assertEqual(path, str(pic))

    def test_a_video_is_refused_outright(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            clip.write_bytes(b"x")
            path, _, _ = self.flow._resolve_photo(
                _profile(media_path=str(clip), picture=None), self._emit)
        self.assertIsNone(path)
        self.assertTrue(any("posts stills only" in n for n in self.notes))

    def test_a_missing_file_is_refused(self):
        path, _, _ = self.flow._resolve_photo(
            _profile(media_path="/nope/none.jpg", picture=None), self._emit)
        self.assertIsNone(path)

    def test_nothing_configured_is_refused(self):
        with patch("adb_bot.automation.flows.instagram_photo._adb_resolve_story_media_path",
                   return_value=None):
            path, _, _ = self.flow._resolve_photo(
                _profile(media_path=None, picture=None), self._emit)
        self.assertIsNone(path)

    def test_a_folder_yields_a_photo_and_skips_clips_without_consuming_them(self):
        # Marking a skipped clip "used" would MOVE it into used/ and take it
        # away from the reel flow, which is the flow it belongs to.
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "a_clip.mp4"
            pic = Path(tmp) / "b_still.jpg"
            clip.write_bytes(b"x")
            pic.write_bytes(b"x")

            path, selected, queue = self.flow._resolve_photo(
                _profile(media_path=tmp, picture=None), self._emit)

            self.assertEqual(Path(path).name, "b_still.jpg")
            self.assertIsNotNone(queue)
            self.assertTrue(clip.exists(), "the reel flow's clip must stay where it was")

    def test_a_folder_with_no_stills_is_refused(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "only.mp4").write_bytes(b"x")
            path, _, _ = self.flow._resolve_photo(
                _profile(media_path=tmp, picture=None), self._emit)
        self.assertIsNone(path)


class RemotePathTest(TestCase):
    def test_photos_are_pushed_to_pictures_with_spaces_removed(self):
        flow = InstagramPhotoPostU2Flow()
        self.assertEqual(
            flow._build_remote_media_path("/local/my holiday.jpg"),
            "/sdcard/Pictures/my_holiday.jpg",
        )


class PhotoFolderMappingTest(TestCase):
    """A scheduled photo post has to find a picture by itself. It reuses the
    per-model media folder mapping -- the thing that already answers "which
    model owns this media" -- and takes the `photos/` subfolder of it."""

    def test_photo_flow_is_not_treated_as_a_reel_flow(self):
        # If it were, a scheduled photo post would be handed the model's clips.
        from adb_bot.config.settings import PHOTO_FLOWS, REEL_FLOWS
        self.assertIn("instagram_photo_post_u2", PHOTO_FLOWS)
        self.assertNotIn("instagram_photo_post_u2", REEL_FLOWS)
        self.assertEqual(set(PHOTO_FLOWS) & set(REEL_FLOWS), set())

    def test_it_resolves_the_photos_subfolder(self):
        from adb_bot.config.settings import PHOTO_SUBFOLDER, photo_folder_for
        with TemporaryDirectory() as tmp:
            stills = Path(tmp) / PHOTO_SUBFOLDER
            stills.mkdir()
            self.assertEqual(photo_folder_for(tmp), str(stills))

    def test_no_subfolder_resolves_to_nothing_rather_than_the_clips(self):
        # Falling back to the parent folder would post the model's video queue.
        from adb_bot.config.settings import photo_folder_for
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "a_clip.mp4").write_bytes(b"x")
            self.assertIsNone(photo_folder_for(tmp))

    def test_no_mapping_resolves_to_nothing(self):
        from adb_bot.config.settings import photo_folder_for
        self.assertIsNone(photo_folder_for(None))
        self.assertIsNone(photo_folder_for(""))

    def test_it_does_not_create_the_folder_as_a_side_effect(self):
        # An empty directory appearing by itself reads as "photos are set up
        # here" to the next person who looks.
        from adb_bot.config.settings import PHOTO_SUBFOLDER, photo_folder_for
        with TemporaryDirectory() as tmp:
            photo_folder_for(tmp)
            self.assertFalse((Path(tmp) / PHOTO_SUBFOLDER).exists())

    def test_airtable_may_drive_the_photo_flow(self):
        from adb_bot.automation.airtable_runner import VALID_FLOWS
        self.assertIn("instagram_photo_post_u2", VALID_FLOWS)
