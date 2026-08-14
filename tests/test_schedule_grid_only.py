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

from adb_bot.automation import report, report_html

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


def _model(name, profiles, known=True, raw_folder=None):
    return {"model": name, "profiles": profiles, "known": known, "flexible": False,
            "times": ["15:00", "18:00", "21:00"], "posts_per_day": profiles * 3,
            "per_day": 3, "free": 0, "next": "15:00", "raw_folder": raw_folder}


class StrayAndIdleRenderTest(unittest.TestCase):
    """A model posting three times a day must not be flagged as a fault.

    Nikki has 14 Active profiles and no Models row, because the Airtable row for
    the same person is spelled "Corina". That combination produced two alarming
    and untrue lines: a red "not a model" pill saying Nikki "stays flexible" --
    a runner mode this box does not have -- and Corina listed among models with
    "nothing scheduled", while the profiles drawing on its footage posted all day.
    """

    def _render(self, per_model=False):
        return report_html._section_schedules({
            "models": [_model("Nikki", 14, known=False, raw_folder="Corina"),
                       _model("Corina", 0), _model("Lou", 0), _model("Laila", 7)],
            "timezone": "Europe/Berlin", "server_timezone": "UTC", "same_clock": False,
            "fallback": ["15:00", "18:00", "21:00"], "per_model": per_model, "error": "",
        })

    def test_a_grid_loop_does_not_flag_the_missing_row_as_bad(self):
        page = self._render()
        self.assertNotIn("not a model", page)
        self.assertIn("no Models row", page)

    def test_it_stops_claiming_the_model_stays_flexible(self):
        page = self._render()
        self.assertNotIn("stays flexible", page)
        self.assertIn("costs nothing while the queue loop fills a fixed grid", page)

    def test_a_per_model_runner_still_calls_the_missing_row_a_problem(self):
        page = self._render(per_model=True)
        self.assertIn("stays flexible", page)

    def test_the_alias_model_is_not_listed_as_having_nothing_scheduled(self):
        page = self._render()
        self.assertIn("has no Active profile of its own, but it is not idle", page)
        self.assertIn("its raw footage is what the", page)

    def test_a_genuinely_empty_model_is_still_listed(self):
        self.assertIn("Lou", self._render())

    def test_the_alias_is_excluded_from_the_idle_sentence(self):
        page = self._render()
        tail = page[page.index("nothing is scheduled") - 200:]
        self.assertIn("Lou", tail)
        self.assertNotIn("Corina,", tail)


if __name__ == "__main__":
    unittest.main()
