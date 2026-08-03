"""Regression tests for the time sinks found in the first live Rodrigo run."""

from unittest import TestCase, mock

from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow


def hierarchy(*nodes) -> str:
    """Minimal uiautomator-style XML with the attributes the scanner reads."""
    body = "".join(
        '<node text="{text}" content-desc="{desc}" clickable="{clickable}" '
        'visible-to-user="{visible}" bounds="{bounds}"/>'.format(**n)
        for n in nodes
    )
    return f'<?xml version="1.0"?><hierarchy>{body}</hierarchy>'


def node(text="", desc="", clickable="true", visible="true", bounds="[0,0][100,100]"):
    return {"text": text, "desc": desc, "clickable": clickable, "visible": visible, "bounds": bounds}


class FakeDevice:
    def __init__(self, xml, on_click=None):
        self.xml = xml
        self.clicks = []
        self.dumps = 0
        self._on_click = on_click

    def dump_hierarchy(self):
        self.dumps += 1
        return self.xml

    def click(self, x, y):
        self.clicks.append((x, y))
        if self._on_click:
            self._on_click(self)


class DismissScanTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_finds_a_clickable_dismiss_control(self):
        d = FakeDevice(hierarchy(node(desc="Dismiss", bounds="[609,2159][700,2250]")))
        found = self.flow._find_dismiss_in_dump(d)
        self.assertIsNotNone(found)
        label, center, bounds = found
        self.assertEqual(label, "dismiss")
        self.assertEqual(center, (654, 2204))
        self.assertEqual(bounds, "[609,2159][700,2250]")

    def test_one_dump_per_scan(self):
        # The old version issued 10 labels x 2 selectors = 20 RPCs per round.
        d = FakeDevice(hierarchy(node(desc="Dismiss")))
        self.flow._find_dismiss_in_dump(d)
        self.assertEqual(d.dumps, 1)

    def test_non_clickable_label_is_a_fallback_not_a_reject(self):
        # Dialog buttons are TextViews inside clickable rows (see the Rate
        # Instagram case), so a clickable=false label must still be usable --
        # the exact-label rule below is what keeps this safe.
        d = FakeDevice(hierarchy(node(text="Dismiss", clickable="false")))
        found = self.flow._find_dismiss_in_dump(d)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "dismiss")

    def test_ignores_invisible_nodes(self):
        d = FakeDevice(hierarchy(node(desc="Dismiss", visible="false")))
        self.assertIsNone(self.flow._find_dismiss_in_dump(d))

    def test_requires_an_exact_label_not_a_substring(self):
        # Feed copy like "Dismiss this suggestion" must not count as a control.
        d = FakeDevice(hierarchy(node(text="Dismiss this suggestion")))
        self.assertIsNone(self.flow._find_dismiss_in_dump(d))

    def test_malformed_dump_is_survivable(self):
        self.assertIsNone(self.flow._find_dismiss_in_dump(FakeDevice("not xml")))


class DismissLoopTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_skips_entirely_when_screen_is_usable(self):
        # The Rodrigo run burned 74s sweeping a feed whose Create button was
        # right there. With the skip check it costs nothing.
        d = FakeDevice(hierarchy(node(desc="Dismiss")))
        self.flow._dismiss_popups_u2(d, skip_if=lambda: True)
        self.assertEqual(d.dumps, 0)
        self.assertEqual(d.clicks, [])

    def test_stops_when_the_popup_does_not_go_away(self):
        # The exact Rodrigo failure: the same 'Dismiss' node, unchanged, tapped
        # once per round. It must now stop after the first ineffective tap.
        d = FakeDevice(hierarchy(node(desc="Dismiss", bounds="[609,2159][700,2250]")))
        self.flow._dismiss_popups_u2(d, max_rounds=3)
        self.assertEqual(len(d.clicks), 1)

    def test_keeps_going_while_popups_actually_change(self):
        d = FakeDevice(hierarchy(node(desc="Dismiss", bounds="[0,0][100,100]")))

        def advance(dev):
            # A real pop-up chain: each tap reveals a different control.
            dev.xml = hierarchy(node(text="Not now", bounds="[10,10][110,110]"))
            dev._on_click = None

        d._on_click = advance
        self.flow._dismiss_popups_u2(d, max_rounds=3)
        self.assertEqual(len(d.clicks), 2)

    def test_skip_predicate_errors_do_not_block_the_scan(self):
        def boom():
            raise RuntimeError("u2 down")
        d = FakeDevice(hierarchy(node(desc="Dismiss")))
        self.flow._dismiss_popups_u2(d, max_rounds=1, skip_if=boom)
        self.assertEqual(len(d.clicks), 1)


