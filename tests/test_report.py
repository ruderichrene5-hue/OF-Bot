"""The operational report: it must be read-only, honest, and never crash.

The page exists to be looked at when something is wrong, so the cases that
matter most are the degraded ones -- Airtable unreachable, no log file, a run
still in flight, phones open that no slot accounts for.
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from adb_bot.automation import report, report_html


SAMPLE_LOG = """\
2026-08-05 16:12:54,455 | INFO | Posting-queue plan: 48 due, 0 skipped
2026-08-05 16:12:54,465 | INFO | Posting 46 profile(s), up to 10 at a time (rolling, global ceiling 12)
2026-08-05 16:34:01,154 | INFO | Posting run complete (48 post(s)); MLX launch health: 53 attempt(s), 48 ok, 5 MLX-side 500 (9.4%), 0 our-side/other failure(s), 2 queue retry(s) burned by MLX 500s [5 x 'failed to get profiles start urls']
2026-08-05 16:34:06,122 | INFO | Posting-queue plan: 2 due, 0 skipped
2026-08-05 16:34:06,123 | INFO | Posting 2 profile(s), up to 10 at a time (rolling, global ceiling 12)
2026-08-05 16:39:39,273 | INFO | Posting run complete (2 post(s)); MLX launch health: 2 attempt(s), 2 ok, 0 MLX-side 500 (0.0%), 0 our-side/other failure(s), 0 queue retry(s) burned by MLX 500s
2026-08-04 21:00:00,000 | INFO | Posting 9 profile(s), up to 10 at a time (rolling, global ceiling 12)
2026-08-04 21:10:00,000 | INFO | Posting run complete (9 post(s))
"""


class ParsePostingRunsTest(unittest.TestCase):
    def _log(self, text=SAMPLE_LOG):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_pairs_start_and_completion(self):
        runs = report.parse_posting_runs(self._log(), day="2026-08-05")
        self.assertEqual(len(runs), 2)
        first = runs[0]
        self.assertEqual((first.planned, first.posts), (46, 48))
        self.assertEqual(first.seconds, 21 * 60 + 7)
        self.assertAlmostEqual(first.seconds_per_post, 1267 / 48, places=3)

    def test_reads_launch_health_off_the_summary_line(self):
        run = report.parse_posting_runs(self._log(), day="2026-08-05")[0]
        self.assertEqual((run.attempts, run.ok, run.mlx_500), (53, 48, 5))
        self.assertEqual(run.mlx_rate, 9.4)
        self.assertEqual((run.other_failures, run.retries_burned), (0, 2))

    def test_a_summary_without_health_counters_still_parses(self):
        """Runs from before the counters existed must not break the page."""
        runs = report.parse_posting_runs(self._log(), day="2026-08-04")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].posts, 9)
        self.assertEqual(runs[0].attempts, 0)

    def test_a_run_still_in_flight_is_reported_not_dropped(self):
        text = SAMPLE_LOG + ("2026-08-05 18:00:00,000 | INFO | Posting 7 profile(s), "
                             "up to 10 at a time (rolling, global ceiling 12)\n")
        runs = report.parse_posting_runs(self._log(text), day="2026-08-05")
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[-1].finished, "")
        self.assertEqual(runs[-1].seconds, 0.0)
        self.assertEqual(runs[-1].seconds_per_post, 0.0)   # no divide-by-zero

    def test_ansi_colour_in_the_log_does_not_hide_a_run(self):
        coloured = ("2026-08-05 16:12:54,465 | \x1b[32mINFO\x1b[0m | Posting 3 profile(s), up to 10\n"
                    "2026-08-05 16:20:54,465 | \x1b[32mINFO\x1b[0m | Posting run complete (3 post(s))\n")
        runs = report.parse_posting_runs(self._log(coloured), day="2026-08-05")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].posts, 3)

    def test_a_missing_log_is_empty_not_an_error(self):
        self.assertEqual(report.parse_posting_runs("/nonexistent/nope.log"), [])

    def test_rotated_backups_are_read_oldest_first(self):
        """The 2026-08-05 case: the log rolled at 17:26 and took the day's runs
        with it, so the page claimed 0 runs an hour after 48 posts went out."""
        import os, time
        base = Path(self._log(""))
        early = ("2026-08-05 16:12:54,465 | INFO | Posting 46 profile(s), up to 10\n"
                 "2026-08-05 16:34:01,154 | INFO | Posting run complete (48 post(s))\n")
        late = ("2026-08-05 17:26:50,856 | INFO | Posting 2 profile(s), up to 10\n"
                "2026-08-05 17:30:00,869 | INFO | Posting run complete (2 post(s))\n")
        backup = base.with_name(base.name + ".1")
        backup.write_text(early)
        self.addCleanup(lambda: backup.unlink(missing_ok=True))
        base.write_text(late)

        runs = report.parse_posting_runs(str(base), day="2026-08-05")
        self.assertEqual([r.posts for r in runs], [48, 2], "backup was not read, or read out of order")

    def test_a_run_split_across_a_rotation_is_still_paired(self):
        base = Path(self._log(""))
        backup = base.with_name(base.name + ".1")
        backup.write_text("2026-08-05 16:12:54,465 | INFO | Posting 46 profile(s), up to 10\n")
        self.addCleanup(lambda: backup.unlink(missing_ok=True))
        base.write_text("2026-08-05 16:34:01,154 | INFO | Posting run complete (48 post(s))\n")

        runs = report.parse_posting_runs(str(base), day="2026-08-05")
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0].planned, runs[0].posts), (46, 48))

    def test_backups_older_than_the_reported_day_are_skipped(self):
        import os, time
        base = Path(self._log("2026-08-05 16:12:54,465 | INFO | Posting 1 profile(s), up to 10\n"
                              "2026-08-05 16:13:54,465 | INFO | Posting run complete (1 post(s))\n"))
        stale = base.with_name(base.name + ".1")
        stale.write_text("2026-08-01 10:00:00,000 | INFO | Posting 9 profile(s), up to 10\n"
                         "2026-08-01 10:10:00,000 | INFO | Posting run complete (9 post(s))\n")
        self.addCleanup(lambda: stale.unlink(missing_ok=True))
        old = time.mktime(time.strptime("2026-08-01", "%Y-%m-%d"))
        os.utime(stale, (old, old))

        runs = report.parse_posting_runs(str(base), day="2026-08-05")
        self.assertEqual([r.posts for r in runs], [1])


class ContentStockTest(unittest.TestCase):
    def test_groups_ready_variants_by_model_from_the_file_path(self):
        class FakeAirtable:
            def list_ready_variants(self):
                return [
                    {"file_path": "/opt/adbbot/spoofed/Nikki/run4/a.mp4"},
                    {"file_path": "/opt/adbbot/spoofed/Nikki/run4/b.mp4"},
                    {"file_path": "/opt/adbbot/spoofed/Jil/run4/c.mp4"},
                    {"file_path": None},
                ]

        stock = report.content_stock(FakeAirtable())
        self.assertEqual(stock["ready"], 4)
        self.assertEqual(stock["by_model"], {"Jil": 1, "Nikki": 2, "unknown": 1})

    def test_folder_case_does_not_split_one_model_in_two(self):
        """Both spellings exist on disk; two half-sized rows would mislead."""
        class FakeAirtable:
            def list_ready_variants(self):
                return [{"file_path": "/opt/adbbot/spoofed/Jasmin/run4/a.mp4"},
                        {"file_path": "/opt/adbbot/spoofed/jasmin/run5/b.mp4"}]

        self.assertEqual(report.content_stock(FakeAirtable())["by_model"], {"Jasmin": 2})

    def test_an_airtable_failure_is_survivable(self):
        class Broken:
            def list_ready_variants(self):
                raise RuntimeError("429")

        self.assertEqual(report.content_stock(Broken()), {"ready": 0, "by_model": {}})


class QueueTodayTest(unittest.TestCase):
    ROWS = [
        {"id": "r1", "fields": {"Scheduled DateTime": "2026-08-05T16:00:00.000Z",
                                "Post Status": "Posted", "Name": "Jil 1 / 18:00"}},
        {"id": "r2", "fields": {"Scheduled DateTime": "2026-08-05T16:00:00.000Z",
                                "Post Status": "Failed", "Name": "Jil 2 / 18:00",
                                "Issue Type": "Retries Exhausted", "Retry Count": 3,
                                "Notes": "device unreachable"}},
        {"id": "r3", "fields": {"Scheduled DateTime": "2026-08-04T21:00:00.000Z",
                                "Post Status": "Posted", "Name": "yesterday"}},
    ]

    def _queue(self):
        class FakeAirtable:
            def list_queue_rows(inner):
                return self.ROWS

        return report.queue_today(FakeAirtable(), "2026-08-05")

    def test_only_todays_rows_are_counted(self):
        data = self._queue()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["by_status"], {"Posted": 1, "Failed": 1})

    def test_failures_carry_what_a_person_needs_to_act(self):
        failure = self._queue()["failures"][0]
        self.assertEqual(failure["name"], "Jil 2 / 18:00")
        self.assertEqual(failure["issue"], "Retries Exhausted")
        self.assertEqual(failure["retries"], 3)
        self.assertEqual(failure["slot"], "16:00")

    def test_rows_are_grouped_by_scheduled_time(self):
        self.assertEqual(self._queue()["by_slot"], {"16:00": {"Posted": 1, "Failed": 1}})


class CollectTest(unittest.TestCase):
    def setUp(self):
        report.invalidate_cache()
        self.addCleanup(report.invalidate_cache)

    def _patched(self, **overrides):
        defaults = {
            "running_now": lambda: {"loops": [("posting", "active")], "active_loops": ["posting"],
                                    "profiles": ["Jil 1"], "slots_held": 1, "slot_ceiling": 12,
                                    "phones": 1, "agent_up": True},
            "parse_posting_runs": lambda **kw: [],
            "ledger_today": lambda **kw: {"total": 0, "by_status": {}, "verify_seconds": 0.0},
            "health": lambda: {"loops": [], "bad": []},
            "recent_alerts": lambda *a, **k: [],
        }
        defaults.update(overrides)
        return [mock.patch.object(report, name, value) for name, value in defaults.items()]

    def _collect(self, airtable=None, **overrides):
        patches = self._patched(**overrides)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return report.collect(airtable=airtable, use_cache=False)

    def test_airtable_failure_degrades_instead_of_raising(self):
        class Broken:
            def list_queue_rows(self):
                raise RuntimeError("401 auth rejected")

        data = self._collect(airtable=Broken())
        self.assertIn("401", data["airtable_error"])
        # The local half is still there, which is the whole point.
        self.assertEqual(data["now"]["phones"], 1)
        self.assertEqual(data["queue"]["total"], 0)

    def test_no_airtable_client_at_all_is_fine(self):
        data = self._collect(airtable=None)
        self.assertEqual(data["airtable_error"], "")
        self.assertEqual(data["queue"]["by_status"], {})

    def test_totals_never_divide_by_zero(self):
        data = self._collect()
        self.assertEqual(data["totals"]["seconds_per_post"], 0.0)
        self.assertEqual(data["totals"]["mlx_rate"], 0.0)

    def test_the_cache_is_used_and_can_be_invalidated(self):
        calls = []

        def counting_now():
            calls.append(1)
            return {"loops": [], "active_loops": [], "profiles": [], "slots_held": 0,
                    "slot_ceiling": 12, "phones": 0, "agent_up": True}

        patches = self._patched(running_now=counting_now)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        report.collect(use_cache=True)
        report.collect(use_cache=True)
        self.assertEqual(len(calls), 1, "second call should have been served from cache")
        report.invalidate_cache()
        report.collect(use_cache=True)
        self.assertEqual(len(calls), 2)


class RenderTest(unittest.TestCase):
    def _data(self, **overrides):
        data = {
            "generated_at": "2026-08-05 18:30:00", "day": "2026-08-05",
            "now": {"loops": [("posting", "active")], "active_loops": ["posting"],
                    "profiles": ["Jil 1"], "slots_held": 1, "slot_ceiling": 12,
                    "phones": 1, "agent_up": True},
            "runs": [],
            "totals": {"runs": 0, "posts": 0, "seconds": 0.0, "seconds_per_post": 0.0,
                       "attempts": 0, "mlx_500": 0, "mlx_rate": 0.0, "retries_burned": 0,
                       "other_failures": 0},
            "ledger": {"total": 0, "by_status": {}, "verify_seconds": 0.0},
            "health": {"loops": [], "bad": []}, "alerts": [],
            "queue": {"total": 0, "by_status": {}, "by_slot": {}, "failures": []},
            "content": {"ready": 0, "by_model": {}},
            "airtable_error": "",
        }
        data.update(overrides)
        return data

    def test_renders_a_complete_page(self):
        page = report_html.render(self._data())
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("</html>", page)
        self.assertIn("Right now", page)

    def test_live_pages_refresh_and_snapshots_do_not(self):
        self.assertIn("http-equiv=\"refresh\"", report_html.render(self._data(), live=True))
        self.assertNotIn("http-equiv=\"refresh\"", report_html.render(self._data(), live=False))

    def test_a_down_agent_is_called_out(self):
        data = self._data()
        data["now"]["agent_up"] = False
        page = report_html.render(data)
        self.assertIn("DOWN", page)

    def test_phones_without_slots_are_explained(self):
        data = self._data()
        data["now"].update(phones=8, slots_held=1)
        self.assertIn("7 phone(s) open with no slot held", report_html.render(data))

    def test_profile_names_are_escaped(self):
        data = self._data()
        data["queue"]["failures"] = [{"name": "<script>x</script>", "issue": "Other",
                                      "retries": 1, "notes": "a & b", "slot": "16:00"}]
        page = report_html.render(data)
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_an_airtable_error_is_shown_on_the_page(self):
        page = report_html.render(self._data(airtable_error="RuntimeError: 401"))
        self.assertIn("Airtable unreachable", page)
        self.assertIn("401", page)

    def test_empty_state_says_so_rather_than_rendering_a_blank_table(self):
        page = report_html.render(self._data())
        self.assertIn("No posting run has started today", page)
        self.assertIn("Nothing failed today", page)

    def test_both_themes_are_styled(self):
        page = report_html.render(self._data())
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn('[data-theme="light"]', page)


if __name__ == "__main__":
    unittest.main()


class FragmentRenderTest(RenderTest):
    """`standalone=False` is what a host that supplies its own <html> gets."""

    def test_fragment_has_no_document_skeleton(self):
        page = report_html.render(self._data(), standalone=False)
        for tag in ("<!doctype", "<html", "<head", "<body"):
            self.assertNotIn(tag, page.lower(), f"{tag} leaked into the fragment")

    def test_fragment_still_carries_its_styles_and_content(self):
        page = report_html.render(self._data(), standalone=False)
        self.assertIn("<style>", page)
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn("Right now", page)

    def test_fragment_and_page_share_the_same_body(self):
        data = self._data()
        fragment = report_html.render(data, standalone=False)
        full = report_html.render(data, standalone=True)
        body = fragment.split("</style>", 1)[1].strip()
        self.assertIn(body, full)
