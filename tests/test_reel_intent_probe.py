"""The share-Intent probe: URI building, landing classification, and the safety
property that matters most -- it must never post.

The flow exists to answer whether `am start -a ACTION_SEND` can drop Instagram
into the Reel composer. Since that is unproven, an untested Intent landing on the
Story composer would publish to the wrong surface, which is not undoable. These
tests pin the diagnostic-only behaviour so a later edit cannot quietly turn it
into something that publishes.
"""

import unittest
from unittest import mock

from adb_bot.automation.flows.instagram_reel_intent import InstagramReelIntentProbeFlow
from adb_bot.core.models import Profile


def make_flow(**kwargs):
    # The real defaults watch for 20s and hold for 45s so a human can see the
    # phone. Tests opt out unless they are exercising that behaviour.
    kwargs.setdefault("watch_seconds", 0)
    kwargs.setdefault("hold_seconds", 0)
    return InstagramReelIntentProbeFlow(**kwargs)


class ClassifyLandingTest(unittest.TestCase):
    """Where Instagram lands decides the whole verdict."""

    def test_composer_activities(self):
        flow = make_flow()
        for activity in (
            "com.instagram.android/com.instagram.creation.activity.MediaCaptureActivity",
            "com.instagram.android/com.instagram.clips.share.ClipsShareActivity",
            "com.instagram.android/com.instagram.reels.ReelsActivity",
        ):
            self.assertEqual(flow._classify(activity), "composer", activity)

    def test_story_is_not_treated_as_success(self):
        """Story is the likely landing for a video SEND, and is a FAILURE for
        this purpose -- posting a reel to Story is the wrong surface."""
        flow = make_flow()
        self.assertEqual(
            flow._classify("com.instagram.android/com.instagram.share.handleractivity.StoryShareHandlerActivity"),
            "story")

    def test_chooser_and_other_app(self):
        flow = make_flow()
        self.assertEqual(flow._classify("android/com.android.internal.app.ResolverActivity"),
                         "chooser")
        self.assertEqual(flow._classify("com.instagram.android/com.instagram.app.ChooserActivity"),
                         "chooser")
        self.assertEqual(flow._classify("com.android.launcher/.Launcher"), "left-instagram")
        self.assertEqual(flow._classify(None), "unknown")

    def test_direct_share_handler_is_not_a_composer(self):
        """The real handler this device reports for SEND video/mp4. Its name ends
        in "ShareActivity", so a loose "share" marker would call it a composer and
        we would chase an Intent that only shares to DMs."""
        self.assertEqual(
            make_flow()._classify(
                "com.instagram.direct.share.handler.DirectExternalMediaShareActivity"),
            "direct")

    def test_bare_activity_names_are_recognised(self):
        """query-activities reports class names with no package prefix; matching
        on the full package would call all of them 'left-instagram'."""
        flow = make_flow()
        self.assertEqual(flow._classify("com.instagram.clips.share.ClipsShareActivity"), "composer")
        self.assertNotEqual(flow._classify("com.instagram.direct.share.handler.DirectExternalMediaShareActivity"),
                            "left-instagram")

    def test_share_router_is_neither_success_nor_failure(self):
        self.assertEqual(
            make_flow()._classify("com.instagram.android/com.instagram.share.handleractivity.ShareHandlerActivity"),
            "share-router")


