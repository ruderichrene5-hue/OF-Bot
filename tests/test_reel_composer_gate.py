"""The composer check must not accept the Reels *viewer*.

`_GALLERY_SELECTORS` includes a bare `(?i)^reels?$` text match, which is the only
thing that finds the composer's destination tab on some builds -- and which also
matches the Reels viewer's action-bar title and the Reels tab in the bottom nav.
Accepting one of those is worse than finding nothing: the flow believes the
composer is open, walks on, fails to find Next and Share, blind-taps their usual
coordinates into a video feed, and records a share that never happened. The
ledger then blocks a re-post until the deferred recheck disproves it.

Measured on 2026-08-06: 50 of 57 composer checks matched an impostor and every
one of those runs failed to post.
"""

from unittest import TestCase

from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow

IG = "com.instagram.android:id"


class FakeSel:
    def __init__(self, present, resource_name=None):
        self._present = present
        self.info = {"resourceName": resource_name} if present else {}

    @property
    def exists(self):
        return self._present


class FakeDevice:
    """Matches a selector only if the screen carries a node satisfying it.

    `screen` maps a matchable value ("Reels") to the resource-id showing it, so a
    test can put the same *text* on different elements -- which is the whole
    point of the bug under test.
    """

    def __init__(self, screen: dict):
        self.screen = screen
        self.queries = []

    def __call__(self, **kwargs):
        self.queries.append(kwargs)
        for value, resource_name in self.screen.items():
            if self._matches(kwargs, value, resource_name):
                return FakeSel(True, resource_name)
        return FakeSel(False)

    @staticmethod
    def _matches(kwargs, value, resource_name):
        if "resourceId" in kwargs:
            return kwargs["resourceId"] == resource_name
        if "textMatches" in kwargs:
            import re
            return bool(re.match(kwargs["textMatches"], value))
        if "descriptionMatches" in kwargs:
            import re
            return bool(re.match(kwargs["descriptionMatches"], value))
        if "textContains" in kwargs:
            return kwargs["textContains"] in value
        if "descriptionStartsWith" in kwargs:
            return value.startswith(kwargs["descriptionStartsWith"])
        return False


class ComposerGateTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def _check(self, screen):
        d = FakeDevice(screen)
        return self.flow._first_present(
            d, list(self.flow._GALLERY_SELECTORS), timeout=0,
            purpose="reel composer / gallery",
            reject_ids=self.flow._NOT_THE_COMPOSER_IDS,
        )

    def test_the_real_composer_is_accepted(self):
        # cam_dest_clips is the composer's own REEL destination tab.
        self.assertIsNotNone(self._check({"Reels": f"{IG}/cam_dest_clips"}))

    def test_the_reels_viewer_title_is_rejected(self):
        # The screen the failing runs were actually sitting on.
        self.assertIsNone(self._check({"Reels": f"{IG}/clips_viewer_action_bar_title"}))

    def test_the_reels_nav_tab_is_rejected(self):
        self.assertIsNone(self._check({"Reels": f"{IG}/clips_tab"}))

    def test_the_profile_tab_is_rejected(self):
        self.assertIsNone(self._check({"Reels": f"{IG}/profile_tab_icon_view"}))

    def test_an_impostor_does_not_hide_the_real_composer(self):
        # Both on screen at once: rejecting one must not abandon the scan. This
        # is why a rejected match continues the loop instead of returning None.
        self.assertIsNotNone(self._check({
            "Reels": f"{IG}/clips_viewer_action_bar_title",
            "Recents": f"{IG}/gallery_folder_menu_tv",
        }))

    def test_an_unknown_id_still_passes(self):
        # The denylist names proven impostors; it is not an allowlist of every
        # id a composer might have, so an unrecognised build still works.
        self.assertIsNotNone(self._check({"Reels": f"{IG}/some_new_composer_id"}))

    def test_an_element_with_no_id_still_passes(self):
        self.assertIsNotNone(self._check({"Reels": None}))

    def test_an_empty_screen_is_still_absent(self):
        self.assertIsNone(self._check({}))


def dump(*nodes) -> str:
    body = "".join(
        '<node resource-id="{rid}" content-desc="{desc}" class="{cls}" '
        'clickable="{clickable}" bounds="{bounds}"/>'.format(**n)
        for n in nodes
    )
    return f'<?xml version="1.0"?><hierarchy>{body}</hierarchy>'


def dnode(rid="", desc="", cls="android.widget.ImageView", clickable="true", bounds="[0,0][100,100]"):
    return {"rid": rid, "desc": desc, "cls": cls, "clickable": clickable, "bounds": bounds}


