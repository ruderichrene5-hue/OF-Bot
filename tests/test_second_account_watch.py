"""The two-account watch: what it says while the rollout is still landing, and
what it raises once it is running.

The rules under test are all about the same thing. Every way second-account
posting can fail leaves a Posting Queue row saying Posted, so none of them will
ever surface on their own -- and a monitor that cried wolf through the first
hours of a rollout would be turned off before it ever caught one.
"""

import time
from unittest import TestCase

from adb_bot.automation import second_account_watch as watch
from adb_bot.automation.second_account_watch import STUCK_STAGE_SECONDS, SILENT_SECONDS, evaluate
from adb_bot.clients import airtable as at

NOW = 1_800_000_000.0
HOUR = 3600.0


def _profile(record_id="p1", name="Jil 5", usable=True):
    return {"record_id": record_id, "name": name, "status": "Active",
            "primary": "helenaiscutee", "second": "jiji.ll12" if usable else None,
            "checked_at": None, "usable": usable}


def _row(profile_id="p1", slot=at.SLOT_PRIMARY, status=at.POST_STATUS_POSTED,
         variant="v1", when=NOW - HOUR, rid=None):
    from datetime import datetime, timezone
    fields = {
        at.F_PQ_POST_STATUS: status,
        at.F_PQ_SCHEDULED: datetime.fromtimestamp(when, timezone.utc).isoformat(),
        at.F_PQ_TARGET_PROFILE: [profile_id],
    }
    if slot:
        fields[at.F_PQ_ACCOUNT_SLOT] = slot
    if variant:
        fields[at.F_PQ_SPOOF_VARIANT] = [variant]
    return {"id": rid or f"q{profile_id}{slot}{variant}{when}", "fields": fields}


def _variant(profile_id="p1", slot=at.SLOT_SECOND, vid="v9"):
    return {"id": vid, "file_path": f"/out/{vid}.mp4", "status": at.SV_STATUS_READY,
            "account_id": None, "profile_id": profile_id, "slot": slot, "created": "2026-08-07"}


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


class NothingToWatchTest(TestCase):
    def test_a_base_without_the_fields_is_not_a_fault(self):
        report = evaluate(None, [], now=NOW)
        self.assertEqual(report.failures, [])

    def test_a_fleet_with_no_two_account_phone_is_not_a_fault(self):
        report = evaluate([], [], now=NOW)
        self.assertEqual(report.failures, [])


class ConfigurationTest(TestCase):
    """The one failure that is a person's to fix, so it never waits."""

    def test_a_missing_handle_fails_from_the_first_tick(self):
        report = evaluate([_profile(usable=False)], [], state={"first_seen_at": NOW}, now=NOW)
        self.assertIn("handles_complete", report.failures)
        self.assertIn("Jil 5", _check(report, "handles_complete").detail)

    def test_both_handles_known_passes(self):
        report = evaluate([_profile()], [], state={"first_seen_at": NOW}, now=NOW)
        self.assertNotIn("handles_complete", report.failures)


class RolloutIsNotAFailureTest(TestCase):
    """Before the first post, "not there yet" must read as progress."""

    def test_a_fresh_fleet_raises_nothing(self):
        report = evaluate([_profile()], [], ready_variants=[],
                          state={"first_seen_at": NOW}, now=NOW)
        self.assertEqual(report.failures, [])
        self.assertFalse(report.started)
        self.assertTrue(_check(report, "spoofed_for_second").pending)

    def test_variants_made_but_no_rows_yet_is_still_progress(self):
        report = evaluate([_profile()], [], ready_variants=[_variant()],
                          state={"first_seen_at": NOW}, now=NOW)
        self.assertEqual(report.failures, [])
        self.assertEqual(report.second_variants, 1)
        self.assertTrue(_check(report, "spoofed_for_second").ok)

    def test_a_stage_stuck_for_hours_does_fail(self):
        """Progress and paralysis look identical in a single snapshot; the only
        thing that separates them is how long it has looked that way."""
        state = {"first_seen_at": NOW - STUCK_STAGE_SECONDS - HOUR}
        report = evaluate([_profile()], [], ready_variants=[], state=state, now=NOW)
        self.assertIn("spoofed_for_second", report.failures)

    def test_video_ready_but_never_queued_is_the_queue_loops_fault(self):
        state = {"first_seen_at": NOW - STUCK_STAGE_SECONDS - HOUR}
        report = evaluate([_profile()], [], ready_variants=[_variant()], state=state, now=NOW)
        self.assertIn("queued_for_second", report.failures)
        self.assertIn("still got none", _check(report, "queued_for_second").detail)

    def test_queued_but_never_posted_fails_once_it_has_had_time(self):
        rows = [_row(slot=at.SLOT_SECOND, status=at.POST_STATUS_PENDING, variant="v2")]
        state = {"first_seen_at": NOW - STUCK_STAGE_SECONDS - HOUR}
        report = evaluate([_profile()], rows, state=state, now=NOW)
        self.assertIn("second_account_posts", report.failures)