class IntentVariantTest(unittest.TestCase):
    def test_file_uri_variant_always_present(self):
        """The original proposal is always measured, even though it is expected
        to fail -- the point is to have evidence rather than an argument."""
        variants = make_flow()._intent_variants("/sdcard/Download/reel.mp4", None)
        self.assertEqual(len(variants), 1)
        label, command = variants[0]
        self.assertIn("file://", label + command)
        self.assertIn("--eu android.intent.extra.STREAM file:///sdcard/Download/reel.mp4", command)
        self.assertIn("-p com.instagram.android", command)

    def test_content_variants_added_when_uri_resolves(self):
        # Without enumerated share targets there are no -n variants, so this is
        # the file:// form plus the content:// one.
        variants = make_flow()._intent_variants(
            "/sdcard/Download/reel.mp4", "content://media/external/video/media/42")
        self.assertEqual(len(variants), 2)
        commands = [c for _, c in variants]
        self.assertTrue(any("content://media/external/video/media/42" in c for c in commands))

    def test_content_variants_grant_read_permission(self):
        """Without the grant the receiving app cannot open the URI, so a content
        variant missing it would fail for the wrong reason and muddy the result."""
        variants = make_flow()._intent_variants("/sdcard/x.mp4", "content://media/external/video/media/7")
        for label, command in variants[1:]:
            self.assertIn("--grant-read-uri-permission", command, label)

    def test_remote_path_spaces_are_normalised(self):
        flow = make_flow()
        self.assertEqual(flow._build_remote_media_path("/local/my reel.mp4"),
                         "/sdcard/Download/my_reel.mp4")


class ContentUriResolutionTest(unittest.TestCase):
    def test_parses_id_from_content_query(self):
        flow = make_flow()
        with mock.patch.object(flow, "_shell",
                               return_value=mock.Mock(stdout="Row: 0 _id=1234", stderr="")):
            uri = flow._resolve_content_uri("1.2.3.4:5555", "/sdcard/Download/r.mp4", lambda *a: None)
        self.assertEqual(uri, "content://media/external/video/media/1234")

    def test_returns_none_when_query_is_empty(self):
        """The MLX cloud phones return nothing from `content query` -- that is the
        documented behaviour and the probe must report it, not crash."""
        flow = make_flow()
        with mock.patch.object(flow, "_shell",
                               return_value=mock.Mock(stdout="No result found.", stderr="")):
            self.assertIsNone(
                flow._resolve_content_uri("1.2.3.4:5555", "/sdcard/Download/r.mp4", lambda *a: None))

    def test_survives_shell_failure(self):
        flow = make_flow()
        with mock.patch.object(flow, "_shell", side_effect=OSError("boom")):
            self.assertIsNone(
                flow._resolve_content_uri("1.2.3.4:5555", "/sdcard/Download/r.mp4", lambda *a: None))


class NeverPostsTest(unittest.TestCase):
    """The safety property. A probe that posts defeats its own purpose."""

    def _run_probe(self, post_after_probe, landing):
        flow = make_flow(post_after_probe=post_after_probe)
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        module = "adb_bot.automation.flows.instagram_reel_intent"
        from pathlib import Path as P
        with mock.patch(f"{module}._adb_resolve_story_media_path", return_value="/local/reels"), \
             mock.patch(f"{module}.discover_story_media_files", return_value=[P("/local/reels/r.mp4")]), \
             mock.patch(f"{module}._adb_push_media_to_device", return_value=True), \
             mock.patch(f"{module}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{module}._adb_get_foreground_activity", return_value=landing), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch("time.sleep"):
            return flow.run(profile, adb_client=mock.Mock(), logger=mock.Mock())

    def test_does_not_post_even_when_a_composer_is_reached(self):
        result = self._run_probe(
            post_after_probe=True,
            landing="com.instagram.android/com.instagram.clips.share.ClipsShareActivity")
        self.assertTrue(result["success"])          # a composer was reached
        self.assertFalse(result["posted"])          # and nothing was published

    def test_story_landing_is_not_success(self):
        result = self._run_probe(
            post_after_probe=False,
            landing="com.instagram.android/com.instagram.share.handleractivity.StoryShareHandlerActivity")
        self.assertFalse(result["success"])
        self.assertFalse(result["posted"])

    def test_reports_when_content_uri_is_unavailable(self):
        result = self._run_probe(post_after_probe=False, landing=None)
        self.assertFalse(result["content_uri_available"])

    def test_missing_media_fails_cleanly(self):
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        module = "adb_bot.automation.flows.instagram_reel_intent"
        with mock.patch(f"{module}._adb_resolve_story_media_path", return_value=None), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")):
            result = flow.run(profile, adb_client=mock.Mock(), logger=mock.Mock())
        self.assertFalse(result["success"])
        self.assertIn("no media", result["reason"])


