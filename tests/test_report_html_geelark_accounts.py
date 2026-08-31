"""New GeeLark Accounts tab (2026-08-31): per-account overview grouped by
model folder -- handle, followers, IG post count, today's post outcomes
(from the post ledger), and a reel-views column that is explicitly
"not connected" rather than silently empty, since the read mechanism for
it doesn't exist yet.
"""

from unittest import TestCase

from adb_bot.automation.report_html import _section_geelark_accounts


def _phone(**overrides):
    base = {
        "id": "1", "name": "Nikki 1 Geelark", "group": "Nikki",
        "ig_handle": "", "ig_followers": -1, "ig_followers_exact": False,
        "ig_posts": -1, "ig_posts_exact": False,
        "posts_today_confirmed": 0, "posts_today_failed": 0,
        "posts_today_uncertain": 0, "ig_reel_views": None,
    }
    base.update(overrides)
    return base


def _geelark(*phones):
    return {"configured": True, "phones": list(phones) or [_phone()]}


class GroupingTest(TestCase):
    def test_phones_are_grouped_under_their_folder_heading(self):
        html = _section_geelark_accounts(_geelark(
            _phone(name="Nikki 1", group="Nikki"),
            _phone(name="Lea 1", group="Lea"),
        ))
        self.assertIn("Nikki", html)
        self.assertIn("Lea", html)
        # Both group headings present, each with its own table.
        self.assertEqual(html.count("<h3>"), 2)

    def test_a_phone_with_no_folder_gets_its_own_bucket_not_dropped(self):
        html = _section_geelark_accounts(_geelark(_phone(group="")))
        self.assertIn("(no folder)", html)

    def test_not_configured_shows_a_clear_message_not_a_crash(self):
        html = _section_geelark_accounts({"configured": False})
        self.assertIn("credentials", html)


class TodayCellTest(TestCase):
    def test_no_posts_today_shows_an_em_dash(self):
        html = _section_geelark_accounts(_geelark(_phone()))
        self.assertIn(">—<", html)

    def test_confirmed_failed_and_uncertain_all_render(self):
        html = _section_geelark_accounts(_geelark(_phone(
            posts_today_confirmed=2, posts_today_failed=1, posts_today_uncertain=1)))
        self.assertIn("2 ok", html)
        self.assertIn("1 failed", html)
        self.assertIn("1 unsicher", html)


class ViewsColumnTest(TestCase):
    def test_views_is_explicitly_not_connected_not_a_blank_cell(self):
        html = _section_geelark_accounts(_geelark(_phone(ig_reel_views=None)))
        self.assertIn("noch nicht verbunden", html)

    def test_a_real_views_value_renders_once_the_data_exists(self):
        """Not wired up as of this commit, but the column must already be
        ready to show a real number the moment it is."""
        html = _section_geelark_accounts(_geelark(_phone(ig_reel_views=4200)))
        self.assertIn("4,200", html)
        self.assertNotIn("noch nicht verbunden", html)


class FollowerPostCountTest(TestCase):
    def test_unread_is_an_em_dash_not_minus_one(self):
        html = _section_geelark_accounts(_geelark(_phone(ig_followers=-1, ig_posts=-1)))
        self.assertNotIn(">-1<", html)

    def test_rounded_gets_a_tilde(self):
        html = _section_geelark_accounts(_geelark(
            _phone(ig_followers=5000, ig_followers_exact=False)))
        self.assertIn("~5,000", html)

    def test_exact_has_no_tilde(self):
        html = _section_geelark_accounts(_geelark(
            _phone(ig_posts=42, ig_posts_exact=True)))
        self.assertIn(">42<", html)
