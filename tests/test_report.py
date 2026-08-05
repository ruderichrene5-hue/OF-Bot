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


# One run with every shape of per-profile ending the log can produce: a clean
# post, a share that could not be proven, a launch MultiLogin 500ed, a phone
# that never came up, and a post that was still going when the log ended.
PROFILE_LOG = """\
2026-08-05 18:11:14,000 | INFO | Posting 5 profile(s), up to 10 at a time (rolling, global ceiling 12)
2026-08-05 18:11:20,000 | INFO | Launched profile 1001
2026-08-05 18:11:21,000 | INFO | Launched profile 1002
2026-08-05 18:11:22,000 | ERROR | Failed to launch profile 1003 -- MultiLogin-side 500 (their cloud; self-heals, but it still spends this row's retry budget): {'status': 'error'}
2026-08-05 18:11:30,000 | INFO | Preparing to push reel media for profile 1002: /opt/adbbot/spoofed/Nikki/run4/nikki_3_I_5_aug__Nikki_15.mp4 -> /sdcard/Download/x.mp4
2026-08-05 18:12:00,000 | INFO | Post result for Laila 2 (profile 1001): done
2026-08-05 18:13:00,000 | WARNING | Workflow outcome UNCERTAIN for profile 1002 -- Share was tapped but the post could not be confirmed; check before re-posting
2026-08-05 18:14:00,000 | WARNING | Profile 1004 is not ready for ADB automation
2026-08-05 18:15:00,000 | INFO | Launched profile 1005
2026-08-05 18:16:00,000 | INFO | Posting run complete (4 post(s)); MLX launch health: 6 attempt(s), 4 ok, 1 MLX-side 500 (16.7%), 0 our-side/other failure(s), 1 queue retry(s) burned by MLX 500s
"""

RETRY_LOG_TEXT = """\
2026-08-05 18:20:00,000 | INFO | Re-queueing Nikki 3 / 18:00 in 30 min (attempt 2/3)
2026-08-05 18:20:01,000 | INFO | Not retrying Laila 4 / 21:00: issue type is Retries Exhausted -- not a retryable failure
2026-08-05 18:20:02,000 | INFO | Profile needs a human for Luisa 7 / 18:00: Human Verification Required
2026-08-04 09:00:00,000 | INFO | Re-queueing Nikki 3 / 09:00 in 15 min (attempt 1/3)
"""


class ProfileOutcomeTest(unittest.TestCase):
    """Which profiles posted, which did not -- the run's own log is the source."""

    def _log(self, text=PROFILE_LOG):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def _run(self):
        return report.parse_posting_runs(self._log(), day="2026-08-05")[0]

    def _outcomes(self):
        return {p.launch_id: p.outcome for p in self._run().profiles}

    def test_every_profile_the_run_touched_is_accounted_for(self):
        self.assertEqual(set(self._outcomes()), {"1001", "1002", "1003", "1004", "1005"})

    def test_the_named_result_line_gives_both_outcome_and_name(self):
        posted = [p for p in self._run().profiles if p.launch_id == "1001"][0]
        self.assertEqual((posted.name, posted.outcome), ("Laila 2", "posted"))

    def test_a_share_that_could_not_be_proven_is_verifying_not_failed(self):
        self.assertEqual(self._outcomes()["1002"], "verifying")

    def test_a_name_is_recovered_from_the_variant_filename(self):
        unproven = [p for p in self._run().profiles if p.launch_id == "1002"][0]
        self.assertEqual(unproven.name, "Nikki 15")

    def test_a_launch_that_500ed_did_not_post(self):
        entry = [p for p in self._run().profiles if p.launch_id == "1003"][0]
        self.assertEqual(entry.outcome, "failed")
        self.assertIn("MultiLogin", entry.detail)

    def test_a_phone_that_never_came_up_is_a_failure_with_a_reason(self):
        entry = [p for p in self._run().profiles if p.launch_id == "1004"][0]
        self.assertEqual(entry.outcome, "failed")
        self.assertIn("ADB-ready", entry.detail)

    def test_a_post_still_running_is_unknown_not_invented(self):
        self.assertEqual(self._outcomes()["1005"], "unknown")

    def test_a_relaunch_after_a_500_ends_the_run_posted(self):
        """MultiLogin flakiness that self-heals must not read as a lost post."""
        text = PROFILE_LOG.replace(
            "2026-08-05 18:12:00,000 | INFO | Post result for Laila 2 (profile 1001): done",
            "2026-08-05 18:12:00,000 | INFO | Post result for Laila 2 (profile 1001): done\n"
            "2026-08-05 18:12:30,000 | INFO | Post result for Katja 4 (profile 1003): done")
        runs = report.parse_posting_runs(self._log(text), day="2026-08-05")
        entry = [p for p in runs[0].profiles if p.launch_id == "1003"][0]
        self.assertEqual((entry.name, entry.outcome), ("Katja 4", "posted"))

    def test_counts_and_ordering_put_the_problems_first(self):
        run = self._run()
        self.assertEqual(run.counts(),
                         {"failed": 2, "unknown": 1, "skipped": 0,
                          "verifying": 1, "posted": 1})
        self.assertEqual([p.outcome for p in run.sorted_profiles()][:2], ["failed", "failed"])

    def test_a_profile_with_no_name_anywhere_is_labelled_by_its_id(self):
        entry = [p for p in self._run().profiles if p.launch_id == "1005"][0]
        self.assertEqual(entry.label, "1005")