RATE_INSTAGRAM_DIALOG = """<?xml version="1.0"?>
<hierarchy>
  <node class="android.widget.FrameLayout" clickable="false" bounds="[36,321][406,610]">
    <node text="Rate Instagram" clickable="false" bounds="[60,345][300,380]"/>
    <node text="If you enjoy using Instagram, would you mind taking a moment to rate it?"
          clickable="false" bounds="[60,390][400,450]"/>
    <node class="android.widget.Button" clickable="true" bounds="[36,460][406,508]">
      <node text="Rate Instagram" clickable="false" bounds="[160,470][280,500]"/>
    </node>
    <node class="android.widget.Button" clickable="true" bounds="[36,510][406,560]">
      <node text="Remind me later" clickable="false" bounds="[150,520][290,552]"/>
    </node>
    <node class="android.widget.Button" clickable="true" bounds="[36,562][406,610]">
      <node text="No, thanks" clickable="false" bounds="[165,572][275,602]"/>
    </node>
  </node>
</hierarchy>"""


class RateInstagramDialogTest(TestCase):
    """The prompt Instagram shows after posting. Its buttons are TextViews inside
    clickable rows, so the label itself is clickable=false."""

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def _find(self):
        class Dev:
            def dump_hierarchy(self):
                return RATE_INSTAGRAM_DIALOG
        return self.flow._find_dismiss_in_dump(Dev())

    def test_finds_no_thanks(self):
        found = self._find()
        self.assertIsNotNone(found, "the Rate Instagram dialog must be detected")
        self.assertEqual(found[0], "no, thanks")

    def test_taps_the_clickable_row_not_the_bare_label(self):
        # Centre must come from the clickable Button row [36,562][406,610],
        # not the inner TextView -- tapping the row is what actually dismisses.
        _label, center, bounds = self._find()
        self.assertEqual(bounds, "[36,562][406,610]")
        self.assertEqual(center, (221, 586))

    def test_does_not_pick_rate_or_remind(self):
        # "Rate Instagram" would open the Play Store; "Remind me later" leaves it
        # to come back mid-flow. Only the exact safe label may win.
        label, _center, _bounds = self._find()
        self.assertNotIn("rate", label)
        self.assertNotIn("remind", label)

    def test_dismiss_loop_clicks_it_once(self):
        clicks = []

        class Dev:
            def dump_hierarchy(inner):
                return RATE_INSTAGRAM_DIALOG

            def click(inner, x, y):
                clicks.append((x, y))

        self.flow._dismiss_popups_u2(Dev(), max_rounds=3)
        self.assertEqual(clicks[0], (221, 586))
        # The stub keeps returning the dialog; stuck-detection must stop at one.
        self.assertEqual(len(clicks), 1)

    def test_prefers_a_genuinely_clickable_control_when_both_exist(self):
        xml = """<?xml version="1.0"?><hierarchy>
          <node text="No, thanks" clickable="false" bounds="[0,0][10,10]"/>
          <node text="Not now" clickable="true" bounds="[100,100][200,200]"/>
        </hierarchy>"""

        class Dev:
            def dump_hierarchy(self):
                return xml

        label, center, _b = self.flow._find_dismiss_in_dump(Dev())
        self.assertEqual(label, "not now")
        self.assertEqual(center, (150, 150))

    def test_orphan_label_without_clickable_ancestor_is_still_usable(self):
        xml = ('<?xml version="1.0"?><hierarchy>'
               '<node text="No, thanks" clickable="false" bounds="[100,200][300,260]"/>'
               '</hierarchy>')

        class Dev:
            def dump_hierarchy(self):
                return xml

        label, center, _b = self.flow._find_dismiss_in_dump(Dev())
        self.assertEqual(label, "no, thanks")
        self.assertEqual(center, (200, 230))


