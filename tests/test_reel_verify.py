from unittest import TestCase

from adb_bot.automation.flows import reel_verify as rv
from adb_bot.automation.flows.reel_verify import (
    Count,
    NotificationState,
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
        self.assertIn("CONFIRMED via banner", result.summary())


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

    def test_count_probe_is_throttled(self):
        # Navigating to the profile is expensive; the probe must not do it on
        # every poll. Second immediate call returns None ("no opinion").
        calls = []

        class CountingFlow(type(self.flow)):
            def _open_profile_tab_u2(inner, d, target, logger=None):
                calls.append(1)
                return True

            def _read_post_count_u2(inner, d, target, logger=None):
                return rv.Count(7, True)

        flow = CountingFlow()
        probe = flow._profile_post_count_probe_u2(FakeDevice(), "t", None, None, every_seconds=999)
        first = probe()
        second = probe()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1)


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