class NotSchedulableTest(unittest.TestCase):
    def test_probe_is_not_a_lifecycle_or_posting_flow(self):
        """A diagnostic that a scheduled loop could select would eventually run
        unattended against real accounts."""
        from adb_bot.automation import lifecycle, posting_runner
        from adb_bot.automation.airtable_runner import VALID_FLOWS

        name = InstagramReelIntentProbeFlow.name
        self.assertNotEqual(posting_runner.POST_FLOW, name)
        self.assertNotEqual(lifecycle.FLOW_REEL, name)
        self.assertNotIn(name, VALID_FLOWS)

    def test_registered_for_manual_runs(self):
        """...but it must be selectable by hand, or it can't be tested."""
        from adb_bot.automation.bootstrap import build_automation
        self.assertIn(InstagramReelIntentProbeFlow.name, build_automation().flows)


if __name__ == "__main__":
    unittest.main()


class MediaResolutionTest(unittest.TestCase):
    """The first live run pushed a *folder* (`.../Run 3/used`) instead of a video
    and failed at adb push. The setting is normally a folder, so a real file has
    to be picked out of it."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run_with_media(self, media_setting):
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        module = "adb_bot.automation.flows.instagram_reel_intent"
        pushed = {}

        def fake_push(target, local, remote, logger=None):
            pushed["local"] = local
            pushed["remote"] = remote
            return True

        profile.media_path = media_setting
        with mock.patch(f"{module}._adb_push_media_to_device", side_effect=fake_push), \
             mock.patch(f"{module}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{module}._adb_get_foreground_activity", return_value=None), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch("time.sleep"):
            result = flow.run(profile, adb_client=mock.Mock(), logger=mock.Mock())
        return result, pushed

    def test_picks_a_video_out_of_a_folder(self):
        from pathlib import Path as P
        root = P(self.tmp.name)
        (root / "used").mkdir()
        (root / "clip.mp4").write_bytes(b"x")
        result, pushed = self._run_with_media(str(root))
        self.assertTrue(pushed["local"].endswith("clip.mp4"))
        self.assertEqual(pushed["remote"], "/sdcard/Download/clip.mp4")

    def test_folder_with_no_video_fails_with_a_clear_reason(self):
        from pathlib import Path as P
        root = P(self.tmp.name)
        (root / "used").mkdir()
        result, pushed = self._run_with_media(str(root))
        self.assertFalse(result["success"])
        self.assertIn("no video file", result["reason"])
        self.assertEqual(pushed, {})          # never attempted the push

    def test_does_not_consume_production_media(self):
        """A diagnostic must not take a clip out of rotation for the next real
        post, so it must not touch the media queue."""
        from pathlib import Path as P
        root = P(self.tmp.name)
        (root / "clip.mp4").write_bytes(b"x")
        with mock.patch("adb_bot.automation.flows.story_media.get_story_media_queue") as queue:
            self._run_with_media(str(root))
        queue.assert_not_called()


class VerdictHonestyTest(unittest.TestCase):
    """An inconclusive run must not be reported as proof the Intent fails.

    The first live run pushed a .jpg as video/mp4, never saw Instagram come to
    the foreground, and could only test the file:// variant -- yet the summary
    said the shortcut "does not work". That is a wrong conclusion from a broken
    run, and the most expensive kind of mistake here.
    """

    def _messages(self, landing, content_uri):
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        module = "adb_bot.automation.flows.instagram_reel_intent"
        from pathlib import Path as P
        logger = mock.Mock()
        with mock.patch(f"{module}._adb_resolve_story_media_path", return_value="/m"), \
             mock.patch(f"{module}.discover_story_media_files", return_value=[P("/m/clip.mp4")]), \
             mock.patch(f"{module}._adb_push_media_to_device", return_value=True), \
             mock.patch(f"{module}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{module}._adb_get_foreground_activity", return_value=landing), \
             mock.patch.object(flow, "_resolve_content_uri", return_value=content_uri), \
             mock.patch.object(flow, "_foreground_any", return_value=landing), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch("time.sleep"):
            flow.run(profile, adb_client=mock.Mock(), logger=logger)
        return " ".join(str(c) for c in logger.info.call_args_list + logger.warning.call_args_list)

    def test_unknown_landing_is_inconclusive_not_negative(self):
        text = self._messages(landing=None, content_uri=None)
        self.assertIn("INCONCLUSIVE", text)

    def test_missing_content_uri_alone_is_inconclusive(self):
        """Only the file:// form could be tested, which is the one expected to
        fail -- that is not evidence about the approach as a whole."""
        text = self._messages(landing="com.android.launcher/.Launcher", content_uri=None)
        self.assertIn("INCONCLUSIVE", text)

    def test_real_negative_when_everything_was_observed(self):
        text = self._messages(
            landing="com.instagram.direct.share.handler.DirectExternalMediaShareActivity",
            content_uri="content://media/external/video/media/1")
        self.assertNotIn("INCONCLUSIVE", text)
        self.assertIn("does not reach Reels", text)


class VideoOnlyMediaTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, filenames):
        from pathlib import Path as P
        root = P(self.tmp.name)
        for name in filenames:
            (root / name).write_bytes(b"x")
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        profile.media_path = str(root)
        module = "adb_bot.automation.flows.instagram_reel_intent"
        pushed = {}
        with mock.patch(f"{module}._adb_push_media_to_device",
                        side_effect=lambda t, l, r, logger=None: pushed.update(local=l) or True), \
             mock.patch(f"{module}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{module}._adb_get_foreground_activity", return_value=None), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch("time.sleep"):
            return flow.run(profile, adb_client=mock.Mock(), logger=mock.Mock()), pushed

    def test_images_are_rejected(self):
        """The live run pushed 'Ani Waleski - Copia (2) - Copia.jpg' and sent it
        as video/mp4."""
        result, pushed = self._run(["photo.jpg", "other.png"])
        self.assertFalse(result["success"])
        self.assertIn("no video file", result["reason"])
        self.assertEqual(pushed, {})

    def test_video_is_chosen_over_images(self):
        result, pushed = self._run(["photo.jpg", "clip.mp4"])
        self.assertTrue(pushed["local"].endswith("clip.mp4"))


class DirectComponentTargetingTest(unittest.TestCase):
    """The live run reached the system chooser showing five Instagram targets,
    one of which is Reels. `-p <package>` cannot disambiguate them; naming the
    component with `-n` is what removes the chooser."""

    TARGETS = [
        "com.instagram.android/com.instagram.share.handleractivity.ShareHandlerActivity",
        "com.instagram.android/com.instagram.direct.share.handler.DirectExternalMediaShareActivity",
        "com.instagram.android/com.instagram.clips.share.ClipsShareHandlerActivity",
        "com.instagram.android/com.instagram.share.handleractivity.StoryShareHandlerActivity",
    ]

    def test_reels_alias_is_identified(self):
        flow = make_flow()
        reels = [t for t in self.TARGETS if flow._looks_like_reels(t)]
        self.assertEqual(len(reels), 1)
        self.assertIn("clips", reels[0].lower())

    def test_a_direct_component_variant_is_built_for_reels(self):
        variants = make_flow()._intent_variants("/sdcard/Download/r.mp4", None, self.TARGETS)
        direct = [c for _, c in variants if "-n " in c]
        self.assertTrue(direct, "no -n variant was built")
        for command in direct:
            self.assertIn("clips", command.lower())
            self.assertNotIn("-p com.instagram.android", command)

    def test_no_component_variants_without_enumeration(self):
        variants = make_flow()._intent_variants("/sdcard/Download/r.mp4", None, [])
        self.assertTrue(all("-n " not in c for _, c in variants))

    def test_story_and_direct_aliases_are_never_targeted(self):
        """Aiming at those would publish to the wrong surface if posting were on."""
        variants = make_flow()._intent_variants("/sdcard/Download/r.mp4", None, self.TARGETS)
        for _, command in variants:
            if "-n " in command:
                self.assertNotIn("StoryShare", command)
                self.assertNotIn("DirectExternal", command)

    def test_both_uri_forms_tried_against_the_reels_alias(self):
        variants = make_flow()._intent_variants(
            "/sdcard/Download/r.mp4", "content://media/external/video/media/9", self.TARGETS)
        reels_cmds = [c for _, c in variants if "clips" in c.lower()]
        self.assertTrue(any("file://" in c for c in reels_cmds))
        self.assertTrue(any("content://" in c for c in reels_cmds))


class BouncedToFeedTest(unittest.TestCase):
    """Aiming at ReelShareHandlerActivity delivered the Intent (am reported
    `cmp=...`) and Instagram then landed on InstagramMainActivity and quit.

    That is not 'some other screen' -- it means the handler looked at the media
    and refused it. Distinguishing it from a routing failure is what tells us the
    remaining work is the URI, not the component name."""

    def test_main_activity_is_its_own_verdict(self):
        self.assertEqual(
            make_flow()._classify("com.instagram.android/com.instagram.mainactivity.InstagramMainActivity"),
            "bounced-to-feed")

    def test_reel_share_handler_is_recognised_as_a_reels_target(self):
        """The real component name from the device -- singular 'Reel', not
        'Reels', and no 'clips' anywhere."""
        flow = make_flow()
        component = "com.instagram.android/com.instagram.share.handleractivity.ReelShareHandlerActivity"
        self.assertTrue(flow._looks_like_reels(component))
        variants = flow._intent_variants("/sdcard/Download/r.mp4", None, [component])
        self.assertTrue(any("-n " in c and "ReelShareHandler" in c for _, c in variants))

    def test_verdict_blames_the_media_not_the_routing(self):
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        module = "adb_bot.automation.flows.instagram_reel_intent"
        from pathlib import Path as P
        logger = mock.Mock()
        with mock.patch(f"{module}._adb_resolve_story_media_path", return_value="/m"), \
             mock.patch(f"{module}.discover_story_media_files", return_value=[P("/m/clip.mp4")]), \
             mock.patch(f"{module}._adb_push_media_to_device", return_value=True), \
             mock.patch(f"{module}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{module}._adb_get_foreground_activity",
                        return_value="com.instagram.android/com.instagram.mainactivity.InstagramMainActivity"), \
             mock.patch.object(flow, "_resolve_content_uri", return_value=None), \
             mock.patch.object(flow, "_enumerate_share_targets", return_value=[]), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch("time.sleep"):
            flow.run(profile, adb_client=mock.Mock(), logger=logger)
        text = " ".join(str(c) for c in logger.warning.call_args_list)
        self.assertIn("Routing works", text)
        self.assertIn("scoped storage", text)


class ContentUriStrategiesTest(unittest.TestCase):
    """`_data` is deprecated under scoped storage and is very likely why the
    lookup returned nothing on these phones -- not a device restriction."""

    def test_display_name_is_tried_before_data(self):
        flow = make_flow()
        seen = []

        def fake_shell(target, command, timeout=25):
            seen.append(command)
            return mock.Mock(stdout="No result found.", stderr="")

        with mock.patch.object(flow, "_shell", side_effect=fake_shell):
            flow._resolve_content_uri("t", "/sdcard/Download/clip.mp4", lambda *a: None)
        where_queries = [c for c in seen if "--where" in c]
        self.assertIn("_display_name='clip.mp4'", where_queries[0])
        self.assertTrue(any("_data=" in c for c in where_queries))

    def test_resolves_via_display_name(self):
        flow = make_flow()

        def fake_shell(target, command, timeout=25):
            if "_display_name" in command:
                return mock.Mock(stdout="Row: 0 _id=77", stderr="")
            return mock.Mock(stdout="No result found.", stderr="")

        with mock.patch.object(flow, "_shell", side_effect=fake_shell):
            uri = flow._resolve_content_uri("t", "/sdcard/Download/clip.mp4", lambda *a: None)
        self.assertEqual(uri, "content://media/external/video/media/77")

    def test_falls_back_to_listing_when_every_where_clause_fails(self):
        flow = make_flow()

        def fake_shell(target, command, timeout=25):
            if "--where" in command:
                return mock.Mock(stdout="No result found.", stderr="")
            return mock.Mock(stdout="Row: 0 _id=5, _display_name=other.mp4\n"
                                    "Row: 1 _id=9, _display_name=clip.mp4", stderr="")

        with mock.patch.object(flow, "_shell", side_effect=fake_shell):
            uri = flow._resolve_content_uri("t", "/sdcard/Download/clip.mp4", lambda *a: None)
        self.assertEqual(uri, "content://media/external/video/media/9")


class RemoteDirectoryTest(unittest.TestCase):
    """A push to /sdcard/DCIM reported success and the file was then gone --
    DCIM is a MediaStore-managed collection and the media provider can drop what
    adb writes there. Every working flow uses /sdcard/Download."""

    def test_pushes_to_download_like_the_other_flows(self):
        self.assertEqual(make_flow()._build_remote_media_path("/local/clip.mp4"),
                         "/sdcard/Download/clip.mp4")

    def test_matches_the_production_reel_flow(self):
        from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
        probe = make_flow()._build_remote_media_path("/local/clip.mp4")
        production = InstagramReelUploadU2Flow()._build_remote_media_path("/local/clip.mp4")
        self.assertEqual(probe, production)


class OfflineTunnelTest(unittest.TestCase):
    """`adb.exe: device offline` made the probe report "Pushed file not found",
    which points at storage paths when the real problem is the connection. These
    MLX tunnels drop -- a 12 MB push at 0.6 MB/s takes 20 s and does not always
    survive it."""

    MODULE = "adb_bot.automation.flows.instagram_reel_intent"

    def test_detects_offline_state(self):
        flow = make_flow()
        with mock.patch(f"{self.MODULE}._adb_run",
                        return_value=mock.Mock(stdout="", stderr="adb.exe: device offline")):
            self.assertTrue(flow._is_offline("t"))

    def test_online_device_is_not_offline(self):
        flow = make_flow()
        with mock.patch(f"{self.MODULE}._adb_run",
                        return_value=mock.Mock(stdout="device", stderr="")):
            self.assertFalse(flow._is_offline("t"))

    def test_empty_response_counts_as_offline(self):
        flow = make_flow()
        with mock.patch(f"{self.MODULE}._adb_run",
                        return_value=mock.Mock(stdout="", stderr="")):
            self.assertTrue(flow._is_offline("t"))

    def _run(self, offline, verify_results, reconnect_ok=True):
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        from pathlib import Path as P
        with mock.patch(f"{self.MODULE}._adb_resolve_story_media_path", return_value="/m"), \
             mock.patch(f"{self.MODULE}.discover_story_media_files", return_value=[P("/m/c.mp4")]), \
             mock.patch(f"{self.MODULE}._adb_push_media_to_device", return_value=True), \
             mock.patch(f"{self.MODULE}._adb_verify_remote_media_exists",
                        side_effect=verify_results), \
             mock.patch(f"{self.MODULE}._adb_reconnect_device", return_value=reconnect_ok), \
             mock.patch(f"{self.MODULE}._adb_get_foreground_activity", return_value=None), \
             mock.patch.object(flow, "_is_offline", return_value=offline), \
             mock.patch.object(flow, "_enumerate_share_targets", return_value=[]), \
             mock.patch.object(flow, "_resolve_content_uri", return_value=None), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch("time.sleep"):
            return flow.run(profile, adb_client=mock.Mock(), logger=mock.Mock())

    def test_offline_is_not_reported_as_a_missing_file(self):
        result = self._run(offline=True, verify_results=[False, False], reconnect_ok=False)
        self.assertFalse(result["success"])
        self.assertIn("offline", result["reason"])
        self.assertNotIn("missing", result["reason"])

    def test_recovers_when_reconnect_succeeds(self):
        """Verification failed once because the tunnel was down; after a
        reconnect the file is there and the probe carries on."""
        result = self._run(offline=True, verify_results=[False, True], reconnect_ok=True)
        self.assertNotIn("reason", result)      # got past media verification

    def test_genuinely_missing_file_still_reported_as_missing(self):
        result = self._run(offline=False, verify_results=[False])
        self.assertIn("missing", result["reason"])


class WatchAndHoldTest(unittest.TestCase):
    """A single sample a few seconds in cannot tell "arrived at the composer"
    from "still routing, about to bounce" -- and an earlier run did exactly that.
    What matters is where it settles."""

    def test_reel_share_handler_is_a_router_not_a_composer(self):
        """It contains "Reel", but it is the routing activity. Scoring it as a
        composer on one early sample would call a run that later bounced a win."""
        self.assertEqual(
            make_flow()._classify(
                "com.instagram.android/com.instagram.share.handleractivity.ReelShareHandlerActivity"),
            "share-router")

    def test_settles_on_the_last_activity_seen(self):
        flow = make_flow()
        seen = iter([
            "com.instagram.android/com.instagram.share.handleractivity.ReelShareHandlerActivity",
            "com.instagram.android/com.instagram.share.handleractivity.ReelShareHandlerActivity",
            "com.instagram.android/com.instagram.mainactivity.InstagramMainActivity",
        ])
        module = "adb_bot.automation.flows.instagram_reel_intent"
        with mock.patch(f"{module}._adb_get_foreground_activity", side_effect=lambda *a, **k: next(seen)), \
             mock.patch(f"{module}.time.sleep"), \
             mock.patch(f"{module}.time.time", side_effect=[0, 0, 1, 2, 99, 99]):
            final, sequence = flow._watch_landing("t", lambda *a: None, None, seconds=10)
        # The bounce is what counts, not the promising first sample.
        self.assertIn("MainActivity", final)
        self.assertEqual(flow._classify(final), "bounced-to-feed")
        self.assertEqual(len(sequence), 2)      # transitions, not every poll

    def test_always_samples_at_least_once(self):
        flow = make_flow()
        module = "adb_bot.automation.flows.instagram_reel_intent"
        with mock.patch(f"{module}._adb_get_foreground_activity", return_value="com.instagram.android/X"), \
             mock.patch(f"{module}.time.sleep"):
            final, sequence = flow._watch_landing("t", lambda *a: None, None, seconds=0)
        self.assertEqual(final, "com.instagram.android/X")

    def test_hold_is_configurable_and_off_for_tests(self):
        self.assertEqual(make_flow().hold_seconds, 0)
        self.assertEqual(InstagramReelIntentProbeFlow().hold_seconds, 45)
        # Raised from 20s: variant B sat on the handler past 20s and only then
        # dropped to the feed, so the old window scored a failure as a success.
        self.assertEqual(InstagramReelIntentProbeFlow().watch_seconds, 45)

    def test_router_landing_is_reported_as_a_slow_failure(self):
        """This assertion used to say the opposite. Sitting on the handler was
        read as "the media was accepted" -- then the device showed the same
        variant dropping to the feed a few seconds after the watch ended. The
        handler stalls before giving up, so it must not read as progress."""
        flow = make_flow()
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        module = "adb_bot.automation.flows.instagram_reel_intent"
        from pathlib import Path as P
        logger = mock.Mock()
        handler = "com.instagram.android/com.instagram.share.handleractivity.ReelShareHandlerActivity"
        with mock.patch(f"{module}._adb_resolve_story_media_path", return_value="/m"), \
             mock.patch(f"{module}.discover_story_media_files", return_value=[P("/m/c.mp4")]), \
             mock.patch(f"{module}._adb_push_media_to_device", return_value=True), \
             mock.patch(f"{module}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{module}._adb_get_foreground_activity", return_value=handler), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch.object(flow, "_enumerate_share_targets", return_value=[]), \
             mock.patch.object(flow, "_resolve_content_uri", return_value=None), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch("time.sleep"):
            result = flow.run(profile, adb_client=mock.Mock(), logger=logger)
        self.assertFalse(result["posted"])
        text = " ".join(str(c) for c in logger.warning.call_args_list)
        self.assertIn("never reached a composer", text)
        self.assertIn("slow failure", text)


class HoldSupersedesWatchTest(unittest.TestCase):
    """The 20s watch scored variant B "media accepted" because it was still on
    the reel share handler. The hold that ran seconds later showed the same
    variant dropping to the feed at ~25s. The handler gives up slowly, so the
    longer observation has to win."""

    MODULE = "adb_bot.automation.flows.instagram_reel_intent"
    HANDLER = "com.instagram.android/com.instagram.share.handleractivity.ReelShareHandlerActivity"
    FEED = "com.instagram.android/com.instagram.mainactivity.InstagramMainActivity"

    def _run_with_hold(self):
        flow = InstagramReelIntentProbeFlow(watch_seconds=0, hold_seconds=1)
        profile = Profile(id="p1", status="active", ip="1.2.3.4", port="5555")
        from pathlib import Path as P
        logger = mock.Mock()
        # Still on the handler during the watch; on the feed during the hold.
        activities = [self.HANDLER, self.HANDLER, self.FEED, self.FEED, self.FEED]
        supply = iter(activities)

        def foreground(*a, **k):
            try:
                return next(supply)
            except StopIteration:
                return self.FEED

        with mock.patch(f"{self.MODULE}._adb_resolve_story_media_path", return_value="/m"), \
             mock.patch(f"{self.MODULE}.discover_story_media_files", return_value=[P("/m/c.mp4")]), \
             mock.patch(f"{self.MODULE}._adb_push_media_to_device", return_value=True), \
             mock.patch(f"{self.MODULE}._adb_verify_remote_media_exists", return_value=True), \
             mock.patch(f"{self.MODULE}._adb_get_foreground_activity", side_effect=foreground), \
             mock.patch.object(flow, "_is_offline", return_value=False), \
             mock.patch.object(flow, "_enumerate_share_targets", return_value=[]), \
             mock.patch.object(flow, "_resolve_content_uri", return_value=None), \
             mock.patch.object(flow, "_shell", return_value=mock.Mock(stdout="", stderr="")), \
             mock.patch.object(flow, "_reset_instagram"), \
             mock.patch(f"{self.MODULE}.time.sleep"):
            result = flow.run(profile, adb_client=mock.Mock(), logger=logger)
        text = " ".join(str(c) for c in logger.info.call_args_list + logger.warning.call_args_list)
        return result, text

    def test_hold_correction_is_emitted(self):
        _result, text = self._run_with_hold()
        self.assertIn("CORRECTION", text)

    def test_final_verdict_is_a_failure(self):
        _result, text = self._run_with_hold()
        self.assertIn("does not work for Reels", text)

    def test_success_is_withdrawn_after_the_correction(self):
        """The run must not report success when the hold showed a bounce."""
        result, _text = self._run_with_hold()
        self.assertFalse(result["success"])

    def test_router_alone_is_no_longer_called_media_accepted(self):
        flow = InstagramReelIntentProbeFlow(watch_seconds=0, hold_seconds=0)
        self.assertEqual(flow._classify(self.HANDLER), "share-router")
        self.assertNotEqual(flow._classify(self.HANDLER), "composer")

    def test_default_watch_is_long_enough_for_the_slow_bounce(self):
        """B bounced somewhere past 20s, so the default window has to exceed it."""
        self.assertGreaterEqual(InstagramReelIntentProbeFlow().watch_seconds, 45)