class DumpDevice:
    def __init__(self, xml, size=(1080, 2400)):
        self.xml = xml
        self._size = size

    def dump_hierarchy(self):
        return self.xml

    def window_size(self):
        return self._size


class CreateButtonDumpTest(TestCase):
    """The '+' locator must not pick a full-screen layout.

    Scoring awards +3 for "camera" anywhere in the resource-id, and this build
    wraps the whole screen in ids that contain it. With nothing scoring higher
    the wrapper won, and the flow tapped its centre -- the middle of the feed,
    which opens whatever post is there. That is the first domino: it is how a
    run ends up in the Reels viewer thinking it opened the composer.
    """

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_a_full_screen_camera_wrapper_is_not_the_create_button(self):
        # Verbatim from the 2026-08-06 logs: won 48 runs, centre (540, 1116).
        d = DumpDevice(dump(dnode(
            rid=f"{IG}/bottom_sheet_camera_container",
            cls="android.widget.FrameLayout", bounds="[0,0][1080,2232]")))
        self.assertIsNone(self.flow._find_create_via_dump_u2(d, "t"))

    def test_the_real_create_button_still_wins(self):
        # profile_header_create_button [768,72][912,240] -- the one that worked.
        d = DumpDevice(dump(dnode(
            rid=f"{IG}/profile_header_create_button", bounds="[768,72][912,240]")))
        self.assertEqual(self.flow._find_create_via_dump_u2(d, "t"), (840, 156))

    def test_the_real_button_wins_even_beside_the_wrapper(self):
        d = DumpDevice(dump(
            dnode(rid=f"{IG}/bottom_sheet_camera_container",
                  cls="android.widget.FrameLayout", bounds="[0,0][1080,2232]"),
            dnode(rid=f"{IG}/profile_header_create_button", bounds="[768,72][912,240]"),
        ))
        self.assertEqual(self.flow._find_create_via_dump_u2(d, "t"), (840, 156))

    def test_a_wide_but_short_row_is_still_allowed(self):
        # The guard rejects only things large on BOTH axes, so a full-width nav
        # row holding the + is not thrown away with the scenery.
        d = DumpDevice(dump(dnode(
            rid=f"{IG}/creation_tab", bounds="[0,2200][1080,2340]")))
        self.assertEqual(self.flow._find_create_via_dump_u2(d, "t"), (540, 2270))


class GallerySelectorOrderTest(TestCase):
    def test_the_composer_id_is_tried_first(self):
        # When the composer is genuinely open, the unambiguous id should settle
        # it before any text match gets the chance to be wrong.
        first = InstagramReelUploadU2Flow._GALLERY_SELECTORS[0]
        self.assertEqual(first, {"resourceId": f"{IG}/cam_dest_clips"})


class LoneNextIsNotTheComposerTest(TestCase):
    """"Next" alone must not open the gate.

    The denylist cannot help here: the impostor caught on 2026-08-11 carried no
    resource-id at all (text='Next' desc=None id=None class='android.view.View'),
    and you cannot deny-list something you cannot name. 13 of that day's 63
    composer checks passed on it, and every one then failed at the REEL-mode
    guard -- the screen was never the composer.
    """

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def _check(self, screen):
        return self.flow._first_present(
            FakeDevice(screen), list(self.flow._GALLERY_SELECTORS), timeout=0,
            purpose="reel composer / gallery",
            reject_ids=self.flow._NOT_THE_COMPOSER_IDS,
        )

    def test_a_bare_next_is_not_the_composer(self):
        self.assertIsNone(self._check({"Next": None}))

    def test_next_is_not_a_gallery_selector_at_all(self):
        self.assertNotIn({"textMatches": "(?i)^next$"},
                         list(InstagramReelUploadU2Flow._GALLERY_SELECTORS))

    def test_next_beside_a_real_gallery_still_passes(self):
        # Dropping the selector must not cost us the genuine composer: the
        # gallery's own cells are what prove it, and they are still listed.
        self.assertIsNotNone(self._check({"Next": None, "Video, 12 seconds": None}))

    def test_next_is_still_how_the_next_button_is_found(self):
        # Only the *gate* stopped trusting "Next". Tapping Next must not regress.
        self.assertIn({"textMatches": "(?i)^next$"},
                      list(InstagramReelUploadU2Flow._NEXT_SELECTORS))