class NameFromMediaPathTest(unittest.TestCase):
    def test_reads_the_handle_the_pipeline_stamped_on_the_variant(self):
        self.assertEqual(
            report.name_from_media_path("/opt/adbbot/spoofed/Nikki/run4/clip__Nikki_6_again.mp4"),
            "Nikki 6 again")

    def test_a_source_name_that_already_ends_in_mp4_still_parses(self):
        self.assertEqual(
            report.name_from_media_path("/x/jasmin_3_I_agency.mp4__Jasmin_1.mp4"), "Jasmin 1")

    def test_a_path_without_the_stamp_returns_nothing_rather_than_a_guess(self):
        self.assertEqual(report.name_from_media_path("/opt/adbbot/raw/Nikki/clip.mp4"), "")


class RetryAnnotationTest(unittest.TestCase):
    """A failure that is already re-queued needs nobody; the page must say so."""

    def _retry_log(self, text=RETRY_LOG_TEXT):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def _events(self):
        return report.retry_events(self._retry_log(), day="2026-08-05")

    def test_only_the_reported_day_is_read(self):
        self.assertEqual([e["profile"] for e in self._events()],
                         ["nikki 3", "laila 4", "luisa 7"])

    def test_a_requeued_failure_reads_as_handled(self):
        run = report.RunSummary(started="2026-08-05 18:11:14", finished="2026-08-05 18:16:00")
        run.profiles.append(report.ProfileRun(launch_id="1", name="Nikki 3", outcome="failed"))
        report.annotate_runs([run], events=self._events())
        self.assertEqual(run.profiles[0].next_tone, "ok")
        self.assertIn("attempt 2/3", run.profiles[0].next_step)

    def test_a_failure_the_retry_pass_parked_asks_for_a_person(self):
        run = report.RunSummary(started="2026-08-05 18:11:14", finished="2026-08-05 18:16:00")
        run.profiles.append(report.ProfileRun(launch_id="2", name="Luisa 7", outcome="failed"))
        report.annotate_runs([run], events=self._events())
        self.assertEqual(run.profiles[0].next_tone, "bad")
        self.assertIn("needs a person", run.profiles[0].next_step)

    def test_a_verdict_from_before_the_run_is_not_borrowed(self):
        """Yesterday's re-queue says nothing about today's failure."""
        run = report.RunSummary(started="2026-08-05 19:00:00", finished="2026-08-05 19:05:00")
        run.profiles.append(report.ProfileRun(launch_id="3", name="Nikki 3", outcome="failed"))
        report.annotate_runs([run], events=self._events())
        self.assertEqual(run.profiles[0].next_step, "waiting for the next retry pass")

    def test_a_posted_profile_is_left_alone(self):
        run = report.RunSummary(started="2026-08-05 18:11:14", finished="2026-08-05 18:16:00")
        run.profiles.append(report.ProfileRun(launch_id="4", name="Nikki 3", outcome="posted"))
        report.annotate_runs([run], events=self._events())
        self.assertEqual(run.profiles[0].next_step, "")

    def test_names_fill_in_from_the_offline_map(self):
        run = report.RunSummary(started="2026-08-05 18:11:14")
        run.profiles.append(report.ProfileRun(launch_id="1001", outcome="posted"))
        report.annotate_runs([run], names={"1001": "Katja 2"})
        self.assertEqual(run.profiles[0].name, "Katja 2")


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

    def test_variants_held_by_a_queue_row_are_not_postable(self):
        """The Viktoria case (2026-08-05): 11 Ready, 0 usable.

        A variant only becomes `Used` on a successful post, so a Failed row
        leaves it Ready *and* linked -- and `queue_runner` will never draw a
        linked variant again. Counting those as stock says "there is content"
        when the next run can post nothing.
        """
        class FakeAirtable:
            def list_ready_variants(self):
                return [{"id": "v1", "file_path": "/opt/adbbot/spoofed/Viktoria/run1/a.mp4"},
                        {"id": "v2", "file_path": "/opt/adbbot/spoofed/Viktoria/run1/b.mp4"},
                        {"id": "v3", "file_path": "/opt/adbbot/spoofed/Nikki/run4/c.mp4"}]

        stock = report.content_stock(FakeAirtable(), claimed={"v1", "v2"})
        self.assertEqual(stock["ready"], 3)
        self.assertEqual(stock["drawable"], 1)
        self.assertEqual(stock["held"], 2)
        self.assertEqual(stock["by_model"], {"Nikki": 1, "Viktoria": 0})
        self.assertEqual(stock["held_by_model"], {"Nikki": 0, "Viktoria": 2})

    def test_claimed_ids_come_from_rows_of_any_status(self):
        from adb_bot.clients import airtable as at
        rows = [{"fields": {at.F_PQ_SPOOF_VARIANT: ["v1"], "Post Status": "Posted"}},
                {"fields": {at.F_PQ_SPOOF_VARIANT: ["v2"], "Post Status": "Failed"}},
                {"fields": {"Post Status": "Pending"}}]
        self.assertEqual(report.claimed_variant_ids(rows), {"v1", "v2"})

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

        self.assertEqual(report.content_stock(Broken()),
                         {"ready": 0, "drawable": 0, "held": 0,
                          "by_model": {}, "held_by_model": {}})


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
        # Still collected (cheap, and other consumers may want it); the page no
        # longer renders it -- the retry pass rewrites timestamps off their slot,
        # so the grouping was mostly noise like 16:22 / 17:07.
        self.assertEqual(self._queue()["by_slot"], {"16:00": {"Posted": 1, "Failed": 1}})


