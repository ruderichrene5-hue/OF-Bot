"""The loop production watchdog: alerting when a loop stops producing.

Every test here is really asking the one question that matters: can this tell
the difference between a quiet night and a broken one? Three real outages --
a dead MultiLogin agent, profile locks left behind by `systemctl stop`, and a
recheck pass that returned `unknown` forever -- all looked identical in the logs
to "nothing was scheduled". If IDLE and STALLED are not cleanly separated here,
the alert is either useless or it cries wolf every night at 3am.
"""

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from adb_bot.automation import loop_watchdog, queue_runner
from adb_bot.automation.loop_watchdog import (
    ALERT_CLEARED,
    ALERT_RECOVERED,
    ALERT_REMINDER,
    ALERT_STALLED,
    STATE_IDLE,
    STATE_OK,
    STATE_STALLED,
    STATE_WAITING,
    LoopWatchdog,
)
from adb_bot.automation.post_ledger import PostLedger
from adb_bot.clients import airtable as at

GRACE = 1800.0        # what "posting" gets: 30 min
T0 = 1_800_000_000.0  # a fixed, arbitrary epoch so the arithmetic is readable


class WatchdogTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.delivered = []
        self.history = self.root / "alerts.jsonl"

    def build(self, renotify=loop_watchdog.DEFAULT_RENOTIFY_SECONDS, sinks=None):
        return LoopWatchdog(
            state_dir=self.root / "watchdog",
            sinks=[self.delivered.append] if sinks is None else sinks,
            renotify_seconds=renotify,
        )

    def kinds(self):
        return [a.kind for a in self.delivered]


class GraceDerivationTest(TestCase):
    """The grace period must track the loop's own cadence, or the two drift and
    a loop that runs hourly gets alerted on after a single missed tick."""

    def test_at_least_three_ticks_and_never_under_half_an_hour(self):
        from adb_bot.automation import schedule_spec

        for loop, minutes in schedule_spec.RECOMMENDED_INTERVALS.items():
            grace = loop_watchdog.stall_after_seconds(loop)
            self.assertGreaterEqual(grace, loop_watchdog.MIN_STALL_AFTER_SECONDS, loop)
            self.assertGreaterEqual(grace, 3 * minutes * 60, loop)

    def test_posting_matches_the_thirty_minutes_the_incident_asked_for(self):
        self.assertEqual(loop_watchdog.stall_after_seconds("posting"), GRACE)

    def test_an_unknown_loop_still_gets_a_sane_grace(self):
        self.assertGreaterEqual(loop_watchdog.stall_after_seconds("brand-new-loop"),
                                loop_watchdog.MIN_STALL_AFTER_SECONDS)


class IdleIsNotAnAlertTest(WatchdogTestBase):
    """Nothing due means quiet is correct. This is the false-positive side of
    the whole feature: if a silent night pages somebody, the alert gets muted
    and then it never catches anything again."""

    def test_nothing_due_never_alerts_however_long_it_lasts(self):
        wd = self.build()
        for tick in range(24):           # a whole day of empty ticks
            verdict = wd.observe("posting", due=0, produced=0, now=T0 + tick * 3600)
            self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.delivered, [])

    def test_nothing_due_does_not_reset_the_clock_for_real_work_later(self):
        # An idle stretch must not be mistaken for production: the moment work
        # is due again, the loop is judged on what it produced, not on having
        # been asked nothing for six hours.
        wd = self.build()
        wd.observe("posting", due=0, produced=0, now=T0)
        verdict = wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        self.assertEqual(verdict.state, STATE_STALLED)