class CreateButtonEvidenceTest(TestCase):
    """The '+' locator must not tap a button just for being a button.

    Scoring started at 0 and the best candidate won whatever it was worth, so
    "clickable, and a button" -- one point, true of dozens of nodes on any
    screen -- won unopposed. On 2026-08-11 it elected desc='next' at the bottom
    of the screen and the queue row was lost before the composer ever opened.
    """

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_a_bare_clickable_button_is_not_evidence(self):
        # Verbatim from the 2026-08-11 logs: score=1, centre (540, 2139).
        d = DumpDevice(dump(dnode(
            rid="", desc="next", cls="android.widget.Button",
            bounds="[440,2100][640,2178]")))
        self.assertIsNone(self.flow._find_create_via_dump_u2(d, "t"))

    def test_the_floor_alone_rejects_an_unlabelled_button(self):
        # Same shape with no content-desc, so the composer-button denylist can
        # not be what rejects it -- this is the score floor doing the work.
        d = DumpDevice(dump(dnode(
            rid="", desc="", cls="android.widget.Button",
            bounds="[440,2100][640,2178]")))
        self.assertIsNone(self.flow._find_create_via_dump_u2(d, "t"))

    def test_a_composer_button_is_never_the_create_button(self):
        for desc in ("next", "share", "post", "continue", "done", "cancel"):
            with self.subTest(desc=desc):
                d = DumpDevice(dump(dnode(
                    rid="", desc=desc, cls="android.widget.Button",
                    bounds="[40,60][200,180]")))  # top-left, so it would score 5
                self.assertIsNone(self.flow._find_create_via_dump_u2(d, "t"))

    def test_new_post_still_scores_despite_containing_post(self):
        # The composer-button denylist matches the whole desc, not a substring,
        # so the legitimate "New post" is untouched.
        d = DumpDevice(dump(dnode(desc="New post", bounds="[768,72][912,240]")))
        self.assertEqual(self.flow._find_create_via_dump_u2(d, "t"), (840, 156))

    def test_a_top_left_button_still_wins_on_the_position_hint(self):
        # 1 (clickable button) + 4 (top-left) = 5, comfortably over the floor.
        d = DumpDevice(dump(dnode(rid="", desc="", bounds="[40,60][200,180]")))
        self.assertEqual(self.flow._find_create_via_dump_u2(d, "t"), (120, 120))

    def test_a_camera_button_still_meets_the_floor(self):
        # 3 ("camera" in id) + 1 (clickable button) = 4, exactly the floor.
        d = DumpDevice(dump(dnode(
            rid=f"{IG}/camera_shutter", bounds="[900,2000][1000,2100]")))
        self.assertEqual(self.flow._find_create_via_dump_u2(d, "t"), (950, 2050))

    def test_the_floor_is_the_lowest_evidenced_score(self):
        self.assertEqual(InstagramReelUploadU2Flow._MIN_CREATE_SCORE, 4)


class ComposerReopenTest(TestCase):
    """A run that lands on the wrong screen must reset and try again.

    Getting this wrong costs more than the post. A failed run leaves Instagram
    on whatever it wandered into, so the *next* run starts there and fails the
    same way -- which is how a healthy phone burns all three queue retries and
    ends up flagged `Retries Exhausted`.
    """

    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()
        self.reset_calls = []
        self.flow._recover_to_instagram_u2 = lambda *a, **k: True
        self.flow._dismiss_popups_u2 = lambda *a, **k: None
        self.flow._tap_ig_home_icon_u2 = (
            lambda *a, **k: self.reset_calls.append("home") or True)

    def _run(self, outcomes, attempts=2):
        results = list(outcomes)
        self.flow._open_reel_composer_once_u2 = lambda *a, **k: results.pop(0)
        from unittest.mock import patch
        with patch("adb_bot.automation.flows.instagram_reel.waits.settle"):
            return self.flow._open_reel_composer_u2(
                DumpDevice(dump()), "t", None, attempts=attempts)

    def test_a_first_attempt_that_works_does_not_reset(self):
        self.assertTrue(self._run([True]))
        self.assertEqual(self.reset_calls, [])

    def test_a_failed_attempt_resets_and_retries(self):
        self.assertTrue(self._run([False, True]))
        self.assertEqual(self.reset_calls, ["home"])

    def test_giving_up_after_the_last_attempt(self):
        self.assertFalse(self._run([False, False]))
        # Reset between attempts only -- never after the final one, which would
        # be a pointless extra second on a run that is already over.
        self.assertEqual(self.reset_calls, ["home"])

    def test_a_single_attempt_never_resets(self):
        self.assertFalse(self._run([False], attempts=1))
        self.assertEqual(self.reset_calls, [])
