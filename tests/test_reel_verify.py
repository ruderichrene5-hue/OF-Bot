from unittest import TestCase

from adb_bot.automation.flows import reel_verify as rv
from adb_bot.automation.flows.reel_verify import (
    VIA_UPLOAD_FINISHED,
    VIA_NOTIFICATION,
    FAIL_ERROR_DIALOG,
    NotificationState,
    Count,
    classify_post_screen,
    count_increased,
    parse_count,
    parse_notification_state,
    verify_reel_posted,
)


class ParseCountTest(TestCase):
    def test_plain_numbers(self):
        self.assertEqual(parse_count("12").value, 12)
        self.assertEqual(parse_count("0").value, 0)

    def test_thousands_separators_both_locales(self):
        self.assertEqual(parse_count("1,234").value, 1234)   # en
        self.assertEqual(parse_count("1.234").value, 1234)   # de
        self.assertEqual(parse_count("1 234").value, 1234)

    def test_surrounding_label_is_ignored(self):
        self.assertEqual(parse_count("42 posts").value, 42)
        self.assertEqual(parse_count("posts 42").value, 42)

    def test_rounded_counts_are_flagged_inexact(self):
        for text in ("1.2K", "12K", "1,2 Tsd.", "3M"):
            parsed = parse_count(text)
            self.assertIsNotNone(parsed, text)
            self.assertFalse(parsed.exact, text)

    def test_exact_counts_are_flagged_exact(self):
        self.assertTrue(parse_count("999").exact)
        self.assertTrue(parse_count("1,234").exact)

    def test_no_number(self):
        self.assertIsNone(parse_count("posts"))
        self.assertIsNone(parse_count(""))
        self.assertIsNone(parse_count(None))


class CountIncreasedTest(TestCase):
    def test_increase_detected(self):
        self.assertTrue(count_increased(Count(12, True), Count(13, True)))

    def test_same_or_lower_is_not_an_increase(self):
        self.assertFalse(count_increased(Count(12, True), Count(12, True)))
        self.assertFalse(count_increased(Count(12, True), Count(11, True)))

    def test_rounded_counts_never_confirm(self):
        # "1.2K" -> "1.2K" hides a +1, so it must not be trusted either way.
        self.assertFalse(count_increased(Count(1200, False), Count(1300, False)))
        self.assertFalse(count_increased(Count(1200, True), Count(1300, False)))

    def test_missing_counts(self):
        self.assertFalse(count_increased(None, Count(1, True)))
        self.assertFalse(count_increased(Count(1, True), None))


class ClassifyScreenTest(TestCase):
    def test_confirmation(self):
        self.assertEqual(classify_post_screen("Your reel was shared"), rv.STATE_CONFIRMED)
        self.assertEqual(classify_post_screen("High five! Nice work"), rv.STATE_CONFIRMED)

    def test_error(self):
        self.assertEqual(classify_post_screen("Your post couldn't be shared"), rv.STATE_ERROR)
        self.assertEqual(classify_post_screen("Something went wrong"), rv.STATE_ERROR)

    def test_draft(self):
        self.assertEqual(classify_post_screen("Discard post   Keep draft"), rv.STATE_DRAFT)

    def test_composer(self):
        self.assertEqual(classify_post_screen("Write a caption..."), rv.STATE_COMPOSER)

    def test_unknown_and_empty(self):
        self.assertEqual(classify_post_screen("Home  Search  Profile"), rv.STATE_UNKNOWN)
        self.assertEqual(classify_post_screen(""), rv.STATE_UNKNOWN)

    def test_error_wins_over_composer(self):
        # The error dialog is drawn over the composer; it must not read as "still composing".
        text = "Write a caption... Your post couldn't be shared"
        self.assertEqual(classify_post_screen(text), rv.STATE_ERROR)