class StalledFiresTest(WatchdogTestBase):
    def test_work_due_and_nothing_produced_alerts_once_the_grace_passes(self):
        wd = self.build()
        wd.observe("posting", due=12, produced=0, now=T0)
        verdict = wd.observe("posting", due=12, produced=0, now=T0 + GRACE + 60)
        self.assertEqual(verdict.state, STATE_STALLED)
        self.assertEqual(self.kinds(), [ALERT_STALLED])
        alert = self.delivered[0]
        self.assertIn("STALLED", alert.message)
        self.assertIn("12 item(s) are due", alert.message)
        # The alert has to say what to look at; "it stopped" alone makes the
        # reader start the investigation from scratch every single time.
        self.assertIn("45001", alert.message)

    def test_one_bad_tick_inside_the_grace_is_not_an_outage(self):
        wd = self.build()
        wd.observe("posting", due=3, produced=0, now=T0)
        verdict = wd.observe("posting", due=3, produced=0, now=T0 + 600)
        self.assertEqual(verdict.state, STATE_WAITING)
        self.assertEqual(self.delivered, [])

    def test_a_first_ever_observation_cannot_stall_immediately(self):
        # A freshly deployed box (or a pruned ledger) has no history. Alerting
        # on the very first tick would mean an alert every restart.
        wd = self.build()
        verdict = wd.observe("posting", due=99, produced=0, now=T0)
        self.assertEqual(verdict.state, STATE_WAITING)
        self.assertEqual(self.delivered, [])

    def test_production_pushes_the_deadline_out(self):
        wd = self.build()
        wd.observe("posting", due=10, produced=2, now=T0)
        # 29 minutes later, still inside the grace measured from the last post.
        self.assertEqual(wd.observe("posting", due=8, produced=0, now=T0 + 1740).state,
                         STATE_WAITING)
        self.assertEqual(wd.observe("posting", due=8, produced=0, now=T0 + GRACE + 10).state,
                         STATE_STALLED)


class NoAlertStormTest(WatchdogTestBase):
    def test_a_persistent_stall_is_reported_once_per_renotify_interval(self):
        wd = self.build(renotify=3600)
        wd.observe("posting", due=7, produced=0, now=T0)
        wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 1)      # trips
        # The posting loop ticks every 5 minutes; an hour of that is 12 ticks.
        for tick in range(1, 13):
            wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 1 + tick * 300)
        self.assertEqual(self.kinds(), [ALERT_STALLED, ALERT_REMINDER])

    def test_state_still_reads_stalled_while_the_notification_is_suppressed(self):
        wd = self.build(renotify=3600)
        wd.observe("posting", due=7, produced=0, now=T0)
        wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 1)
        verdict = wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 300)
        self.assertEqual(verdict.state, STATE_STALLED)
        self.assertFalse(verdict.alerted)
        self.assertTrue(wd.state_for("posting").stalled)

    def test_a_night_long_outage_is_hours_of_lines_not_ticks(self):
        wd = self.build(renotify=3600)
        wd.observe("posting", due=40, produced=0, now=T0)
        for tick in range(1, 12 * 12 + 1):        # 12 hours at 5-minute ticks
            wd.observe("posting", due=40, produced=0, now=T0 + tick * 300)
        # ~12 hours stalled -> the first alert plus roughly one per hour.
        self.assertLessEqual(len(self.delivered), 13)
        self.assertGreaterEqual(len(self.delivered), 10)