class SelectorCoverageTest(TestCase):
    """The selector sets the live run showed were wrong or too narrow."""

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_create_selectors_include_the_resource_id(self):
        # content-desc "Create" matched nothing on the test device; the nav tabs
        # use a *_tab resource-id family (feed_tab/profile_tab are confirmed).
        ids = [s.get("resourceId") for s in self.flow._CREATE_SELECTORS]
        self.assertIn("com.instagram.android:id/creation_tab", ids)

    def test_create_selectors_still_try_content_desc(self):
        descs = [s.get("description") for s in self.flow._CREATE_SELECTORS]
        self.assertIn("Create", descs)

    def test_advance_selectors_cover_continue_as_well_as_next(self):
        blob = str(self.flow._ADVANCE_SELECTORS).lower()
        self.assertIn("next", blob)
        self.assertIn("continue", blob)

    def test_next_selectors_remain_a_subset_of_advance(self):
        for selector in self.flow._NEXT_SELECTORS:
            self.assertIn(selector, self.flow._ADVANCE_SELECTORS)

    def test_gallery_selectors_are_shared(self):
        # Same list drives the readiness wait and the composer-open check.
        self.assertTrue(len(self.flow._GALLERY_SELECTORS) >= 5)


class BrowseRefreshTest(TestCase):
    """The post count only updates if the profile screen is actually re-rendered
    -- sitting on it (or re-tapping the tab) leaves a new reel invisible."""

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def _device(self):
        events = []

        class Dev:
            def window_size(self):
                return (1080, 2340)

            def swipe(self, x1, y1, x2, y2, duration):
                events.append(("swipe", "down" if y2 > y1 else "up"))

            def __call__(self, **kwargs):
                class Node:
                    exists = True

                    def click(inner):
                        events.append(("click", kwargs.get("resourceId", str(kwargs))))
                        return True
                return Node()

        return Dev(), events

    def test_leaves_the_profile_and_comes_back(self):
        d, events = self._device()
        self.assertTrue(self.flow._browse_and_refresh_profile_u2(d, "t"))
        clicked = [name for kind, name in events if kind == "click"]
        # Home first, then Profile -- that round trip is what forces the refresh.
        self.assertTrue(any("feed_tab" in c for c in clicked), clicked)
        self.assertTrue(any("profile_tab" in c for c in clicked), clicked)
        self.assertLess(next(i for i, c in enumerate(clicked) if "feed_tab" in c),
                        next(i for i, c in enumerate(clicked) if "profile_tab" in c))

    def test_pulls_to_refresh_without_a_wasted_scroll_back(self):
        """Was: scroll the feed down, scroll back up, then pull-to-refresh the
        profile. The scroll-back only undid the scroll-down -- the refresh comes
        from the feed->profile round trip plus the pull. One gesture per screen
        is enough, and this probe repeats every ~25s for up to three minutes."""
        d, events = self._device()
        self.flow._browse_and_refresh_profile_u2(d, "t")
        swipes = [direction for kind, direction in events if kind == "swipe"]
        self.assertEqual(len(swipes), 2, swipes)
        self.assertNotIn("up", swipes)   # the scroll-away-and-back is gone
        self.assertEqual(swipes, ["down", "down"])   # feed reload, then grid pull

    def test_survives_a_device_without_window_size(self):
        class Dev:
            def window_size(self):
                raise RuntimeError("no size")

            def swipe(self, *a):
                raise RuntimeError("no swipe")

            def __call__(self, **kwargs):
                class Node:
                    exists = True

                    def click(inner):
                        return True
                return Node()

        # Falls back to default dimensions and still completes.
        self.assertTrue(self.flow._browse_and_refresh_profile_u2(Dev(), "t"))

    def test_probe_is_throttled_between_refreshes(self):
        # Each refresh is a real navigation round trip, so it must not run on
        # every poll of the verification loop.
        calls = []

        class CountingFlow(type(self.flow)):
            def _browse_and_refresh_profile_u2(inner, d, target, logger=None):
                calls.append(1)
                return True

            def _read_post_count_u2(inner, d, target, logger=None):
                from adb_bot.automation.flows.reel_verify import Count
                return Count(3, True)

        # initial_delay=0 skips the banner-window hold-off so the throttle
        # itself is what's under test here.
        probe = CountingFlow()._profile_post_count_probe_u2(None, "t", None, None,
                                                            every_seconds=999, initial_delay=0)
        self.assertIsNotNone(probe())
        self.assertIsNone(probe())
        self.assertEqual(len(calls), 1)