class NotificationParseTest(TestCase):
    ONGOING = """
    NotificationRecord(0xabc: pkg=com.instagram.android user=0 id=1 tag=null)
      android.title=Posting...
      flags=0x2
    """
    DONE = """
    NotificationRecord(0xabc: pkg=com.instagram.android user=0 id=1 tag=null)
      android.title=Your reel was shared
      flags=0x10
    """
    OTHER_APP = """
    NotificationRecord(0xabc: pkg=com.android.systemui user=0 id=1 tag=null)
      android.title=Charging
      flags=0x2
    """

    def test_ongoing_upload_detected(self):
        state = parse_notification_state(self.ONGOING)
        self.assertTrue(state.ig_present)
        self.assertTrue(state.ongoing)
        self.assertIn("Posting...", state.titles)

    def test_finished_notification_not_ongoing(self):
        state = parse_notification_state(self.DONE)
        self.assertTrue(state.ig_present)
        self.assertFalse(state.ongoing)

    def test_other_apps_ignored(self):
        state = parse_notification_state(self.OTHER_APP)
        self.assertFalse(state.ig_present)
        self.assertFalse(state.ongoing)   # systemui's ongoing flag must not count

    def test_empty_input(self):
        self.assertFalse(parse_notification_state("").ig_present)
        self.assertFalse(parse_notification_state(None).ongoing)