class RecoveryTest(WatchdogTestBase):
    def test_production_clears_the_stall_and_says_so(self):
        wd = self.build()
        wd.observe("posting", due=7, produced=0, now=T0)
        wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 1)
        verdict = wd.observe("posting", due=7, produced=4, now=T0 + GRACE + 300)
        self.assertEqual(verdict.state, STATE_OK)
        self.assertEqual(self.kinds(), [ALERT_STALLED, ALERT_RECOVERED])
        self.assertFalse(wd.state_for("posting").stalled)

    def test_a_second_outage_after_a_recovery_alerts_again(self):
        # The re-notify budget must reset on recovery, or the second failure of
        # the night is silent -- which is the failure mode this replaces.
        wd = self.build(renotify=3600)
        wd.observe("posting", due=7, produced=0, now=T0)
        wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 1)
        wd.observe("posting", due=7, produced=4, now=T0 + GRACE + 60)
        wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 120)
        wd.observe("posting", due=7, produced=0, now=T0 + 2 * GRACE + 200)
        self.assertEqual(self.kinds(), [ALERT_STALLED, ALERT_RECOVERED, ALERT_STALLED])

    def test_work_ceasing_to_be_due_clears_the_stall_distinctly(self):
        # The rows failed out / the slot passed. The stall is over, but nothing
        # was produced, so it is reported as CLEARED and not as a recovery --
        # otherwise a loop that never worked would read as having healed.
        wd = self.build()
        wd.observe("posting", due=7, produced=0, now=T0)
        wd.observe("posting", due=7, produced=0, now=T0 + GRACE + 1)
        verdict = wd.observe("posting", due=0, produced=0, now=T0 + GRACE + 600)
        self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.kinds(), [ALERT_STALLED, ALERT_CLEARED])
        self.assertEqual(self.delivered[-1].severity, "info")


class SurvivesRestartsTest(WatchdogTestBase):
    """Each loop tick is a separate short-lived process (`run_loop` runs one loop
    once and exits), so anything held only in memory is worth nothing here."""

    def test_state_carries_across_instances(self):
        self.build().observe("posting", due=5, produced=0, now=T0)
        verdict = self.build().observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        self.assertEqual(verdict.state, STATE_STALLED)
        self.assertEqual(self.kinds(), [ALERT_STALLED])

    def test_loops_do_not_share_a_file(self):
        wd = self.build()
        wd.observe("posting", due=5, produced=0, now=T0)
        wd.observe("recheck", due=0, produced=0, now=T0)
        self.assertEqual(wd.state_for("posting").last_due, 5)
        self.assertEqual(wd.state_for("recheck").last_due, 0)
        self.assertEqual(sorted(wd.snapshot()), ["posting", "recheck"])

    def test_a_corrupt_state_file_costs_the_grace_period_and_nothing_else(self):
        wd = self.build()
        wd.observe("posting", due=5, produced=0, now=T0)
        (self.root / "watchdog" / "posting.json").write_text("{ not json", encoding="utf-8")
        verdict = wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        self.assertEqual(verdict.state, STATE_WAITING)   # started over, did not raise
        self.assertEqual(self.delivered, [])


class DeliveryTest(WatchdogTestBase):
    def test_a_broken_sink_cannot_take_a_loop_down(self):
        def boom(alert):
            raise RuntimeError("pager is on fire")

        wd = self.build(sinks=[boom, self.delivered.append])
        wd.observe("posting", due=5, produced=0, now=T0)
        wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        self.assertEqual(self.kinds(), [ALERT_STALLED])   # the other sink still got it

    def test_history_file_is_append_only_json_lines(self):
        wd = LoopWatchdog(state_dir=self.root / "watchdog",
                          sinks=[loop_watchdog.jsonl_sink(self.history)])
        wd.observe("posting", due=5, produced=0, now=T0)
        wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        wd.observe("posting", due=5, produced=3, now=T0 + GRACE + 120)
        lines = [json.loads(l) for l in self.history.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["kind"] for l in lines], [ALERT_STALLED, ALERT_RECOVERED])
        self.assertEqual(lines[0]["loop"], "posting")

    def test_log_sink_shouts_for_a_stall_and_murmurs_for_a_recovery(self):
        class FakeLogger:
            def __init__(self):
                self.errors, self.infos = [], []

            def error(self, message, *args):
                self.errors.append(message % args)

            def info(self, message, *args):
                self.infos.append(message % args)

        logger = FakeLogger()
        wd = LoopWatchdog(state_dir=self.root / "watchdog",
                          sinks=[loop_watchdog.log_sink(logger)])
        wd.observe("posting", due=5, produced=0, now=T0)
        wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        wd.observe("posting", due=5, produced=3, now=T0 + GRACE + 120)
        self.assertEqual(len(logger.errors), 1)
        self.assertIn("LOOP ALERT", logger.errors[0])
        self.assertEqual(len(logger.infos), 1)

    def test_airtable_sink_writes_one_unlinked_run_log_row(self):
        class FakeAirtable:
            def __init__(self):
                self.rows = []

            def create_run_log(self, account_id, account_name, flow, result, notes=None):
                self.rows.append((account_id, account_name, flow, result, notes))
                return "recX"

        airtable = FakeAirtable()
        wd = LoopWatchdog(state_dir=self.root / "watchdog",
                          sinks=[loop_watchdog.airtable_run_log_sink(airtable)])
        wd.observe("posting", due=5, produced=0, now=T0)
        wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)
        self.assertEqual(len(airtable.rows), 1)
        account_id, _, flow, result, notes = airtable.rows[0]
        self.assertIsNone(account_id)          # a stalled loop belongs to no account
        self.assertEqual(flow, "watchdog/posting")
        self.assertEqual(result, at.RESULT_FAILED)
        self.assertIn("STALLED", notes)

    def test_a_failing_airtable_sink_is_swallowed(self):
        class BrokenAirtable:
            def create_run_log(self, *args, **kwargs):
                raise RuntimeError("Airtable is down")

        wd = LoopWatchdog(state_dir=self.root / "watchdog",
                          sinks=[loop_watchdog.airtable_run_log_sink(BrokenAirtable())])
        wd.observe("posting", due=5, produced=0, now=T0)
        self.assertEqual(wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1).state,
                         STATE_STALLED)