class CollectTest(unittest.TestCase):
    def setUp(self):
        report.invalidate_cache()
        self.addCleanup(report.invalidate_cache)

    def _patched(self, **overrides):
        defaults = {
            "running_now": lambda: {"loops": [("posting", "active")], "active_loops": ["posting"],
                                    "profiles": [], "stale_locks": [], "slots_held": 1,
                                    "slot_ceiling": 12, "phones": 1, "agent_up": True},
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
            return {"loops": [], "active_loops": [], "profiles": [], "stale_locks": [],
                    "slots_held": 0, "slot_ceiling": 12, "phones": 0, "agent_up": True}

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
                    "profiles": [{"profile_id": "111", "name": "Jil 1", "owner": "posting",
                                  "pid": "42", "age_seconds": 30.0}],
                    "stale_locks": [], "slots_held": 1, "slot_ceiling": 12,
                    "phones": 1, "agent_up": True},
            "runs": [],
            "totals": {"runs": 0, "posts": 0, "seconds": 0.0, "seconds_per_post": 0.0,
                       "attempts": 0, "mlx_500": 0, "mlx_rate": 0.0, "retries_burned": 0,
                       "other_failures": 0},
            "ledger": {"total": 0, "by_status": {}, "verify_seconds": 0.0},
            "health": {"loops": [], "bad": []}, "alerts": [],
            "queue": {"total": 0, "by_status": {}, "by_slot": {}, "failures": []},
            "content": {"ready": 0, "by_model": {}},
            "needs_human": {"rows": [], "retrying": [], "profiles": [], "error": ""},
            "disks": [], "uptime": 0.0, "top_processes": [], "phones": [], "timers": [],
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
        data["now"].update(phones=8, slots_held=1, slot_ceiling=12)
        page = report_html.render(data)
        self.assertIn("7 phone(s) open that no slot accounts for", page)
        self.assertNotIn("cap is not holding", page)

    def test_a_healthy_run_at_the_ceiling_is_not_alarming(self):
        """12 phones against a ceiling of 12 is the cap working, not a fault.

        Regression for 2026-08-05: the page read `held_slots()` (per-process,
        always 0 from the report server) and so painted every full run as a
        fleet-wide leak.
        """
        data = self._data()
        data["now"].update(phones=12, slots_held=12, slot_ceiling=12)
        page = report_html.render(data)
        self.assertNotIn("cap is not holding", page)
        self.assertNotIn("no slot accounts for", page)
        self.assertIn("12 of 12 tracked by the cap", page)

    def test_slots_over_the_ceiling_is_called_out_as_a_cap_failure(self):
        data = self._data()
        data["now"].update(phones=14, slots_held=14, slot_ceiling=12)
        self.assertIn("cap is not holding", report_html.render(data))

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


class RunningNowTest(unittest.TestCase):
    """`slots_held` must be the GLOBAL count, not this process's holdings."""

    def test_it_reads_the_global_slot_count_not_the_callers(self):
        from adb_bot.core import locks

        with mock.patch.object(locks, "live_profile_count", return_value=12) as global_count, \
             mock.patch.object(locks, "held_slots", return_value=[]) as mine, \
             mock.patch.object(locks, "max_live_profiles", return_value=12), \
             mock.patch.object(report, "systemd_state", return_value=[]), \
             mock.patch.object(report, "live_phones", return_value=12), \
             mock.patch.object(report, "mlx_agent_up", return_value=True):
            now = report.running_now()

        self.assertEqual(now["slots_held"], 12)
        global_count.assert_called()
        mine.assert_not_called()


class ContentSectionRenderTest(RenderTest):
    def test_held_only_stock_is_called_out_as_unpostable(self):
        data = self._data()
        data["content"] = {"ready": 11, "drawable": 0, "held": 11,
                           "by_model": {"Viktoria": 0}, "held_by_model": {"Viktoria": 11}}
        page = report_html.render(data)
        self.assertIn("no postable content", page)
        self.assertIn("already attached to a queue row", page)

    def test_mixed_stock_leads_with_what_can_be_drawn(self):
        data = self._data()
        data["content"] = {"ready": 30, "drawable": 19, "held": 11,
                           "by_model": {"Nikki": 19, "Viktoria": 0},
                           "held_by_model": {"Nikki": 0, "Viktoria": 11}}
        page = report_html.render(data)
        self.assertIn("<strong>19</strong> spoofed video(s) are free", page)
        self.assertNotIn("no postable content", page)

    def test_no_variants_at_all_still_says_so(self):
        page = report_html.render(self._data())
        self.assertIn("No spoofed videos in stock at all", page)