class ItStartedWorkingTest(TestCase):
    def test_the_first_post_flips_it_to_started(self):
        rows = [_row(slot=at.SLOT_SECOND, variant="v2")]
        report = evaluate([_profile()], rows, state={"first_seen_at": NOW}, now=NOW)
        self.assertTrue(report.started)
        self.assertEqual(report.second_posted, 1)
        self.assertAlmostEqual(report.first_post_at, NOW - HOUR, delta=60)

    def test_it_stays_started_from_the_state_file(self):
        """A day with no second-account row must not read as "never worked" --
        the correctness checks would switch themselves off."""
        report = evaluate([_profile()], [], state={"first_seen_at": NOW - 10 * HOUR,
                                                   "first_post_at": NOW - 5 * HOUR}, now=NOW)
        self.assertTrue(report.started)

    def test_the_correctness_checks_only_exist_once_it_started(self):
        fresh = evaluate([_profile()], [], state={"first_seen_at": NOW}, now=NOW)
        self.assertNotIn("no_shared_clip", [c.name for c in fresh.checks])
        running = evaluate([_profile()], [_row(slot=at.SLOT_SECOND, variant="v2")],
                           state={"first_seen_at": NOW}, now=NOW)
        self.assertIn("no_shared_clip", [c.name for c in running.checks])


class CorrectnessTest(TestCase):
    """Once it runs, the quiet failures."""

    STATE = {"first_seen_at": NOW - 10 * HOUR, "first_post_at": NOW - 5 * HOUR}

    def test_one_clip_on_both_accounts_is_caught(self):
        """The expensive one: duplicate content, and both rows say Posted."""
        rows = [_row(slot=at.SLOT_PRIMARY, variant="shared"),
                _row(slot=at.SLOT_SECOND, variant="shared")]
        report = evaluate([_profile()], rows, state=self.STATE, now=NOW)
        self.assertIn("no_shared_clip", report.failures)
        self.assertIn("duplicate content", _check(report, "no_shared_clip").detail)

    def test_separate_clips_pass(self):
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a"),
                _row(slot=at.SLOT_SECOND, variant="b")]
        report = evaluate([_profile()], rows, state=self.STATE, now=NOW)
        self.assertNotIn("no_shared_clip", report.failures)

    def test_a_second_account_that_stopped_is_caught(self):
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a", when=NOW - HOUR),
                _row(slot=at.SLOT_SECOND, variant="b", when=NOW - SILENT_SECONDS - HOUR)]
        report = evaluate([_profile()], rows, state=self.STATE, now=NOW)
        self.assertIn("second_accounts_still_posting", report.failures)

    def test_a_phone_stuck_on_the_second_account_is_caught(self):
        """The first account's posts stop landing while the second's keep going --
        which is what a phone that never switches back looks like."""
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a", when=NOW - SILENT_SECONDS - 2 * HOUR),
                _row(slot=at.SLOT_SECOND, variant="b", when=NOW - HOUR)]
        report = evaluate([_profile()], rows, state=self.STATE, now=NOW)
        self.assertIn("first_accounts_still_posting", report.failures)
        self.assertIn("stuck on the second account",
                      _check(report, "first_accounts_still_posting").detail)

    def test_both_accounts_posting_recently_passes_everything(self):
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a", when=NOW - 2 * HOUR),
                _row(slot=at.SLOT_SECOND, variant="b", when=NOW - HOUR)]
        report = evaluate([_profile()], rows, state=self.STATE, now=NOW)
        self.assertEqual(report.failures, [])

    def test_switch_refusals_in_the_log_are_counted(self):
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a"), _row(slot=at.SLOT_SECOND, variant="b")]
        log = ("Not posting on 1.2.3.4:5555: could not prove it is signed in as @jiji.ll12. "
               "The clip stays queued\n"
               "The account switcher on 1.2.3.4:5555 does not list @jills.sav -- Airtable\n")
        report = evaluate([_profile()], rows, state=self.STATE, log_text=log, now=NOW)
        self.assertEqual(report.switch_failures, 2)
        self.assertIn("account_switch_works", report.failures)

    def test_a_clean_log_passes(self):
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a"), _row(slot=at.SLOT_SECOND, variant="b")]
        report = evaluate([_profile()], rows, state=self.STATE,
                          log_text="Posted a reel. All good.\n", now=NOW)
        self.assertNotIn("account_switch_works", report.failures)


class OtherPhonesAreNotItsBusinessTest(TestCase):
    def test_rows_for_single_account_phones_are_ignored(self):
        """Most of the fleet has one account; its rows must not be read as a
        two-account phone's primary half."""
        rows = [_row(profile_id="other", slot=None, variant="a")]
        report = evaluate([_profile()], rows, state={"first_seen_at": NOW}, now=NOW)
        self.assertEqual(report.second_rows, 0)
        self.assertFalse(report.started)

    def test_an_unusable_phones_rows_do_not_count(self):
        rows = [_row(profile_id="p1", slot=at.SLOT_SECOND, variant="a")]
        report = evaluate([_profile(usable=False)], rows,
                          state={"first_seen_at": NOW}, now=NOW)
        self.assertEqual(report.second_rows, 0)