# --- where "due" and "produced" actually come from ---------------------------

def _queue_row(scheduled, status=at.POST_STATUS_PENDING):
    return {"id": "recQ", "fields": {at.F_PQ_SCHEDULED: scheduled,
                                     at.F_PQ_POST_STATUS: status}}


class FakePostingAirtable:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_pending_posts(self):
        self.calls += 1
        return self.rows


class PostingSourcesTest(WatchdogTestBase):
    """Posting's two sides: due rows from the Posting Queue, production from the
    post ledger. The ledger is the honest measure -- it is written the instant
    Share is tapped, so a run that launched nothing leaves no trace in it, which
    is exactly the dead-agent / stale-lock signature."""

    def setUp(self):
        super().setUp()
        self.ledger = PostLedger(self.root / "ledger.jsonl")

    def test_only_rows_whose_slot_has_passed_count_as_due(self):
        airtable = FakePostingAirtable([
            _queue_row("2026-08-05T09:00:00.000Z"),     # passed
            _queue_row("2026-08-05T21:00:00.000Z"),     # still ahead
        ])
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(loop_watchdog.due_posting_rows(airtable, now=now), 1)

    def test_the_ledger_only_counts_shares_after_the_last_tick(self):
        old = self.ledger.record_share("p1", "old.mp4", media_hash="h-old")
        self.assertIsNotNone(old)
        since = time.time()
        self.ledger.record_share("p1", "new.mp4", media_hash="h-new")
        self.assertEqual(loop_watchdog.ledger_production_since(self.ledger, since), 1)
        self.assertEqual(loop_watchdog.ledger_production_since(self.ledger, 0), 2)

    def test_empty_queue_reads_idle_not_stalled(self):
        # The empty-queue night. Nothing was owed, so nothing is wrong.
        wd = self.build()
        airtable = FakePostingAirtable([])
        now = time.time()
        loop_watchdog.observe_posting(wd, airtable, ledger=self.ledger, now=now)
        verdict = loop_watchdog.observe_posting(wd, airtable, ledger=self.ledger,
                                                now=now + GRACE + 60)
        self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.delivered, [])

    def test_rows_due_and_an_empty_ledger_stalls(self):
        # The dead-MLX-agent night: rows are due, launches fail, nothing is ever
        # shared, and the loop's own log says nothing more alarming than usual.
        wd = self.build()
        airtable = FakePostingAirtable([_queue_row("2020-01-01T00:00:00.000Z")])
        now = time.time()
        loop_watchdog.observe_posting(wd, airtable, ledger=self.ledger, now=now)
        verdict = loop_watchdog.observe_posting(wd, airtable, ledger=self.ledger,
                                                now=now + GRACE + 60)
        self.assertEqual(verdict.state, STATE_STALLED)
        self.assertEqual(self.kinds(), [ALERT_STALLED])

    def test_a_share_in_the_window_keeps_it_healthy(self):
        wd = self.build()
        airtable = FakePostingAirtable([_queue_row("2020-01-01T00:00:00.000Z")])
        now = time.time()
        loop_watchdog.observe_posting(wd, airtable, ledger=self.ledger, now=now)
        self.ledger.record_share("p1", "clip.mp4", media_hash="h1")
        verdict = loop_watchdog.observe_posting(wd, airtable, ledger=self.ledger,
                                                now=now + GRACE + 60)
        self.assertEqual(verdict.state, STATE_OK)
        self.assertEqual(self.delivered, [])

    def test_an_airtable_failure_is_not_reported_as_the_loop_stalling(self):
        class Broken:
            def list_pending_posts(self):
                raise RuntimeError("Airtable 503")

        wd = self.build()
        self.assertIsNone(loop_watchdog.observe_posting(wd, Broken(), ledger=self.ledger))
        self.assertEqual(self.delivered, [])
        self.assertEqual(wd.snapshot(), {})