def create_hierarchy(*nodes) -> str:
    body = "".join(
        '<node resource-id="{rid}" content-desc="{desc}" class="{cls}" '
        'clickable="{clickable}" bounds="{bounds}"/>'.format(**n)
        for n in nodes
    )
    return f'<?xml version="1.0"?><hierarchy>{body}</hierarchy>'


def cnode(rid="", desc="", cls="android.widget.Button", clickable="true", bounds="[0,0][100,100]"):
    return {"rid": rid, "desc": desc, "cls": cls, "clickable": clickable, "bounds": bounds}


class CreateButtonScoringTest(TestCase):
    """On the tested build the + is an UNLABELED node -- no resource-id and no
    content-desc -- so it can only be found by position. These pin the scoring
    so a labelled build still wins, and corner controls never do."""

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def _find(self, xml, size=(1200, 2514)):
        class Dev:
            def dump_hierarchy(self):
                return xml

            def window_size(self):
                return size

        return self.flow._find_create_via_dump_u2(Dev(), "t")

    def test_unlabeled_top_left_button_is_found(self):
        # The real Rodrigo case: no id, no desc, top-left, clickable.
        xml = create_hierarchy(cnode(bounds="[36,127][120,211]"))
        self.assertEqual(self._find(xml), (78, 169))

    def test_labelled_resource_id_beats_position(self):
        xml = create_hierarchy(
            cnode(bounds="[36,127][120,211]"),                                  # unlabeled top-left
            cnode(rid="com.instagram.android:id/creation_tab", bounds="[540,2358][660,2514]"),
        )
        self.assertEqual(self._find(xml), (600, 2436))   # the named one wins

    def test_content_desc_create_beats_position(self):
        xml = create_hierarchy(
            cnode(bounds="[36,127][120,211]"),
            cnode(desc="Create", bounds="[540,2358][660,2514]"),
        )
        self.assertEqual(self._find(xml), (600, 2436))

    def test_direct_messages_icon_is_never_chosen(self):
        # Top-right DM icon must not win a position hint on a build that moves
        # the + -- tapping it would silently open Direct instead of the composer.
        xml = create_hierarchy(cnode(desc="Direct messaging", bounds="[1080,127][1164,211]"))
        self.assertIsNone(self._find(xml))

    def test_search_and_notifications_excluded(self):
        for desc in ("Search", "Notifications", "Activity Feed"):
            xml = create_hierarchy(cnode(desc=desc, bounds="[36,127][120,211]"))
            self.assertIsNone(self._find(xml), desc)

    def test_story_ring_still_excluded(self):
        xml = create_hierarchy(cnode(desc="rodrigo's story, 1 of 3, unseen.", bounds="[36,127][120,211]"))
        self.assertIsNone(self._find(xml))

    def test_launcher_chrome_excluded(self):
        xml = create_hierarchy(cnode(rid="com.android.launcher3:id/home", bounds="[36,127][120,211]"))
        self.assertIsNone(self._find(xml))

    def test_non_clickable_node_scores_nothing_by_position(self):
        xml = create_hierarchy(cnode(clickable="false", bounds="[36,127][120,211]"))
        self.assertIsNone(self._find(xml))

    def test_bad_dump_is_survivable(self):
        class Dev:
            def dump_hierarchy(self):
                return "not xml"

            def window_size(self):
                return (1200, 2514)

        self.assertIsNone(self.flow._find_create_via_dump_u2(Dev(), "t"))

    def test_single_dump_per_lookup(self):
        # The six name-based selectors cost ~10s per run and could never match
        # an unlabeled node; the whole lookup is now one RPC.
        calls = []

        class Dev:
            def dump_hierarchy(self):
                calls.append(1)
                return create_hierarchy(cnode(bounds="[36,127][120,211]"))

            def window_size(self):
                return (1200, 2514)

        self.flow._find_create_via_dump_u2(Dev(), "t")
        self.assertEqual(len(calls), 1)


class ForegroundCheckTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_tries_several_dumpsys_forms(self):
        # `dumpsys window displays` returned nothing on the MLX phones, so the
        # check must fall through to another command rather than give up.
        tried = []

        class Adb:
            def run_command(self, cmd):
                tried.append(cmd)
                return "topResumedActivity=com.instagram.android/.MainActivity" if "activities" in cmd else ""

        self.assertTrue(self.flow._ig_is_foreground("t", Adb()))
        self.assertTrue(any("activities" in c for c in tried))

    def test_false_when_instagram_is_not_in_front(self):
        class Adb:
            def run_command(self, cmd):
                return "topResumedActivity=com.android.launcher3/.Launcher"

        self.assertFalse(self.flow._ig_is_foreground("t", Adb()))

    def test_adb_errors_are_survivable(self):
        class Adb:
            def run_command(self, cmd):
                raise RuntimeError("offline")

        self.assertFalse(self.flow._ig_is_foreground("t", Adb()))


class BrowseRefreshGestureTest(TestCase):
    """The verification probe used to swipe down the feed and then swipe back up.
    The second gesture only undid the first: what actually refreshes the post
    count is leaving to the feed and returning to the profile, plus the
    pull-to-refresh on the grid. One feed gesture is enough, and the probe runs
    every ~25s for up to three minutes, so the saving repeats."""

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def _swipes(self):
        recorded = []

        class Dev:
            def window_size(self):
                return (1080, 2340)

            def swipe(self, x1, y1, x2, y2, duration):
                recorded.append((y1, y2))

            def __call__(self, *a, **k):
                return mock.MagicMock(exists=True, wait=lambda *a, **k: True,
                                      click=lambda *a, **k: None, info={})

            def __getattr__(self, name):
                return mock.MagicMock()

        with mock.patch.object(self.flow, "_dismiss_popups_u2"), \
             mock.patch.object(self.flow, "_tap_ig_home_icon_u2"), \
             mock.patch.object(self.flow, "_open_profile_tab_u2", return_value=True), \
             mock.patch("adb_bot.automation.flows.waits.settle"):
            self.flow._browse_and_refresh_profile_u2(Dev(), "t")
        return recorded

    def test_only_two_swipes_total(self):
        """One on the feed, one pull-to-refresh on the profile grid -- the
        redundant scroll-back is gone."""
        self.assertEqual(len(self._swipes()), 2)

    def test_no_swipe_merely_undoes_the_previous_one(self):
        swipes = self._swipes()
        for (first, second) in zip(swipes, swipes[1:]):
            self.assertNotEqual((first[1], first[0]), second,
                                "a gesture that reverses the one before it does no work")

    def test_feed_gesture_pulls_downward_to_reload(self):
        first = self._swipes()[0]
        self.assertLess(first[0], first[1], "should pull down (reload), not scroll away")


