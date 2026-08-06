"""When the running queue loop is grid-only, the schedule table says so.

The loop on this box fills a fixed grid from `--slots` and contains no
per-model-times code at all. Before this, a model with a blank Reel Post Times
was reported as "flexible, up to 7 a day" -- a runner behaviour nothing on the
machine implements -- and the fallback grid printed was `DEFAULT_SLOT_TIMES`
(seven slots from 09:00) rather than the three the unit actually runs. Both
numbers were wrong on the same row, on a page whose whole job is to say what is
happening.

`_model_inputs` is stubbed rather than faked through `collect_targets`: what is
under test is how the running loop's grid overrides the Airtable field, not how
targets are counted.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from adb_bot.automation import report

INPUTS = {
    "counts": {"nikki": 14, "laila": 1},
    "schedules": {"nikki": SimpleNamespace(times=[], per_day=None),
                  "laila": SimpleNamespace(times=["09:00", "17:00"], per_day=None)},
    "error": "",
}
GRID_ONLY = {"slots": ["15:00", "18:00", "21:00"], "per_model": False, "known": True}
PER_MODEL = {"slots": ["15:00", "18:00", "21:00"], "per_model": True, "known": True}


class GridOnlyScheduleTest(unittest.TestCase):

    def setUp(self):
        report.invalidate_cache()
        self.addCleanup(report.invalidate_cache)

    def _run(self, grid):
        now = datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        with mock.patch.object(report, "_model_inputs", return_value=INPUTS):
            return report.model_schedules(object(), content={}, now=now, grid=grid)

    def _model(self, out, name):
        return next(m for m in out["models"] if m["model"] == name)

    def test_the_fallback_is_the_units_grid_not_the_code_default(self):
        self.assertEqual(self._run(GRID_ONLY)["fallback"], ["15:00", "18:00", "21:00"])

    def test_a_model_with_no_times_gets_the_grid_not_flexible_mode(self):
        nikki = self._model(self._run(GRID_ONLY), "Nikki")
        self.assertFalse(nikki["flexible"])
        self.assertEqual(nikki["times"], ["15:00", "18:00", "21:00"])
        self.assertEqual(nikki["per_day"], 3)

    def test_per_model_times_are_overridden_by_a_grid_only_loop(self):
        """The loop cannot read them, so the page must not promise them."""
        self.assertEqual(self._model(self._run(GRID_ONLY), "Laila")["times"],
                         ["15:00", "18:00", "21:00"])

    def test_the_table_stops_claiming_per_model_support(self):
        self.assertFalse(self._run(GRID_ONLY)["per_model"])

    def test_a_per_model_runner_still_honours_the_airtable_times(self):
        out = self._run(PER_MODEL)
        self.assertTrue(out["per_model"])
        self.assertEqual(self._model(out, "Laila")["times"], ["09:00", "17:00"])
        self.assertTrue(self._model(out, "Nikki")["flexible"])

    def test_every_model_with_profiles_is_listed(self):
        out = self._run(GRID_ONLY)
        self.assertIn("Nikki", {m["model"] for m in out["models"]})
        self.assertEqual(self._model(out, "Nikki")["profiles"], 14)


if __name__ == "__main__":
    unittest.main()
