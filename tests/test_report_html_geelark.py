"""The GeeLark phones table on the :8088 dashboard gained an IG Handle /
Follower / Posts / Stats-age set of columns (2026-08-31), fed by
account_stats.py. report_html.py otherwise has no test coverage -- this
pins just the new columns' rendering, not the whole section.
"""

import time
from unittest import TestCase

from adb_bot.automation.report_html import _section_geelark


def _phone(**overrides):
    base = {
        "id": "1", "name": "Test Phone", "status": "started", "adb": "active",
        "country": "Germany", "os": "Android 16", "device": "vivo X200",
        "timezone": "Europe/Berlin", "proxy": "1.2.3.4:5555", "tags": ["Active_Posting"],
        "group": "Nikki", "remark": "",
        "ig_handle": "", "ig_followers": -1, "ig_followers_exact": False,
        "ig_posts": -1, "ig_posts_exact": False, "ig_stats_at": None,
    }
    base.update(overrides)
    return base


def _geelark(**phone_overrides):
    return {
        "configured": True, "error": "", "phones": [_phone(**phone_overrides)],
        "counts": {"phones": 1, "running": 1, "stopped": 0, "adb_enabled": 1,
                   "proxies": 0, "gateways": 0},
        "billing": {}, "proxies": [], "tags": [],
    }


class IgStatsColumnsTest(TestCase):
    def test_never_read_shows_em_dashes_not_zeroes(self):
        html = _section_geelark(_geelark())
        self.assertIn('<th>IG Handle</th>', html)
        self.assertIn('<th>Follower</th>', html)
        self.assertIn('<th>Posts</th>', html)
        # -1 (unread) must render as "—", never "-1" or "0" -- a 0 would look
        # like a genuine reading of zero followers.
        self.assertNotIn(">-1<", html)
        self.assertNotIn(">0<", html)

    def test_an_exact_count_renders_plain(self):
        html = _section_geelark(_geelark(ig_handle="alina.sommer74",
                                         ig_posts=87, ig_posts_exact=True))
        self.assertIn("alina.sommer74", html)
        self.assertIn("87", html)
        self.assertNotIn("~87", html)

    def test_a_rounded_count_gets_a_tilde(self):
        html = _section_geelark(_geelark(ig_followers=1234, ig_followers_exact=False))
        self.assertIn("~1,234", html)

    def test_a_genuine_zero_still_renders_as_zero_when_exact(self):
        """A freshly created account really can have 0 posts -- -1 (unread)
        and 0 (read, genuinely empty) must render differently."""
        html = _section_geelark(_geelark(ig_posts=0, ig_posts_exact=True))
        self.assertIn(">0<", html)

    def test_stats_age_is_shown_as_a_relative_duration(self):
        html = _section_geelark(_geelark(ig_stats_at=time.time() - 3661))
        self.assertIn("vor 1h 01m", html)

    def test_missing_stats_fields_degrade_to_em_dash_not_a_crash(self):
        """A phone dict from before this feature existed has none of the
        ig_* keys at all -- must not KeyError."""
        geelark = _geelark()
        del geelark["phones"][0]["ig_handle"]
        del geelark["phones"][0]["ig_followers"]
        del geelark["phones"][0]["ig_posts"]
        del geelark["phones"][0]["ig_stats_at"]
        html = _section_geelark(geelark)  # must not raise
        self.assertIn("Test Phone", html)