class LogParsingTest(TestCase):
    def test_every_refusal_sentence_the_flow_emits_is_matched(self):
        """These strings are the contract between the flow and this module. If
        one is reworded without updating the other, the monitor goes quiet --
        which is the failure it exists to prevent."""
        from adb_bot.automation.flows import instagram_reel
        import inspect

        source = inspect.getsource(instagram_reel)
        for marker in watch.SWITCH_FAILURE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, source.lower())


class StateFileTest(TestCase):
    def test_the_first_post_time_survives_a_restart(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(watch.save_state({"first_post_at": 123.0}, app_dir=tmp))
            self.assertEqual(watch.load_state(app_dir=tmp)["first_post_at"], 123.0)

    def test_a_missing_state_file_reads_as_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(watch.load_state(app_dir=tmp), {})


class RunWatchTest(TestCase):
    class FakeAirtable:
        def __init__(self, profiles, rows, variants):
            self._p, self._r, self._v = profiles, rows, variants

        def second_account_profiles(self):
            return self._p

        def list_queue_rows(self, statuses=None):
            return self._r

        def list_ready_variants(self):
            return self._v

    class FakeWatchdog:
        def __init__(self):
            self.seen = []

        def observe_health(self, loop, failures, checked=0, detail="", now=None):
            self.seen.append((loop, list(failures), checked))

    class FakeLogger:
        def __init__(self):
            self.lines = []

        def _log(self, message, *args):
            self.lines.append(message % args if args else message)

        info = warning = error = _log

    def test_it_reports_failures_to_the_watchdog(self):
        import tempfile

        rows = [_row(slot=at.SLOT_PRIMARY, variant="shared"),
                _row(slot=at.SLOT_SECOND, variant="shared")]
        airtable = self.FakeAirtable([_profile()], rows, [])
        watchdog, logger = self.FakeWatchdog(), self.FakeLogger()
        with tempfile.TemporaryDirectory() as tmp:
            report = watch.run_watch(airtable, logger, log_path="/nonexistent",
                                     watchdog=watchdog, app_dir=tmp, now=NOW)
            # The first post is remembered, so the next run stays in the
            # correctness phase even if the fleet goes quiet.
            self.assertTrue(watch.load_state(app_dir=tmp)["first_post_at"])

        self.assertIn("no_shared_clip", report.failures)
        self.assertEqual(watchdog.seen[0][0], "second-accounts")
        self.assertIn("no_shared_clip", watchdog.seen[0][1])

    def test_an_airtable_failure_is_a_failing_check_not_a_crash(self):
        class Broken:
            def second_account_profiles(self):
                raise RuntimeError("boom")

        logger = self.FakeLogger()
        report = watch.run_watch(Broken(), logger, log_path="/nonexistent", now=NOW)
        self.assertEqual(report.failures, ["airtable_readable"])


class ScheduleTest(TestCase):
    def test_the_loop_is_runnable_and_scheduled(self):
        from adb_bot.automation import run_loop, schedule_spec

        self.assertIn("second-accounts", run_loop.LOOPS)
        self.assertIn("second-accounts", run_loop._DISPATCH)
        self.assertIn("second-accounts", schedule_spec.RECOMMENDED_LOOPS)
        self.assertIn("second-accounts", schedule_spec.RECOMMENDED_INTERVALS)
        self.assertTrue(schedule_spec.WHAT_IT_DOES.get("second-accounts"))


class FutureDatedRowTest(TestCase):
    """A row scheduled for later must not become "the last post".

    Every staleness rule here is `now - last`. A future `last` makes that
    negative, so "the second accounts have stopped" could never fire again --
    the monitor would look healthy precisely because it had gone blind. Seen
    live on 2026-08-07, from a hand-made test row dated 23:00.
    """

    def test_a_future_posted_row_does_not_silence_the_staleness_check(self):
        rows = [_row(slot=at.SLOT_PRIMARY, variant="a", when=NOW - 40 * HOUR),
                # Posted, but dated six hours from now.
                _row(slot=at.SLOT_SECOND, variant="b", when=NOW + 6 * HOUR),
                _row(slot=at.SLOT_SECOND, variant="c", when=NOW - 40 * HOUR)]
        state = {"first_seen_at": NOW - 50 * HOUR, "first_post_at": NOW - 40 * HOUR}
        report = evaluate([_profile()], rows, state=state, now=NOW)
        # The fleet really has been quiet for 40h, and it says so.
        self.assertIn("second_accounts_still_posting", report.failures)

    def test_the_first_post_is_never_stamped_in_the_future(self):
        rows = [_row(slot=at.SLOT_SECOND, variant="b", when=NOW + 6 * HOUR)]
        report = evaluate([_profile()], rows, state={"first_seen_at": NOW}, now=NOW)
        self.assertTrue(report.started)
        self.assertLessEqual(report.first_post_at, NOW)