class QueueSourcesTest(WatchdogTestBase):
    """Built from real `plan_slot_rows` output, so the skip-reason strings the
    watchdog keys on cannot drift away from the ones queue_runner writes."""

    def _plan(self, variants, queue_rows=()):
        target = queue_runner.SlotTarget(queue_runner.TARGET_PROFILE, "prof1", "Jil 1")
        return queue_runner.plan_slot_rows(
            [target], list(variants), list(queue_rows),
            now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
            slot_times=("09:00",), tz=timezone.utc,
        )

    def test_a_target_starved_of_content_is_not_due(self):
        """The queue cannot write a row with no Ready variant to write about.

        Changed 2026-08-05 after this fired for real: counting starved targets
        as due made the loop permanently STALLED, because 45 unused "Blank (N)"
        staging profiles and one model with no raw videos are starved every tick
        and always will be. Being out of content is a content problem.
        """
        wd = self.build()
        report = self._plan(variants=[])
        verdict = loop_watchdog.observe_queue(wd, report, now=T0)
        self.assertEqual(verdict.due, 0)
        self.assertEqual(verdict.produced, 0)
        # Still recorded in the state file, so running dry stays visible to the
        # report page and to anyone reading the watchdog -- it just is not an alert.
        self.assertIn("1 target(s) with an unserved slot and no content",
                      wd.state_for("queue").detail)

    def test_a_variant_held_by_an_existing_row_is_not_due(self):
        # The one Ready variant belongs to yesterday's 21:00 row, so today's
        # 09:00 slot is unserved and there is nothing left to serve it with --
        # again nothing the queue can act on.
        held = {"id": "recV", "profile_id": "prof1", "status": at.SV_STATUS_READY}
        row = {"id": "recQ", "fields": {
            at.F_PQ_SCHEDULED: "2026-08-04T21:00:00.000Z",
            at.F_PQ_NAME: "Jil 1 / 21:00",
            at.F_PQ_POST_STATUS: at.POST_STATUS_FAILED,
            at.F_PQ_SPOOF_VARIANT: ["recV"],
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        }}
        report = self._plan(variants=[held], queue_rows=[row])
        self.assertEqual(loop_watchdog.observe_queue(self.build(), report, now=T0).due, 0)

    def test_a_fleet_with_no_content_never_stalls_the_queue(self):
        """The 2026-08-05 regression, end to end: many starved targets, forever."""
        wd = self.build()
        report = self._plan(variants=[])
        grace = loop_watchdog.stall_after_seconds("queue")
        for tick in range(12):                      # three hours of ticks
            verdict = loop_watchdog.observe_queue(wd, report, now=T0 + tick * (grace / 2))
        self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.delivered, [])

    def test_a_slot_that_is_already_served_reads_idle(self):
        row = {"id": "recQ", "fields": {
            at.F_PQ_SCHEDULED: "2026-08-05T09:00:00.000Z",
            at.F_PQ_NAME: "Jil 1 / 09:00",
            at.F_PQ_POST_STATUS: at.POST_STATUS_POSTED,
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        }}
        report = self._plan(variants=[], queue_rows=[row])
        wd = self.build()
        self.assertEqual(loop_watchdog.observe_queue(wd, report, now=T0).due, 0)
        verdict = loop_watchdog.observe_queue(wd, report, now=T0 + 10 * 3600)
        self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.delivered, [])

    def test_ready_content_for_an_ineligible_target_is_not_due_work(self):
        # A paused account holding Ready variants must not make the queue look
        # permanently behind -- no row was ever owed for it.
        stranded = {"id": "recV", "profile_id": "somebody-else", "status": at.SV_STATUS_READY}
        row = {"id": "recQ", "fields": {
            at.F_PQ_SCHEDULED: "2026-08-05T09:00:00.000Z",
            at.F_PQ_NAME: "Jil 1 / 09:00",
            at.F_PQ_POST_STATUS: at.POST_STATUS_POSTED,
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        }}
        report = self._plan(variants=[stranded], queue_rows=[row])
        self.assertTrue(report.skipped)          # it IS reported, just not as due
        self.assertEqual(loop_watchdog.observe_queue(self.build(), report, now=T0).due, 0)

    def test_planned_but_unwritten_rows_stall(self):
        report = self._plan(variants=[{"id": "recV", "profile_id": "prof1",
                                       "status": at.SV_STATUS_READY}])
        self.assertEqual(len(report.planned), 1)
        report.rows_created = 0                  # every Airtable write failed
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("queue")
        loop_watchdog.observe_queue(wd, report, now=T0)
        verdict = loop_watchdog.observe_queue(wd, report, now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_STALLED)