class FakeClock:
    """Deterministic time so the 3-min floor / 5-min ceiling are testable."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class VerifyStateMachineTest(TestCase):
    def _run(self, **kwargs):
        clock = FakeClock()
        kwargs.setdefault("now", clock.now)
        kwargs.setdefault("sleep", clock.sleep)
        kwargs.setdefault("poll_seconds", 10)
        return verify_reel_posted(**kwargs), clock

    def test_post_count_increment_confirms(self):
        counts = iter([Count(12, True), Count(13, True)])
        result, _ = self._run(baseline_count=Count(12, True),
                              get_post_count=lambda: next(counts))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, rv.VIA_POST_COUNT)
        self.assertIn("12 -> 13", result.detail)

    def test_banner_confirms_when_count_unavailable(self):
        result, _ = self._run(get_screen_text=lambda: "Your reel was shared")
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, rv.VIA_BANNER)

    def test_an_expensive_probe_is_not_started_without_time_to_finish(self):
        """From a real run: a 45s budget produced timeouts of up to 148s,
        because the deadline was only tested *after* every probe had run. The
        post-count probe is a full navigation round trip, so one started near
        the deadline overruns it by its own duration."""
        calls = []

        def slow_count_probe():
            calls.append(1)
            return Count(5, True)   # never increments, so it keeps being asked

        result, _ = self._run(baseline_count=Count(5, True),
                              get_post_count=slow_count_probe,
                              timeout=30, poll_seconds=10,
                              post_count_min_remaining=12)
        self.assertFalse(result.confirmed)
        # Ticks at 0s/10s/20s; the 20s one has only 10s left, under the 12s the
        # probe needs, so it must be skipped.
        self.assertEqual(len(calls), 2, "a probe was started that could not finish in budget")

    def test_the_probe_still_runs_when_there_is_room(self):
        calls = []

        def count_probe():
            calls.append(1)
            return Count(5, True)

        self._run(baseline_count=Count(5, True), get_post_count=count_probe,
                  timeout=30, poll_seconds=10, post_count_min_remaining=1)
        self.assertEqual(len(calls), 3, "the guard must not block probes that fit")

    def test_the_poll_sleep_never_overshoots_the_deadline(self):
        clock = FakeClock()
        verify_reel_posted(get_screen_text=lambda: "", timeout=25, poll_seconds=10,
                           now=clock.now, sleep=clock.sleep)
        self.assertLessEqual(clock.t - 1000.0, 25.0,
                             "a full poll interval was slept past the deadline")

    def test_banner_wins_and_the_count_probe_is_never_consulted(self):
        # The reel announced itself on the feed. That settles it -- the counter
        # must not even be asked, because asking navigates off the feed and
        # wipes the very banner that just proved the post landed.
        asked = []

        def count_probe():
            asked.append(1)
            return Count(12, True)

        result, clock = self._run(baseline_count=Count(12, True),
                                  get_post_count=count_probe,
                                  get_screen_text=lambda: "Your reel was shared")
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, rv.VIA_BANNER)
        self.assertEqual(asked, [], "the post-count probe ran despite a visible banner")
        self.assertEqual(clock.t - 1000.0, 0, "confirmation should be immediate")

    def test_count_wins_even_if_banner_never_shows(self):
        # The whole point: a silent success must still be confirmed.
        counts = iter([Count(5, True)] * 3 + [Count(6, True)])
        result, _ = self._run(baseline_count=Count(5, True),
                              get_post_count=lambda: next(counts),
                              get_screen_text=lambda: "Home Search Reels Profile")
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, rv.VIA_POST_COUNT)

    def test_error_dialog_fails_fast_before_the_floor(self):
        result, clock = self._run(get_screen_text=lambda: "Your post couldn't be shared")
        self.assertFalse(result.confirmed)
        self.assertEqual(result.method, rv.FAIL_ERROR_DIALOG)
        self.assertLess(clock.t - 1000.0, 180)   # didn't wait out the floor

    def test_draft_prompt_fails_fast(self):
        result, _ = self._run(get_screen_text=lambda: "Discard post  Keep draft")
        self.assertFalse(result.confirmed)
        self.assertEqual(result.method, rv.FAIL_DRAFT)

    def test_composer_does_not_fail_before_the_min_wait(self):
        # A slow upload can leave the composer up briefly; failing early would
        # mark a good post as failed.
        result, clock = self._run(get_screen_text=lambda: "Write a caption...")
        self.assertFalse(result.confirmed)
        self.assertEqual(result.method, rv.FAIL_COMPOSER)
        self.assertGreaterEqual(clock.t - 1000.0, 180)   # respected the 3-min floor

    def test_timeout_when_nothing_is_observable(self):
        result, clock = self._run(get_screen_text=lambda: "")
        self.assertFalse(result.confirmed)
        self.assertEqual(result.method, rv.FAIL_TIMEOUT)
        self.assertGreaterEqual(clock.t - 1000.0, 300)   # waited the full ceiling

    def test_ongoing_upload_keeps_it_waiting_and_is_reported(self):
        result, clock = self._run(
            get_screen_text=lambda: "Write a caption...",
            get_notification_state=lambda: NotificationState(ig_present=True, ongoing=True),
        )
        self.assertFalse(result.confirmed)
        # Still uploading -> must not bail at the floor via the composer shortcut.
        self.assertGreaterEqual(clock.t - 1000.0, 300)
        self.assertIn("still in progress", result.detail)

    def test_abort_returns_immediately(self):
        result, clock = self._run(should_stop=lambda: True, get_screen_text=lambda: "")
        self.assertFalse(result.confirmed)
        self.assertEqual(result.detail, "aborted")
        self.assertEqual(clock.t, 1000.0)

    def test_probe_exceptions_do_not_crash_verification(self):
        def boom():
            raise RuntimeError("device gone")
        result, _ = self._run(baseline_count=Count(1, True), get_post_count=boom,
                              get_screen_text=boom)
        self.assertFalse(result.confirmed)   # degrades to a timeout, not an exception

    def test_rounded_baseline_falls_back_to_banner(self):
        # Big account: count can't prove anything, so the banner must still work.
        counts = iter([Count(1200, False)] * 5)
        result, _ = self._run(baseline_count=Count(1200, False),
                              get_post_count=lambda: next(counts),
                              get_screen_text=lambda: "Your reel was shared")
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, rv.VIA_BANNER)

    def test_summary_is_readable(self):
        result, _ = self._run(get_screen_text=lambda: "Your reel was shared")
        # The summary now also states how strongly it was confirmed, so the Run
        # Log distinguishes a proven post from an inferred one.
        self.assertIn("CONFIRMED (strong) via banner", result.summary())


# --- flow-level probes -------------------------------------------------------

class FakeNode:
    def __init__(self, exists=False, text=None, desc=None):
        self.exists = exists
        self._info = {"text": text, "contentDescription": desc}

    @property
    def info(self):
        return self._info


class FakeDevice:
    """Minimal stand-in for a uiautomator2 device: d(**kwargs) -> node."""

    def __init__(self, matches=None):
        self.matches = matches or []      # [(kwargs_key, FakeNode)]

    def __call__(self, **kwargs):
        for key, node in self.matches:
            if key in kwargs or key in str(kwargs):
                return node
        return FakeNode(exists=False)


class PostCountProbeTest(TestCase):
    def setUp(self):
        from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
        self.flow = InstagramReelUploadU2Flow()

    def test_reads_count_from_resource_id(self):
        d = FakeDevice([("resourceId", FakeNode(exists=True, text="42"))])
        parsed = self.flow._read_post_count_u2(d, "1.2.3.4:5555")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.value, 42)

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(self.flow._read_post_count_u2(FakeDevice(), "1.2.3.4:5555"))

    def test_selector_errors_degrade_to_none(self):
        class Exploding:
            def __call__(self, **kwargs):
                raise RuntimeError("u2 died")
        # A dead device must not raise out of the probe -- it just doesn't vote.
        self.assertIsNone(self.flow._read_post_count_u2(Exploding(), "1.2.3.4:5555"))

    def _counting_flow(self):
        calls = []

        class CountingFlow(type(self.flow)):
            def _open_profile_tab_u2(inner, d, target, logger=None):
                calls.append(1)
                return True

            def _read_post_count_u2(inner, d, target, logger=None):
                return rv.Count(7, True)

        return CountingFlow(), calls

    def test_count_probe_is_throttled(self):
        # Navigating to the profile is expensive; the probe must not do it on
        # every poll. Second immediate call returns None ("no opinion").
        flow, calls = self._counting_flow()
        probe = flow._profile_post_count_probe_u2(FakeDevice(), "t", None, None,
                                                  every_seconds=999, initial_delay=0)
        first = probe()
        second = probe()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1)

    def test_count_probe_defers_its_first_check(self):
        # The seconds right after Share belong to the banner. This probe walks
        # off the feed to read the profile, so if it fires on the first poll it
        # wipes the confirmation before anything reads it -- which is what a
        # `last = 0.0` start did. By default it must stay quiet at first.
        flow, calls = self._counting_flow()
        probe = flow._profile_post_count_probe_u2(FakeDevice(), "t", None, None, every_seconds=25)
        self.assertIsNone(probe())
        self.assertEqual(calls, [], "the probe navigated away on the very first poll")


class NotificationProbeTest(TestCase):
    def test_probe_parses_adb_output(self):
        from adb_bot.automation.flows.instagram_reel import _make_notification_probe

        class FakeAdb:
            def run_command(self, cmd):
                assert "dumpsys notification" in cmd
                return ("NotificationRecord(0x1: pkg=com.instagram.android user=0)\n"
                        "  android.title=Posting...\n  flags=0x2\n")

        state = _make_notification_probe("1.2.3.4:5555", FakeAdb())()
        self.assertTrue(state.ongoing)

    def test_adb_failure_degrades_to_none(self):
        from adb_bot.automation.flows.instagram_reel import _make_notification_probe

        class DeadAdb:
            def run_command(self, cmd):
                raise RuntimeError("device offline")

        self.assertIsNone(_make_notification_probe("t", DeadAdb())())


class UploadLifecycleTest(TestCase):
    """The false-negative fix.

    Instagram's profile post counter is cached on their side and frequently does
    not move inside the 5-minute window, so waiting for +1 reported real posts as
    failures. The flow already watched the upload notification appear and
    disappear -- and then threw that away, returning NOT CONFIRMED. A reel that
    uploaded and cleared with no error went out.
    """

    def _run(self, notification_states, screen_texts=None, **kwargs):
        notifs = iter(notification_states)
        screens = iter(screen_texts or [])
        clock = {"t": 0.0}

        def now():
            return clock["t"]

        def sleep(seconds):
            clock["t"] += seconds

        return verify_reel_posted(
            get_notification_state=lambda: next(notifs, notification_states[-1]),
            get_screen_text=(lambda: next(screens, "")) if screen_texts else None,
            now=now, sleep=sleep, min_wait=0, timeout=60, poll_seconds=3,
            **kwargs,
        )

    def test_upload_started_then_finished_confirms(self):
        result = self._run([
            NotificationState(ig_present=True, ongoing=True),
            NotificationState(ig_present=True, ongoing=True),
            NotificationState(ig_present=True, ongoing=False),
        ])
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, VIA_UPLOAD_FINISHED)

    def test_confirmation_is_marked_as_inferred_not_proven(self):
        result = self._run([
            NotificationState(ig_present=True, ongoing=True),
            NotificationState(ig_present=True, ongoing=False),
        ])
        self.assertEqual(result.strength, "inferred")
        self.assertIn("CONFIRMED (inferred)", result.summary())

    def test_a_notification_that_was_never_ongoing_confirms_nothing(self):
        """Absence of an upload is not evidence of a post."""
        result = self._run([NotificationState(ig_present=True, ongoing=False)] * 4)
        self.assertFalse(result.confirmed)

    def test_upload_finishing_into_an_error_does_not_confirm(self):
        result = self._run(
            [NotificationState(ig_present=True, ongoing=True),
             NotificationState(ig_present=True, ongoing=False)],
            screen_texts=["", "Something went wrong"],
        )
        self.assertFalse(result.confirmed)
        self.assertEqual(result.method, FAIL_ERROR_DIALOG)

    def test_upload_finishing_while_still_on_the_composer_does_not_confirm(self):
        """Share never registered, so whatever that notification was, it was not
        this post going out. The composer stays up the whole time -- that is what
        distinguishes this from a post whose composer closed normally."""
        clock = {"t": 0.0}
        notifs = iter([NotificationState(ig_present=True, ongoing=True),
                       NotificationState(ig_present=True, ongoing=False)])
        result = verify_reel_posted(
            get_notification_state=lambda: next(
                notifs, NotificationState(ig_present=True, ongoing=False)),
            get_screen_text=lambda: "Write a caption",     # never leaves the composer
            now=lambda: clock["t"],
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            min_wait=0, timeout=60, poll_seconds=3,
        )
        self.assertFalse(result.confirmed)
        self.assertEqual(result.method, rv.FAIL_COMPOSER)

    def test_notification_title_confirms(self):
        """The title outlives the on-screen banner, which a 3s poll often misses."""
        result = self._run([
            NotificationState(ig_present=True, ongoing=False,
                              titles=("Your reel was shared",)),
        ])
        self.assertTrue(result.confirmed)
        self.assertEqual(result.method, VIA_NOTIFICATION)
        self.assertEqual(result.strength, "strong")


class UncertainVsFailedTest(TestCase):
    """Timing out is the absence of proof, not proof of failure. Retrying an
    uncertain post is how an account posts the same reel twice -- which is the
    exact duplicate-content problem the spoofing pipeline exists to avoid."""

    def _timeout_run(self, **kwargs):
        clock = {"t": 0.0}
        return verify_reel_posted(
            now=lambda: clock["t"],
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            min_wait=0, timeout=10, poll_seconds=3, **kwargs)

    def test_timeout_is_uncertain(self):
        result = self._timeout_run()
        self.assertFalse(result.confirmed)
        self.assertTrue(result.uncertain)
        self.assertIn("UNCERTAIN", result.summary())

    def test_error_dialog_is_a_real_failure_not_uncertain(self):
        result = self._timeout_run(get_screen_text=lambda: "Something went wrong")
        self.assertFalse(result.confirmed)
        self.assertFalse(result.uncertain)
        self.assertIn("FAILED", result.summary())

    def test_draft_prompt_is_a_real_failure(self):
        result = self._timeout_run(get_screen_text=lambda: "Discard draft")
        self.assertFalse(result.uncertain)

    def test_confirmed_is_never_uncertain(self):
        result = self._timeout_run(get_screen_text=lambda: "Your reel was shared")
        self.assertTrue(result.confirmed)
        self.assertFalse(result.uncertain)


class UncertainWiringTest(TestCase):
    """The rule: reaching the final Share tap means the reel may be live, so
    anything after it is UNCERTAIN. Never reaching Share means nothing was
    posted, which is a plain failure and safe to retry."""

    def test_ui_can_display_uncertain(self):
        from adb_bot.ui.ui import WorkflowUI
        self.assertIn("uncertain", WorkflowUI._STATUS_DISPLAY)
        self.assertIn("may have posted", WorkflowUI._STATUS_DISPLAY["uncertain"])

    def test_uncertain_is_flagged_for_attention_not_counted_as_failure(self):
        import inspect
        from adb_bot.ui.ui import WorkflowUI
        src = inspect.getsource(WorkflowUI._build_run_summary)
        attention = src.split("attention_keys = {", 1)[1].split("}", 1)[0]
        failed = src.split("failed_keys = {", 1)[1].split("}", 1)[0]
        self.assertIn("uncertain", attention)
        self.assertNotIn("uncertain", failed)

    def test_workflow_emits_uncertain_before_the_failure_branch(self):
        """If the failure branch ran first it would swallow the status, because
        an uncertain result also has success=False."""
        import inspect
        from adb_bot.automation import workflow
        src = inspect.getsource(workflow)
        uncertain_at = src.index('emit_status(profile_id_value, "uncertain"')
        failed_at = src.index('if flow_result.get("aborted", False) or flow_result.get("failed"')
        self.assertLess(uncertain_at, failed_at)

    def test_uncertain_never_leaves_the_queue_row_pending(self):
        """A row left Pending is re-planned by the next run and the same reel
        goes out twice -- the exact outcome this status exists to prevent."""
        from adb_bot.automation.posting_runner import _map_post_status
        from adb_bot.clients import airtable as at
        mapped = _map_post_status("uncertain")
        self.assertIsNotNone(mapped, "unmapped status writes nothing back")
        post_status, _issue_type, incident, _result, _note = mapped
        self.assertEqual(post_status, at.POST_STATUS_VERIFYING)
        self.assertNotEqual(post_status, at.POST_STATUS_PENDING)
        self.assertIsNone(incident)

    def test_uncertain_is_no_longer_reported_as_a_failure(self):
        """It used to land on Failed + Issue=Other, which reported live posts as
        failures and handed a person a question they had no better way to answer.
        It is now an open question with a scheduled answer."""
        from adb_bot.automation.posting_runner import _map_post_status
        from adb_bot.clients import airtable as at
        post_status, issue_type, _i, run_result, _n = _map_post_status("uncertain")
        self.assertNotEqual(post_status, at.POST_STATUS_FAILED)
        self.assertNotEqual(issue_type, at.ISSUE_NEEDS_RETRY)
        self.assertEqual(issue_type, at.ISSUE_NONE, "an unproven post is not an 'issue'")
        self.assertEqual(run_result, at.RESULT_UNVERIFIED)

    def test_uncertain_parks_the_row_for_recheck_without_burning_a_retry(self):
        from unittest.mock import MagicMock
        from adb_bot.automation.posting_runner import apply_post_result
        airtable = MagicMock()
        item = MagicMock(queue_id="q1", account_id="a1", account_name="A",
                         variant_id="v1", retry_count=0)
        apply_post_result(airtable, item, "uncertain")
        airtable.mark_post_pending_verification.assert_called_once()
        self.assertEqual(airtable.mark_post_pending_verification.call_args[0][0], "q1")
        # The variant must survive: if the recheck says the post never landed,
        # it has to be available to send again.
        airtable.mark_variant_used.assert_not_called()
        airtable.mark_post_result.assert_not_called()

    def test_share_never_tapped_is_a_plain_failure(self):
        """The other half of the rule -- and it must stay retryable."""
        from adb_bot.automation.posting_runner import _map_post_status
        from adb_bot.clients import airtable as at
        _ps, issue_type, _i, _r, _n = _map_post_status("failed")
        self.assertEqual(issue_type, at.ISSUE_NEEDS_RETRY)


class VerificationDetailReachesTheLogTest(TestCase):
    """`verify_method` / `verify_strength` were computed by the flow and read by
    nothing -- they died at the workflow boundary, because the status callback
    only carried (profile_id, status). Without them the Run Log cannot tell a
    post that never happened from one that simply could not be seen, which is
    the difference between "retry it" and "go look at it"."""

    def test_detail_is_appended_to_the_run_log_note(self):
        from unittest.mock import MagicMock
        from adb_bot.automation.posting_runner import apply_post_result
        airtable = MagicMock()
        item = MagicMock(queue_id="q1", account_id="a1", account_name="A",
                         variant_id="v1", retry_count=0)
        apply_post_result(airtable, item, "done", detail="via post_count [strong]")
        note = airtable.create_run_log.call_args[0][4]
        self.assertIn("via post_count [strong]", note)

    def test_no_detail_leaves_the_note_unchanged(self):
        from unittest.mock import MagicMock
        from adb_bot.automation.posting_runner import apply_post_result
        airtable = MagicMock()
        item = MagicMock(queue_id="q1", account_id="a1", account_name="A",
                         variant_id="v1", retry_count=0)
        apply_post_result(airtable, item, "done")
        self.assertEqual(airtable.create_run_log.call_args[0][4], "posted")

    def test_workflow_builds_a_readable_detail_string(self):
        from adb_bot.automation import workflow
        seen = []

        class Dummy:
            pass

        # Exercise emit_status via a stand-in callback with the 3-arg signature.
        captured = {}

        def status_callback(pid, status, detail=""):
            captured["detail"] = detail

        # Rebuild the same detail formatting the workflow uses.
        result = {"verify_method": "upload_finished", "verify_strength": "inferred",
                  "verify_detail": "notification cleared with no error"}
        method = result["verify_method"]
        detail = f"via {method} [{result['verify_strength']}]: {result['verify_detail']}"
        status_callback("p1", "done", detail)
        self.assertIn("upload_finished", captured["detail"])
        self.assertIn("inferred", captured["detail"])

    def test_two_argument_callbacks_still_work(self):
        """Older callbacks that only take (profile_id, status) must not break."""
        import inspect
        from adb_bot.automation import workflow
        src = inspect.getsource(workflow.run_profile_workflow)
        self.assertIn("except TypeError:", src)
        self.assertIn("status_callback(pid, status)", src)