class NeedsHumanTest(unittest.TestCase):
    """The worklist must come from the retry pass's own source.

    Built from `list_queue_rows` it produced a table of blanks (2026-08-05):
    that listing returns neither Issue Type nor Retry Count, so every row read
    as "(none set)" with 0 retries.
    """

    FAILED = [
        {"id": "q1", "fields": {"Name": "Jil 6 / 20:00", "Issue Type": "Failed - Needs Retry",
                                "Retry Count": 1, "Scheduled DateTime": "2026-08-05T18:00:00.000Z"}},
        {"id": "q2", "fields": {"Name": "Jasmin 9 / 09:00", "Issue Type": "Banned / Blocked",
                                "Retry Count": 0, "Scheduled DateTime": "2026-08-05T07:00:00.000Z"}},
        {"id": "q3", "fields": {"Name": "Laila 9 / 19:00", "Issue Type": "Retries Exhausted",
                                "Retry Count": 3, "Notes": "device unreachable",
                                "Scheduled DateTime": "2026-08-05T17:00:00.000Z"}},
        {"id": "q4", "fields": {"Name": "Jil 2 / 09:00",
                                "Issue Type": "Human Verification Required", "Retry Count": 0,
                                "Scheduled DateTime": "2026-08-05T07:00:00.000Z"}},
        # Retryable by issue type, but out of attempts -- a person's job now.
        {"id": "q5", "fields": {"Name": "Nikki 3 / 20:00", "Issue Type": "Failed - Needs Retry",
                                "Retry Count": 3, "Scheduled DateTime": "2026-08-05T18:00:00.000Z"}},
    ]
    PROFILES = [
        {"id": "p1", "fields": {"Profile Name": "Laila 9", "Needs Human Check": True,
                                "Issue Reason": "Human Verification Required",
                                "Issue Notes": "newest line\nolder line", "Status": "Active",
                                "Flagged At": "2026-08-05T17:30:00.000Z"}},
    ]

    def _airtable(self, failed=None, profiles=None, boom=None):
        outer = self

        class Fake:
            def list_failed_posts(self):
                if boom == "failed":
                    raise RuntimeError("429 rate limited")
                return outer.FAILED if failed is None else failed

            def _list_table(self, table, **kwargs):
                if boom == "profiles":
                    raise RuntimeError("403 forbidden")
                self.filter = kwargs.get("filter_formula")
                return outer.PROFILES if profiles is None else profiles

        return Fake()

    def test_splits_automatic_retries_from_human_work(self):
        out = report.needs_human(self._airtable())
        self.assertEqual([r["name"] for r in out["retrying"]], ["Jil 6 / 20:00"])
        self.assertEqual(len(out["rows"]), 4)

    def test_an_exhausted_retryable_row_is_human_work(self):
        out = report.needs_human(self._airtable())
        names = [r["name"] for r in out["rows"]]
        self.assertIn("Nikki 3 / 20:00", names)

    def test_worst_issues_are_listed_first(self):
        out = report.needs_human(self._airtable())
        self.assertEqual(out["rows"][0]["issue"], "Banned / Blocked")
        self.assertEqual(out["rows"][1]["issue"], "Human Verification Required")

    def test_it_carries_the_detail_a_person_needs(self):
        row = [r for r in report.needs_human(self._airtable())["rows"]
               if r["name"] == "Laila 9 / 19:00"][0]
        self.assertEqual(row["retries"], 3)
        self.assertEqual(row["notes"], "device unreachable")
        self.assertEqual(row["slot"], "2026-08-05 17:00")

    def test_flagged_profiles_are_filtered_on_the_checkbox(self):
        airtable = self._airtable()
        out = report.needs_human(airtable)
        self.assertEqual(len(out["profiles"]), 1)
        self.assertEqual(out["profiles"][0]["reason"], "Human Verification Required")
        # Only the newest note line: the field keeps full history, a dashboard
        # should not.
        self.assertEqual(out["profiles"][0]["note"], ["newest line"])

    def test_it_is_not_limited_to_today(self):
        """A row that failed last night still needs the same person."""
        old = [{"id": "q9", "fields": {"Name": "old / 21:00", "Issue Type": "Banned / Blocked",
                                       "Scheduled DateTime": "2026-08-01T19:00:00.000Z"}}]
        out = report.needs_human(self._airtable(failed=old))
        self.assertEqual(len(out["rows"]), 1)

    def test_an_airtable_failure_is_reported_not_raised(self):
        out = report.needs_human(self._airtable(boom="failed"))
        self.assertIn("429", out["error"])
        self.assertEqual(out["rows"], [])


class NeedsHumanRenderTest(RenderTest):
    def _triage(self, **kw):
        base = {"rows": [], "retrying": [], "profiles": [], "error": ""}
        base.update(kw)
        return base

    def test_clean_state_says_so(self):
        data = self._data(needs_human=self._triage(retrying=[{"name": "x"}]))
        page = report_html.render(data)
        self.assertIn("Nothing to do", page)
        self.assertIn("No account is waiting on you", page)

    def test_work_is_listed_with_its_reason(self):
        data = self._data(needs_human=self._triage(
            rows=[{"name": "Laila 9 / 19:00", "slot": "2026-08-05 17:00",
                   "issue": "Retries Exhausted", "retries": 3, "notes": "device unreachable"}],
            profiles=[{"name": "Laila 9", "reason": "Human Verification Required",
                       "status": "Active", "flagged_at": "2026-08-05 17:30",
                       "note": ["locked out"]}]))
        page = report_html.render(data)
        self.assertIn("Laila 9 / 19:00", page)          # the abandoned post
        self.assertIn("Retries Exhausted", page)
        self.assertIn("Human Verification Required", page)
        self.assertIn("locked out", page)               # the profile's latest note
        self.assertIn("2 item(s) need a person", page)  # one row + one profile
        # The profile view leads with plain-language guidance, not field names.
        self.assertIn("What to do", page)

    def test_the_banner_counts_rows_and_profiles_together(self):
        data = self._data(needs_human=self._triage(
            rows=[{"name": "a", "slot": "", "issue": "Banned / Blocked", "retries": 0, "notes": ""}],
            profiles=[{"name": "b", "reason": "Banned", "status": "Active",
                       "flagged_at": "", "note": []}]))
        self.assertIn("2 item(s) need a person", report_html.render(data))

    def test_names_are_escaped(self):
        data = self._data(needs_human=self._triage(
            rows=[{"name": "<img src=x>", "slot": "", "issue": "Other", "retries": 0, "notes": ""}]))
        page = report_html.render(data)
        self.assertNotIn("<img src=x>", page)
        self.assertIn("&lt;img", page)