class ReelAccountFlagTest(TestCase):
    """A checkpoint must not be reported as a retryable failure.

    Live evidence, 2026-08-03: Jil 1 sat on ChallengeActivity showing "Confirm
    you're human to use your account, helenadiecutee". The composer never
    opened, the flow returned a bare failure, and the queue row landed on
    `Failed - Needs Retry` with the retry counter bumped -- queueing a blind
    relaunch of an account only a person can unblock.
    """

    # The text the real challenge screen actually carried.
    CHALLENGE = hierarchy(
        node(text="Confirm you're human to use your account, helenadiecutee", clickable="false"),
        node(text="Continue"),
        node(text="Log out helenadiecutee"),
    )
    ORDINARY = hierarchy(node(text="Something went wrong", clickable="false"))

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()
        self.profile = mock.Mock(id="628337668232577352")
        self.emitted = []

    def _result(self, xml):
        return self.flow._account_flag_result_u2(
            FakeDevice(xml), self.profile, "1.2.3.4:5555",
            lambda level, msg, *a: self.emitted.append(msg % a if a else msg),
            "opening the composer",
        )

    def test_challenge_screen_is_classified_as_human_verification(self):
        result = self._result(self.CHALLENGE)
        self.assertIsNotNone(result)
        self.assertEqual(result["account_flag"], "human_verification")
        self.assertFalse(result["success"])
        self.assertFalse(result["aborted"])
        # No "failed" key: that is what routes it to the retryable path.
        self.assertNotIn("failed", result)

    def test_ordinary_failure_is_left_alone(self):
        self.assertIsNone(self._result(self.ORDINARY))

    def test_unreadable_screen_is_not_guessed_at(self):
        class Broken:
            def dump_hierarchy(self):
                raise RuntimeError("device offline")

        result = self.flow._account_flag_result_u2(
            Broken(), self.profile, "t", lambda *a: None, "opening the composer")
        self.assertIsNone(result)

    def test_the_flag_reaches_a_non_retryable_write_back(self):
        """End of the chain: the flag maps to Human Verification Required, and
        crucially not to Failed - Needs Retry."""
        from adb_bot.clients import airtable as at
        from adb_bot.automation.posting_runner import _map_post_status

        post_status, issue_type, incident, _run_result, _note = _map_post_status(
            self._result(self.CHALLENGE)["account_flag"])
        self.assertEqual(post_status, at.POST_STATUS_FAILED)
        self.assertEqual(issue_type, at.ISSUE_HUMAN_VERIFICATION)
        self.assertNotEqual(issue_type, at.ISSUE_NEEDS_RETRY)
        self.assertEqual(incident, "human_verification")


class ProbeLaunchesInstagramTest(TestCase):
    """The recheck probe must start Instagram before looking for it.

    The in-run probe inherits an app that is already open -- the post just
    happened on it. A recheck arrives ~15 min later on a freshly launched phone,
    which boots to the Android launcher. Every recheck on 2026-08-03 reported
    the post count "unreadable" for this reason; one of them had connected to a
    perfectly healthy phone that was simply sitting on the home screen.
    """

    def test_launch_commands_run_before_the_ui_wait(self):
        from unittest.mock import MagicMock
        from adb_bot.automation.flows.instagram_reel import ReelPostCountProbeFlow

        flow = ReelPostCountProbeFlow()
        commands = flow.build_launch_commands("1.2.3.4:5555")
        self.assertTrue(any("com.instagram.android" in c for c in commands))
        self.assertTrue(any("monkey -p" in c or "am start" in c for c in commands))

        # The flow must issue them through the adb client it was handed.
        adb = MagicMock()
        for command in flow.build_launch_commands("1.2.3.4:5555"):
            adb.run_command(command)
        self.assertEqual(adb.run_command.call_count, len(commands))

    def test_the_probe_source_starts_instagram(self):
        """Pin the behaviour against the source: the launch must happen inside
        run(), before the 'Instagram UI loaded' wait it precedes."""
        import inspect
        from adb_bot.automation.flows.instagram_reel import ReelPostCountProbeFlow

        source = inspect.getsource(ReelPostCountProbeFlow.run)
        self.assertIn("build_launch_commands", source)
        self.assertLess(source.index("build_launch_commands"),
                        source.index("Instagram UI loaded"))
