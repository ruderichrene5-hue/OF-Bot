"""Follower/post-count reading for the account-stats dashboard.

The primary selector was a guess (2026-08-31) until checked live against a
real phone -- that check found the actual build uses a completely different
id scheme than guessed ("profile_header_familiar_followers_value", not
"*_followers_count"), caught only by the content-desc fallback. These tests
pin the real structure found live, plus the originally-guessed scheme, so a
future Instagram build change shows up as a failing test instead of a
silently-empty dashboard column.
"""

from unittest import TestCase

from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow


class FakeNode:
    def __init__(self, text=None, content_desc=None, exists=True):
        self.info = {"text": text, "contentDescription": content_desc}
        self.exists = exists


class FakeDevice:
    """Answers exactly the selector kwargs the count readers use, against a
    fixed set of (resourceId, text, contentDescription) rows."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self, **kwargs):
        rid = kwargs.get("resourceId")
        if rid is not None:
            for row in self.rows:
                if row.get("resourceId") == rid:
                    return FakeNode(row.get("text"), row.get("contentDescription"))
            return FakeNode(exists=False)
        pattern = kwargs.get("resourceIdMatches")
        if pattern is not None:
            import re
            for row in self.rows:
                if row.get("resourceId") and re.match(pattern, row["resourceId"]):
                    return FakeNode(row.get("text"), row.get("contentDescription"))
            return FakeNode(exists=False)
        pattern = kwargs.get("descriptionMatches")
        if pattern is not None:
            import re
            for row in self.rows:
                desc = row.get("contentDescription")
                if desc and re.match(pattern, desc):
                    return FakeNode(row.get("text"), desc)
            return FakeNode(exists=False)
        return FakeNode(exists=False)


# The real structure found live 2026-08-31 on a fresh ("new profiles 98
# Geelark") account -- 0 posts, 0 followers, 0 following, confirmed genuine
# for a never-posted account, not a parse failure.
REAL_FAMILIAR_BUILD_ROWS = [
    {"resourceId": "com.instagram.android:id/profile_header_familiar_post_count_value", "text": "0"},
    {"resourceId": "com.instagram.android:id/profile_header_familiar_followers_value", "text": "0"},
    {"resourceId": "com.instagram.android:id/profile_header_followers_stacked_familiar",
     "contentDescription": "0followers"},
    {"resourceId": "com.instagram.android:id/action_bar_large_title_auto_size", "text": "mike.tanoshi_x"},
]

OLDER_COUNT_STYLE_BUILD_ROWS = [
    {"resourceId": "com.instagram.android:id/row_profile_header_textview_post_count", "text": "87"},
    {"resourceId": "com.instagram.android:id/row_profile_header_textview_followers_count", "text": "1,234"},
]


class FollowerCountRealBuildTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_reads_the_real_familiar_id_scheme(self):
        d = FakeDevice(REAL_FAMILIAR_BUILD_ROWS)
        count = self.flow._read_follower_count_u2(d, "1.2.3.4:5555")
        self.assertIsNotNone(count)
        self.assertEqual(count.value, 0)
        self.assertTrue(count.exact)

    def test_reads_the_older_count_style_scheme(self):
        d = FakeDevice(OLDER_COUNT_STYLE_BUILD_ROWS)
        count = self.flow._read_follower_count_u2(d, "1.2.3.4:5555")
        self.assertIsNotNone(count)
        self.assertEqual(count.value, 1234)

    def test_falls_back_to_content_desc_when_no_id_matches_at_all(self):
        """The deepest fallback -- what actually caught the real build before
        the familiar-scheme selector was added as a primary match."""
        d = FakeDevice([{"resourceId": "com.instagram.android:id/something_unrelated",
                        "contentDescription": "42followers"}])
        count = self.flow._read_follower_count_u2(d, "1.2.3.4:5555")
        self.assertIsNotNone(count)
        self.assertEqual(count.value, 42)

    def test_nothing_readable_returns_none_not_zero(self):
        d = FakeDevice([])
        self.assertIsNone(self.flow._read_follower_count_u2(d, "1.2.3.4:5555"))


class ReadAccountStatsTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()

    def test_reads_handle_posts_and_followers_together(self):
        rows = REAL_FAMILIAR_BUILD_ROWS + [
            {"resourceId": "com.instagram.android:id/profile_tab", "text": ""},
        ]
        d = FakeDevice(rows)
        state = {"d": d, "target": "1.2.3.4:5555", "log": None}
        # _open_profile_tab_u2 waits on a readiness check this fake can't
        # satisfy without patching waits -- exercised for real by the live
        # check script instead; here we only need the profile tab to already
        # register as tappable so the method proceeds to the header reads.
        self.flow._open_profile_tab_u2 = lambda *a, **k: True
        out = self.flow.read_account_stats(state)
        self.assertEqual(out["posts"].value, 0)
        self.assertEqual(out["followers"].value, 0)
        self.assertEqual(out["handle"], "mike.tanoshi_x")

    def test_missing_d_or_target_is_a_safe_no_op(self):
        out = self.flow.read_account_stats({"d": None, "target": None, "log": None})
        self.assertEqual(out, {"handle": None, "followers": None, "posts": None})

    def test_never_raises_even_if_a_probe_blows_up(self):
        class ExplodingDevice:
            def __call__(self, **kwargs):
                raise RuntimeError("adb dropped")
        self.flow._open_profile_tab_u2 = lambda *a, **k: True
        out = self.flow.read_account_stats(
            {"d": ExplodingDevice(), "target": "1.2.3.4:5555", "log": None})
        self.assertEqual(out, {"handle": None, "followers": None, "posts": None})