class ServerStatsTest(unittest.TestCase):
    def test_it_reads_real_values_off_proc(self):
        stats = report.server_stats()
        self.assertGreater(stats["processes"], 1)
        self.assertGreater(stats["cores"], 0)
        self.assertGreater(stats["mem_total_mb"], 0)
        self.assertGreaterEqual(stats["mem_percent"], 0.0)
        self.assertLessEqual(stats["mem_percent"], 100.0)
        self.assertGreaterEqual(stats["cpu_percent"], 0.0)

    def test_unreadable_proc_degrades_to_zeros(self):
        with mock.patch("builtins.open", side_effect=OSError("nope")), \
             mock.patch("os.listdir", side_effect=OSError("nope")):
            stats = report.server_stats()
        self.assertEqual(stats["processes"], 0)
        self.assertEqual(stats["mem_total_mb"], 0)


class PhoneDurationsTest(unittest.TestCase):
    """Per-phone time, not wall-clock/posts.

    Posting runs up to 10 phones at once, so run wall-clock divided by post
    count reported ~50s when a phone was really held for ~227s (2026-08-05).
    """

    def _log(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    LOG = (
        "2026-08-05 18:00:00,000 | INFO | Launched profile 111\n"
        "2026-08-05 18:00:10,000 | INFO | Launched profile 222\n"
        "2026-08-05 18:03:00,000 | INFO | Closed profile 111 (workflow finished)\n"   # 180s
        "2026-08-05 18:05:10,000 | INFO | Closed profile 222 (workflow finished)\n"   # 300s
        "2026-08-05 18:06:00,000 | INFO | Launched profile 333\n"                     # never closed
    )

    def test_pairs_launch_with_close_per_profile(self):
        out = report.phone_durations(self._log(self.LOG), day="2026-08-05")
        self.assertEqual(out["samples"], 2)
        self.assertEqual(out["average"], 240.0)
        self.assertEqual(out["median"], 300.0)
        self.assertEqual(out["longest"], 300.0)

    def test_concurrent_phones_are_measured_independently(self):
        """The whole point: overlapping phones must not shrink each other."""
        out = report.phone_durations(self._log(self.LOG), day="2026-08-05")
        # Wall clock is 6 min for 2 posts = 180s if naively divided; the real
        # per-phone average is higher because they overlapped.
        self.assertGreater(out["average"], 180.0)

    def test_an_unclosed_launch_is_excluded_but_counted(self):
        out = report.phone_durations(self._log(self.LOG), day="2026-08-05")
        self.assertEqual(out["unclosed"], 1)
        self.assertEqual(out["launches"], 3)

    def test_an_absurd_pair_is_dropped_rather_than_skewing_the_average(self):
        text = ("2026-08-05 01:00:00,000 | INFO | Launched profile 999\n"
                "2026-08-05 23:00:00,000 | INFO | Closed profile 999 (workflow finished)\n")
        self.assertEqual(report.phone_durations(self._log(text), day="2026-08-05")["samples"], 0)

    def test_no_data_is_zeros_not_an_error(self):
        out = report.phone_durations("/nonexistent/none.log", day="2026-08-05")
        self.assertEqual((out["samples"], out["average"], out["median"]), (0, 0.0, 0.0))


class ServerSectionRenderTest(RenderTest):
    def _with_server(self, **kw):
        server = {"processes": 265, "cores": 8, "load1": 4.65, "cpu_percent": 43.7,
                  "mem_total_mb": 15603, "mem_used_mb": 2192, "mem_percent": 14.0,
                  "swap_total_mb": 8192, "swap_used_mb": 2024}
        server.update(kw)
        return self._data(server=server)

    def test_server_tiles_render(self):
        page = report_html.render(self._with_server())
        self.assertIn("Processes", page)
        self.assertIn("265", page)
        self.assertIn("load 4.65 over 8 core(s)", page)
        self.assertIn("2,192 of 15,603 MB used", page)

    def test_memory_pressure_is_coloured_red(self):
        page = report_html.render(self._with_server(mem_percent=97.0, swap_used_mb=8172))
        self.assertIn("var(--bad)", page)

    def test_the_slots_table_is_gone(self):
        self.assertNotIn("<h2>Slots</h2>", report_html.render(self._data()))

    def test_per_phone_average_replaces_wall_clock_per_post(self):
        data = self._data(phone={"samples": 61, "launches": 116, "unclosed": 21,
                                 "average": 227.0, "median": 224.0, "longest": 580.0})
        page = report_html.render(data)
        self.assertIn("Avg per phone", page)
        self.assertNotIn("Avg per post", page)
        self.assertIn("from 61 measured post(s)", page)
        self.assertIn("21 launch(es) have no close recorded", page)


class MonitoringSectionsTest(unittest.TestCase):
    def test_disk_usage_reports_each_filesystem_once(self):
        """`/` and `/opt/adbbot` are the same filesystem here; two rows would
        double-count the same free space and look like more headroom."""
        disks = report.disk_usage(["/", "/", "/root"])
        self.assertEqual(len(disks), 1)
        self.assertGreater(disks[0]["total_gb"], 0)
        self.assertLessEqual(disks[0]["percent"], 100.0)

    def test_a_missing_path_is_skipped_not_fatal(self):
        self.assertEqual(report.disk_usage(["/definitely/not/here"]), [])

    def test_top_processes_are_sorted_biggest_first(self):
        procs = report.top_processes(5)
        self.assertTrue(procs)
        self.assertLessEqual(len(procs), 5)
        self.assertEqual([p["rss_mb"] for p in procs],
                         sorted([p["rss_mb"] for p in procs], reverse=True))

    def test_uptime_is_positive(self):
        self.assertGreater(report.uptime_seconds(), 0)


class TimerStateTest(unittest.TestCase):
    LIST_TIMERS = (
        "Wed 2026-08-05 19:37:49 UTC 15min Wed 2026-08-05 19:07:49 UTC 14min ago "
        "adbbot-pipeline.timer adbbot-pipeline.service\n"
        "- - Wed 2026-08-05 18:43:50 UTC 38min ago "
        "adbbot-recheck.timer adbbot-recheck.service\n"
    )

    def _states(self, active_states):
        show = "".join(f"Id=adbbot-{loop}.timer\nActiveState={state}\n\n"
                       for loop, state in active_states.items())

        def fake_run(cmd, **kwargs):
            out = self.LIST_TIMERS if "list-timers" in cmd else show
            return mock.Mock(stdout=out)

        with mock.patch.object(report.subprocess, "run", side_effect=fake_run):
            return {t["loop"]: t for t in report.timer_states(["pipeline", "recheck"])}

    def test_an_empty_next_while_the_service_runs_is_not_stopped(self):
        """The 2026-08-05 trap: systemd prints '-' for NEXT while the timer's
        service is executing, so reading that column reports every actively
        working loop as stopped."""
        states = self._states({"pipeline": "active", "recheck": "active"})
        self.assertFalse(states["recheck"]["stopped"])
        self.assertEqual(states["recheck"]["next"], "")
        self.assertEqual(states["recheck"]["last"], "2026-08-05 18:43:50")

    def test_a_genuinely_inactive_timer_is_flagged(self):
        states = self._states({"pipeline": "active", "recheck": "inactive"})
        self.assertTrue(states["recheck"]["stopped"])
        self.assertFalse(states["pipeline"]["stopped"])

    def test_next_run_is_read_for_a_scheduled_timer(self):
        states = self._states({"pipeline": "active", "recheck": "active"})
        self.assertEqual(states["pipeline"]["next"], "2026-08-05 19:37:49")


class MonitoringRenderTest(RenderTest):
    def test_stopped_loops_are_banner_worthy(self):
        data = self._data(timers=[
            {"loop": "posting", "state": "inactive", "interval_min": 5,
             "last": "2026-08-05 18:00:00", "next": "", "stopped": True}])
        page = report_html.render(data)
        self.assertIn("1 loop(s) not scheduled", page)
        self.assertIn("cannot alert", page)

    def test_healthy_timers_explain_an_empty_next(self):
        data = self._data(timers=[
            {"loop": "posting", "state": "active", "interval_min": 5,
             "last": "2026-08-05 19:00:00", "next": "", "stopped": False}])
        page = report_html.render(data)
        self.assertIn("running now", page)
        self.assertNotIn("not scheduled", page)

    def test_orphan_phones_are_marked(self):
        data = self._data(phones=[
            {"pid": 1, "name": "Jil 1", "profile_id": "111", "age_seconds": 4000,
             "rss_mb": 15.0, "orphan": True},
            {"pid": 2, "name": "Jil 2", "profile_id": "222", "age_seconds": 60,
             "rss_mb": 170.0, "orphan": False}])
        page = report_html.render(data)
        self.assertIn(">orphan<", page)
        self.assertIn(">in use<", page)
        self.assertIn("The reaper closes those every 20 minutes", page)

    def test_top_processes_render(self):
        data = self._data(top_processes=[{"pid": 99, "name": "claude.exe", "rss_mb": 13866.0}])
        page = report_html.render(data)
        self.assertIn("claude.exe", page)
        self.assertIn("13,866 MB", page)

    def test_disk_pressure_is_coloured(self):
        data = self._data(disks=[{"path": "/", "total_gb": 150.0, "used_gb": 140.0,
                                  "free_gb": 10.0, "percent": 93.0}], uptime=3600.0)
        self.assertIn("var(--bad)", report_html.render(data))


class RunByRunTest(RenderTest):
    """The daily read: run 1, who posted, who did not, who is coming back."""

    def _runs(self):
        finished = report.RunSummary(started="2026-08-05 18:11:14",
                                     finished="2026-08-05 18:16:00", planned=3, posts=3)
        finished.profiles = [
            report.ProfileRun(launch_id="1", name="Katja 2", outcome="posted"),
            report.ProfileRun(launch_id="2", name="Nikki 3", outcome="failed",
                              detail="ADB connect failed",
                              next_step="retrying by itself — attempt 2/3, next try in 30 min",
                              next_tone="ok"),
            report.ProfileRun(launch_id="3", name="Luisa 7", outcome="verifying",
                              detail="share tapped, not yet confirmed",
                              next_step="the recheck pass will confirm or fail it",
                              next_tone="warn"),
        ]
        running = report.RunSummary(started="2026-08-05 18:20:00", planned=1)
        running.profiles = [report.ProfileRun(launch_id="4", name="Jil 5")]
        return [finished, running]

    def test_each_run_is_numbered_and_carries_its_profiles(self):
        page = report_html.render(self._data(runs=self._runs()))
        panel = page.split('id="panel-technical"')[1].split("</section>")[0]
        self.assertIn("Run by run", panel)
        self.assertIn("Run 1", panel)
        self.assertIn("Run 2", panel)
        for name in ("Katja 2", "Nikki 3", "Luisa 7", "Jil 5"):
            self.assertIn(name, panel)

    def test_a_run_that_lost_a_profile_is_open_by_default(self):
        page = report_html.render(self._data(runs=self._runs()))
        self.assertIn('<details class="run" open>', page)

    def test_a_failure_the_bot_is_handling_reads_green_not_red(self):
        page = report_html.render(self._data(runs=self._runs()))
        self.assertIn('<span class="pill ok">retrying by itself — attempt 2/3', page)

    def test_a_run_still_going_does_not_report_its_posts_as_lost(self):
        page = report_html.render(self._data(runs=self._runs()))
        self.assertIn("still going", page)
        self.assertIn("1 did not post", page)      # the running profile is not counted

    def test_a_profile_name_cannot_inject_markup(self):
        runs = self._runs()
        runs[0].profiles[0].name = "<script>x</script>"
        page = report_html.render(self._data(runs=runs))
        self.assertNotIn("<script>x", page)
        self.assertIn("&lt;script&gt;x", page)

    def test_no_run_says_so_rather_than_rendering_an_empty_card(self):
        page = report_html.render(self._data(runs=[]))
        self.assertIn("No posting run has started today.", page)


class TabsTest(RenderTest):
    """Three views on one page, switched without JavaScript.

    The page must work from a file:// URL and inside a strict-CSP host, so the
    tabs are radios and CSS. These pin the wiring: a typo in an id silently
    leaves a panel permanently hidden, which no other test would catch.
    """

    def test_all_three_panels_exist(self):
        page = report_html.render(self._data())
        for panel in ("panel-server", "panel-profiles", "panel-technical"):
            self.assertIn(f'id="{panel}"', page)
        for tab in ("tab-server", "tab-profiles", "tab-technical"):
            self.assertIn(f'id="{tab}"', page)
            self.assertIn(f'for="{tab}"', page)

    def test_every_panel_has_a_rule_that_shows_it(self):
        page = report_html.render(self._data())
        for tab, panel in (("tab-server", "panel-server"),
                           ("tab-profiles", "panel-profiles"),
                           ("tab-technical", "panel-technical")):
            self.assertIn(f"#{tab}:checked ~ #{panel}", page)

    def test_no_javascript_is_used(self):
        page = report_html.render(self._data())
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("onclick", page.lower())

    def test_content_is_filed_under_the_right_tab(self):
        data = self._data(
            timers=[{"loop": "posting", "state": "active", "interval_min": 5,
                     "last": "x", "next": "y", "stopped": False}],
            server={"processes": 5, "cores": 2, "load1": 0.1, "cpu_percent": 1.0,
                    "mem_total_mb": 100, "mem_used_mb": 10, "mem_percent": 10.0,
                    "swap_total_mb": 0, "swap_used_mb": 0})
        page = report_html.render(data)
        server_panel = page.split('id="panel-server"')[1].split("</section>")[0]
        profiles_panel = page.split('id="panel-profiles"')[1].split("</section>")[0]
        technical_panel = page.split('id="panel-technical"')[1].split("</section>")[0]

        self.assertIn("Scheduled loops", server_panel)
        self.assertIn("Top memory use", server_panel)
        self.assertIn("What the words mean", profiles_panel)
        self.assertIn("Content stock", technical_panel)
        self.assertIn("Loop health", technical_panel)
        # The operator view must not be cluttered with engineering detail.
        self.assertNotIn("MLX 500", profiles_panel)
        self.assertNotIn("Top memory use", profiles_panel)

    def test_the_profiles_tab_carries_a_count_when_work_is_waiting(self):
        data = self._data(needs_human={
            "rows": [], "retrying": [], "error": "",
            "profiles": [{"name": "Jil 1", "reason": "Banned / Blocked", "status": "Active",
                          "flagged_at": "", "note": []}]})
        page = report_html.render(data)
        self.assertIn('<span class="count">1</span>', page)
        self.assertIn("open the <strong>Profiles</strong> tab", page)


class ProfilesViewTest(RenderTest):
    def _with(self, reason, **kw):
        profile = {"name": "Jil 1", "reason": reason, "status": "Active",
                   "flagged_at": "2026-08-05 17:30", "note": ["locked out"]}
        profile.update(kw)
        return self._data(needs_human={"rows": [], "retrying": [], "error": "",
                                       "profiles": [profile]})

    def test_each_issue_explains_itself_and_the_next_step(self):
        for reason in ("Human Verification Required", "Banned / Blocked",
                       "Retries Exhausted", "Device Unreachable"):
            page = report_html.render(self._with(reason))
            self.assertIn("What happened", page)
            self.assertIn("What to do", page)
            self.assertIn("MultiLogin", page, f"{reason} gives no concrete action")

    def test_an_unknown_reason_still_gets_guidance(self):
        page = report_html.render(self._with("Something New"))
        self.assertIn("What to do", page)
        self.assertIn("Something New", page)

    def test_it_says_how_to_signal_the_work_is_done(self):
        page = report_html.render(self._with("Banned / Blocked"))
        self.assertIn("Needs Human Check", page)
        self.assertIn("Nothing unticks it for you", page)

    def test_the_worst_issue_is_listed_first(self):
        data = self._data(needs_human={"rows": [], "retrying": [], "error": "", "profiles": [
            {"name": "a", "reason": "Retries Exhausted", "status": "Active",
             "flagged_at": "", "note": []},
            {"name": "b", "reason": "Banned / Blocked", "status": "Active",
             "flagged_at": "", "note": []}]})
        page = report_html.render(data)
        self.assertLess(page.index("Banned / Blocked —"), page.index("Retries Exhausted —"))

    def test_jargon_is_translated_rather_than_shown_raw(self):
        page = report_html.render(self._with("Human Verification Required"))
        self.assertIn("Being checked (Verifying)", page)
        self.assertIn("resolves by itself", page)


class ProfileLocksTest(unittest.TestCase):
    """Live locks and abandoned ones are different facts.

    A lock whose owner died sits on disk until the 45-minute TTL lets the next
    loop steal it. Counting those as "held by a running loop" reported the exact
    condition that cost 17 minutes of dead time on 2026-08-04 as healthy work.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _lock(self, profile_id, owner="posting", age_seconds=0.0, pid=4242):
        import os, time
        path = self.dir / f"{profile_id}.lock"
        path.write_text(f"pid={pid} owner={owner} at=2026-08-05 19:42:02\n")
        when = time.time() - age_seconds
        os.utime(path, (when, when))
        return path

    def _collect(self, phones=()):
        from adb_bot.core import locks
        from adb_bot.automation import phone_reaper

        with mock.patch.object(locks, "lock_dir", return_value=self.dir), \
             mock.patch.object(phone_reaper, "list_phones", return_value=list(phones)):
            return report.profile_locks()

    def test_a_fresh_lock_is_live(self):
        self._lock("111", owner="posting", age_seconds=30)
        live, stale = self._collect()
        self.assertEqual(len(live), 1)
        self.assertEqual(stale, [])
        self.assertEqual(live[0]["owner"], "posting")
        self.assertEqual(live[0]["pid"], "4242")
        self.assertAlmostEqual(live[0]["age_seconds"], 30, delta=5)

    def test_a_lock_past_its_ttl_is_abandoned_not_live(self):
        from adb_bot.core import locks
        self._lock("222", age_seconds=locks.DEFAULT_TTL_SECONDS + 120)
        live, stale = self._collect()
        self.assertEqual(live, [])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["profile_id"], "222")

    def test_the_profile_name_is_resolved_from_its_phone(self):
        """An 18-digit id tells a person nothing; the phone carries the name."""
        from adb_bot.automation import phone_reaper
        self._lock("625727120991322399")
        live, _ = self._collect(phones=[phone_reaper.Phone(
            pid=1, profile_id="625727120991322399", name="Viktoria 6")])
        self.assertEqual(live[0]["name"], "Viktoria 6")

    def test_an_unknown_profile_still_lists_its_id(self):
        self._lock("999")
        live, _ = self._collect()
        self.assertEqual(live[0]["name"], "")
        self.assertEqual(live[0]["profile_id"], "999")

    def test_a_malformed_lock_file_does_not_break_the_page(self):
        (self.dir / "333.lock").write_text("garbage without key values\n")
        live, stale = self._collect()
        self.assertEqual(len(live) + len(stale), 1)
        self.assertEqual((live + stale)[0]["owner"], "unknown")

    def test_an_unreadable_lock_directory_is_empty_not_fatal(self):
        from adb_bot.core import locks
        with mock.patch.object(locks, "lock_dir", side_effect=OSError("boom")):
            self.assertEqual(report.profile_locks(), ([], []))


class LockRenderTest(RenderTest):
    def _now(self, **kw):
        now = {"loops": [], "active_loops": [], "profiles": [], "stale_locks": [],
               "slots_held": 0, "slot_ceiling": 12, "phones": 0, "agent_up": True}
        now.update(kw)
        return self._data(now=now)

    def test_live_locks_show_name_owner_and_age(self):
        page = report_html.render(self._now(profiles=[
            {"profile_id": "111", "name": "Viktoria 6", "owner": "posting",
             "pid": "705725", "age_seconds": 320}]))
        self.assertIn("Profiles being worked on", page)
        self.assertIn("Viktoria 6", page)
        self.assertIn("posting", page)
        self.assertIn("5m 20s", page)
        self.assertIn("705725", page)

    def test_abandoned_locks_are_separated_and_explained(self):
        page = report_html.render(self._now(stale_locks=[
            {"profile_id": "222", "name": "Jil 1", "owner": "warmup",
             "pid": "1", "age_seconds": 4000}]))
        self.assertIn("Abandoned locks", page)
        self.assertIn("outlived their 45-minute TTL", page)
        self.assertIn("no longer block anything", page)

    def test_the_locked_tile_counts_only_live_locks(self):
        page = report_html.render(self._now(
            profiles=[{"profile_id": "1", "name": "a", "owner": "posting",
                       "pid": "1", "age_seconds": 10}],
            stale_locks=[{"profile_id": "2", "name": "b", "owner": "warmup",
                          "pid": "2", "age_seconds": 5000}]))
        self.assertIn("being worked on right now", page)
        self.assertNotIn("held by a running loop", page)
        self.assertIn("owner died; see below", page)
