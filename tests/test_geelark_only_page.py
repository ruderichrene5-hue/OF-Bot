"""A genuinely separate page (2026-08-31, explicit request) -- not just a
different default tab on the shared MLX+Geelark dashboard. render(...,
geelark_only=True) must drop every MLX tab/panel/banner entirely, while the
shared page (geelark_only=False, the default) keeps its original MLX-first
default and everything else unchanged.
"""

import unittest

from adb_bot.automation import report_html
import tests.test_report as tr
from tests.test_geelark_dashboard import _geelark, _phone


class GeelarkOnlyPageTest(unittest.TestCase):
    def _data(self):
        data = tr.RenderTest._data(tr.RenderTest())
        data["geelark"] = _geelark(phones=[_phone()])
        return data

    def test_no_mlx_tabs_or_panels_are_present(self):
        page = report_html.render(self._data(), geelark_only=True)
        for tab_id in ("tab-server", "tab-human", "tab-posts", "tab-schedules",
                      "tab-warmup", "tab-profiles", "tab-technical"):
            self.assertNotIn(f'id="{tab_id}"', page)
        for panel_id in ("panel-server", "panel-human", "panel-posts",
                         "panel-schedules", "panel-warmup", "panel-profiles",
                         "panel-technical"):
            self.assertNotIn(f'id="{panel_id}"', page)

    def test_geelark_tabs_are_still_present_and_default_selected(self):
        page = report_html.render(self._data(), geelark_only=True)
        self.assertIn('id="tab-geelark" checked', page)
        self.assertIn('id="panel-geelark"', page)
        self.assertIn('id="panel-geelark-accounts"', page)

    def test_never_raises_even_with_no_mlx_derived_data_at_all(self):
        """geelark_only skips the MLX banner computation entirely -- must
        not depend on needs_human/handoff/health/timers being present."""
        data = {"day": "2026-08-31", "generated_at": "12:00",
               "geelark": _geelark(phones=[_phone()])}
        page = report_html.render(data, geelark_only=True)  # must not raise
        self.assertIn("panel-geelark", page)

    def test_the_shared_page_still_defaults_to_server_not_geelark(self):
        """The shared :8088 page must keep its original MLX-first default --
        the separate page is what changed, not this one."""
        page = report_html.render(self._data())
        self.assertIn('id="tab-server" checked', page)
        self.assertNotIn('id="tab-geelark-accounts" checked', page)

    def test_the_shared_page_still_has_every_mlx_panel(self):
        page = report_html.render(self._data())
        for panel_id in ("panel-server", "panel-human", "panel-posts",
                         "panel-schedules", "panel-warmup", "panel-profiles",
                         "panel-technical", "panel-geelark", "panel-geelark-accounts"):
            self.assertIn(f'id="{panel_id}"', page)
