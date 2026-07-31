from unittest import TestCase

from adb_bot.automation.flows import waits


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class SpeedTest(TestCase):
    def tearDown(self):
        waits.set_speed(None)

    def test_profiles(self):
        waits.set_speed("fast")
        self.assertEqual(waits.speed_factor(), 0.5)
        waits.set_speed("slow")
        self.assertEqual(waits.speed_factor(), 1.5)
        waits.set_speed("normal")
        self.assertEqual(waits.speed_factor(), 1.0)

    def test_unknown_profile_falls_back_to_normal(self):
        waits.set_speed("turbo")
        self.assertEqual(waits.speed_factor(), 1.0)

    def test_raw_factor_accepted(self):
        waits.set_speed(0.25)
        self.assertEqual(waits.speed_factor(), 0.25)

    def test_scaled_never_reaches_zero(self):
        waits.set_speed(0.001)
        self.assertGreaterEqual(waits.scaled(3), waits.MIN_SLEEP_SECONDS)

    def test_scaled_zero_stays_zero(self):
        self.assertEqual(waits.scaled(0), 0.0)


class WaitForTest(TestCase):
    def test_returns_true_immediately_when_already_ready(self):
        clock = FakeClock()
        self.assertTrue(waits.wait_for(lambda: True, timeout=5, now=clock.now, sleep=clock.sleep))
        self.assertEqual(clock.t, 0.0)   # no time burned

    def test_returns_true_as_soon_as_ready(self):
        clock = FakeClock()
        calls = {"n": 0}

        def ready():
            calls["n"] += 1
            return calls["n"] >= 3      # ready on the 3rd poll

        self.assertTrue(waits.wait_for(ready, timeout=10, poll=0.25, now=clock.now, sleep=clock.sleep))
        self.assertAlmostEqual(clock.t, 0.5)   # 2 polls, not the full 10s

    def test_returns_false_on_timeout(self):
        clock = FakeClock()
        self.assertFalse(waits.wait_for(lambda: False, timeout=2, poll=0.5, now=clock.now, sleep=clock.sleep))
        self.assertGreaterEqual(clock.t, 2.0)

    def test_raising_predicate_is_treated_as_not_ready(self):
        clock = FakeClock()

        def boom():
            raise RuntimeError("dump failed")

        # A flaky dump must not abort the wait.
        self.assertFalse(waits.wait_for(boom, timeout=1, poll=0.5, now=clock.now, sleep=clock.sleep))

    def test_predicate_that_recovers_after_raising(self):
        clock = FakeClock()
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] < 2:
                raise RuntimeError("transient")
            return True

        self.assertTrue(waits.wait_for(flaky, timeout=5, poll=0.25, now=clock.now, sleep=clock.sleep))


class SettleTest(TestCase):
    def tearDown(self):
        waits.set_speed(None)

    def test_without_predicate_sleeps_the_full_time(self):
        clock = FakeClock()
        waits.set_speed("normal")
        confirmed = waits.settle(3, now=clock.now, sleep=clock.sleep)
        self.assertFalse(confirmed)
        self.assertAlmostEqual(clock.t, 3.0)

    def test_speed_profile_scales_the_fallback_sleep(self):
        clock = FakeClock()
        waits.set_speed("fast")
        waits.settle(4, now=clock.now, sleep=clock.sleep)
        self.assertAlmostEqual(clock.t, 2.0)

    def test_ready_screen_returns_early(self):
        clock = FakeClock()
        waits.set_speed("normal")
        confirmed = waits.settle(3, ready=lambda: True, now=clock.now, sleep=clock.sleep)
        self.assertTrue(confirmed)
        self.assertEqual(clock.t, 0.0)      # the whole point: no dead time

    def test_never_waits_less_than_the_old_sleep_when_not_ready(self):
        # The safety invariant: an unconfirmed step is no less patient than the
        # blind sleep it replaced.
        clock = FakeClock()
        waits.set_speed("normal")
        confirmed = waits.settle(3, ready=lambda: False, poll=0.5, now=clock.now, sleep=clock.sleep)
        self.assertFalse(confirmed)
        self.assertGreaterEqual(clock.t, 3.0)

    def test_explicit_timeout_overrides_the_ceiling(self):
        clock = FakeClock()
        waits.set_speed("normal")
        waits.settle(1, ready=lambda: False, timeout=5, poll=1, now=clock.now, sleep=clock.sleep)
        self.assertGreaterEqual(clock.t, 5.0)

    def test_logs_readiness(self):
        class Log:
            def __init__(self):
                self.lines = []

            def info(self, msg, *args):
                self.lines.append(msg % args if args else msg)

        log = Log()
        clock = FakeClock()
        waits.settle(2, ready=lambda: True, logger=log, what="composer", now=clock.now, sleep=clock.sleep)
        self.assertTrue(any("Ready: composer" in line for line in log.lines))


class FakeNode:
    def __init__(self, exists):
        self.exists = exists


class FakeDevice:
    def __init__(self, present_key=None, explode=False):
        self.present_key = present_key
        self.explode = explode

    def __call__(self, **kwargs):
        if self.explode:
            raise RuntimeError("u2 down")
        return FakeNode(self.present_key in kwargs if self.present_key else False)


class AnyExistsTest(TestCase):
    def test_matches_any_selector(self):
        d = FakeDevice(present_key="text")
        self.assertTrue(waits.any_exists(d, {"resourceId": "x"}, {"text": "Next"}))

    def test_no_match(self):
        d = FakeDevice(present_key="text")
        self.assertFalse(waits.any_exists(d, {"resourceId": "x"}))

    def test_device_errors_are_swallowed(self):
        self.assertFalse(waits.any_exists(FakeDevice(explode=True), {"text": "Next"}))

    def test_u2_ready_builds_a_predicate(self):
        predicate = waits.u2_ready(FakeDevice(present_key="text"), {"text": "Next"})
        self.assertTrue(predicate())