class RecheckSourcesTest(WatchdogTestBase):
    """`unknown` is not production. Three passes, three unknowns, no phone ever
    launched -- and every counter that only asked 'did the pass run?' said yes."""

    def test_unknown_forever_stalls(self):
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("recheck")
        tally = {"checked": 3, "posted": 0, "failed": 0, "unknown": 3, "abandoned": 0}
        loop_watchdog.observe_recheck(wd, tally, now=T0)
        verdict = loop_watchdog.observe_recheck(wd, tally, now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_STALLED)
        self.assertEqual(self.kinds(), [ALERT_STALLED])

    def test_rows_leaving_verifying_count_as_production(self):
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("recheck")
        loop_watchdog.observe_recheck(wd, {"unknown": 3}, now=T0)
        verdict = loop_watchdog.observe_recheck(
            wd, {"posted": 2, "failed": 1, "unknown": 1}, now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_OK)
        self.assertEqual(verdict.produced, 3)
        self.assertEqual(self.delivered, [])

    def test_nothing_parked_is_idle(self):
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("recheck")
        empty = {"checked": 0, "posted": 0, "failed": 0, "unknown": 0, "abandoned": 0}
        loop_watchdog.observe_recheck(wd, empty, now=T0)
        self.assertEqual(loop_watchdog.observe_recheck(wd, empty, now=T0 + grace + 60).state,
                         STATE_IDLE)
        self.assertEqual(self.delivered, [])


class PipelineSourcesTest(WatchdogTestBase):
    def test_raw_videos_picked_up_but_no_variants_encoded_stalls(self):
        from adb_bot.automation.spoof_pipeline import PipelineReport

        report = PipelineReport(dry_run=False)
        report.processed_videos = ["clip1.mp4", "clip2.mp4"]
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("pipeline")
        loop_watchdog.observe_pipeline(wd, report, now=T0)
        verdict = loop_watchdog.observe_pipeline(wd, report, now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_STALLED)

    def test_no_raw_stock_is_idle_not_an_alert(self):
        from adb_bot.automation.spoof_pipeline import PipelineReport

        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("pipeline")
        loop_watchdog.observe_pipeline(wd, PipelineReport(dry_run=False), now=T0)
        verdict = loop_watchdog.observe_pipeline(wd, PipelineReport(dry_run=False),
                                                 now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.delivered, [])

    def test_clips_for_a_model_with_no_profiles_are_not_idle(self):
        """The silent failure this whole change exists for.

        A raw folder whose model has no target contributes 0 processed videos
        and 0 variants, so the watchdog read IDLE -- "no new raw video" -- while
        clips were piling up in Drive going nowhere. That is production failure,
        and unlike the queue's starved targets it is cleared by finishing the
        model's onboarding rather than by deleting rows.
        """
        from adb_bot.automation.spoof_pipeline import PipelineReport

        report = PipelineReport(dry_run=False)
        report.skipped = [("clip1.mp4", "no MLX profiles under model 'Kathi'"),
                          ("clip2.mp4", "no MLX profiles under model 'Kathi'")]
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("pipeline")
        loop_watchdog.observe_pipeline(wd, report, now=T0)
        verdict = loop_watchdog.observe_pipeline(wd, report, now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_STALLED)
        self.assertEqual(verdict.due, 2)
        # The alert has to name the condition, or "pipeline stalled" sends
        # somebody looking at the encoder instead of at the model's profiles.
        self.assertTrue(any("no profile to spoof for" in a.detail for a in verdict.alerts),
                        [a.detail for a in verdict.alerts])

    def test_an_ordinary_skip_does_not_make_the_loop_due(self):
        # The run cap is the loop doing its job, not a gap. Counting every skip
        # would make a capped run alert every night.
        from adb_bot.automation.spoof_pipeline import PipelineReport

        report = PipelineReport(dry_run=False)
        report.skipped = [("clip9.mp4", "run cap of 20 variant(s) reached")]
        wd = self.build()
        grace = loop_watchdog.stall_after_seconds("pipeline")
        loop_watchdog.observe_pipeline(wd, report, now=T0)
        verdict = loop_watchdog.observe_pipeline(wd, report, now=T0 + grace + 60)
        self.assertEqual(verdict.state, STATE_IDLE)
        self.assertEqual(self.delivered, [])


class DoctorSurfacesTheAlertTest(WatchdogTestBase):
    """The alert fires once, at 02:00, into a log. Somebody running `doctor` at
    09:00 has to be able to find out without knowing which file to read."""

    def test_a_live_stall_fails_the_doctor_check(self):
        from adb_bot.automation import doctor

        wd = self.build()
        wd.observe("posting", due=5, produced=0, now=T0)
        wd.observe("posting", due=5, produced=0, now=T0 + GRACE + 1)

        original = loop_watchdog.LoopWatchdog
        state_dir = self.root / "watchdog"

        def patched(*args, **kwargs):
            kwargs.setdefault("state_dir", state_dir)
            return original(*args, **kwargs)

        loop_watchdog.LoopWatchdog = patched
        try:
            result = doctor.check_loop_production()
        finally:
            loop_watchdog.LoopWatchdog = original
        self.assertEqual(result.status, doctor.FAIL)
        self.assertIn("posting", result.detail)

    def test_status_lines_render_for_a_human(self):
        wd = self.build()
        wd.observe("posting", due=5, produced=0, now=time.time())
        text = loop_watchdog.format_status(wd.snapshot())
        self.assertIn("posting", text)
        self.assertEqual(loop_watchdog.format_status({}),
                         "no loop has reported to the watchdog yet")
