"""The provider fallback rules, checked against the specification.

Everything here runs on a fake clock and fake providers: the breaker is all
about 45-second waits and 30-minute cooldowns, and a test that actually waited
those out would never be run.
"""

import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.clients.sms.base import (
    InsufficientBalance,
    NoNumbersAvailable,
    NumberOrder,
    PROVIDER_5SIM,
    PROVIDER_SMSPOOL,
    SmsProviderError,
)
from adb_bot.clients.sms.breaker import (
    COOLDOWN_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    SWITCH_WARNING,
    BreakerStore,
)
from adb_bot.clients.sms.router import AllProvidersFailed, SmsRouter


class SmsPoolStatusTest(TestCase):
    """`/sms/check` answers, as SMSPool really returns them.

    Captured 2026-08-11 from a live German order (`BSWODSHR`) and from an order
    old enough that SMSPool had forgotten it.
    """

    class Session:
        def __init__(self, payload):
            self.payload = payload

        def post(self, url, data=None, timeout=None):
            class Response:
                status_code = 200
                text = ""

                def json(_self):
                    return self.payload

            return Response()

    def _provider(self, payload):
        from adb_bot.clients.sms.smspool import SmsPoolProvider
        return SmsPoolProvider("key", session=self.Session(payload))

    def _order(self):
        return NumberOrder(provider="smspool", order_id="BSWODSHR",
                           phone="491787129171", country="DE")

    def test_a_refunded_order_stops_the_poll(self):
        """Status 6 with 'This order has been refunded' -- the one documented."""
        provider = self._provider({"status": 6,
                                   "message": "This order has been refunded",
                                   "resend": 0, "time_left": 1123})
        with self.assertRaises(SmsProviderError):
            provider.poll_code(self._order())

    def test_a_forgotten_order_stops_the_poll(self):
        """No status field at all, just success=0.

        Without this it falls through to 'still pending' and the loop waits out
        the whole 45 seconds on an order that can never answer.
        """
        provider = self._provider({"success": 0,
                                   "message": "We could not find this order!"})
        with self.assertRaises(SmsProviderError):
            provider.poll_code(self._order())

    def test_a_pending_order_keeps_waiting(self):
        provider = self._provider({"status": 1, "time_left": 900})
        self.assertIsNone(provider.poll_code(self._order()))

    def test_an_unknown_status_keeps_waiting(self):
        """Biased toward waiting: a wrong guess must not discard a live number."""
        provider = self._provider({"status": 99, "time_left": 900})
        self.assertIsNone(provider.poll_code(self._order()))

    def test_a_delivered_code_beats_any_status(self):
        provider = self._provider({"status": 3, "sms": "473611"})
        self.assertEqual(provider.poll_code(self._order()), "473611")


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    """A provider that behaves exactly as the test tells it to."""

    def __init__(self, name, code=None, polls_until_code=1, can_sell=True,
                 sell_error=None):
        self.name = name
        self.code = code
        self.polls_until_code = polls_until_code
        self.can_sell = can_sell
        self.sell_error = sell_error
        self.purchases = 0
        self.polls = 0
        self.cancelled = []
        self.finished = []

    def purchase(self, service="instagram", country="US"):
        if not self.can_sell:
            raise (self.sell_error
                   or NoNumbersAvailable(self.name, "no numbers available"))
        self.purchases += 1
        return NumberOrder(provider=self.name,
                           order_id=f"{self.name}-{self.purchases}",
                           phone="15550100001", country=country,
                           country_code="1", national_number="5550100001")

    def poll_code(self, order):
        self.polls += 1
        if self.code and self.polls >= self.polls_until_code:
            return self.code
        return None

    def cancel(self, order):
        self.cancelled.append(order.order_id)
        return True

    def finish(self, order):
        self.finished.append(order.order_id)
        return True

    def balance(self):
        return 10.0


class RecordingLogger:
    def __init__(self):
        self.lines = []

    def _record(self, level, message, *args):
        self.lines.append((level, message % args if args else message))

    def info(self, message, *args):
        self._record("info", message, *args)

    def warning(self, message, *args):
        self._record("warning", message, *args)

    def error(self, message, *args):
        self._record("error", message, *args)

    def text(self):
        return "\n".join(line for _, line in self.lines)


class RouterTestCase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adbbot-sms-"))
        self.clock = FakeClock()
        self.logger = RecordingLogger()

    def build(self, primary, secondary):
        store = BreakerStore(path=self.tmp / "breaker.json", clock=self.clock.time)
        return SmsRouter([primary, secondary], store=store, logger=self.logger,
                         clock=self.clock.time, sleep=self.clock.sleep)

    def failures(self, router):
        return router.store.load().consecutive_failures


class SuccessPathTest(RouterTestCase):
    def test_code_is_returned_and_the_order_is_finished(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code="885485")
        router = self.build(primary, FakeProvider(PROVIDER_5SIM))

        with router.lease() as lease:
            self.assertEqual(lease.typed_number, "5550100001")
            self.assertEqual(lease.wait_for_code(), "885485")

        self.assertEqual(primary.finished, ["smspool-1"])
        self.assertEqual(primary.cancelled, [], "a used number must not be refunded")

    def test_a_received_code_resets_the_failure_counter(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code="123456")
        router = self.build(primary, FakeProvider(PROVIDER_5SIM))

        with router.store.mutate() as state:      # pretend 7 failures happened
            state.consecutive_failures = 7

        with router.lease() as lease:
            lease.wait_for_code()
        self.assertEqual(self.failures(router), 0)


class TimeoutPathTest(RouterTestCase):
    def test_no_code_within_45s_refunds_and_counts_one_failure(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)     # never delivers
        router = self.build(primary, FakeProvider(PROVIDER_5SIM))

        started = self.clock.now
        with router.lease() as lease:
            self.assertIsNone(lease.wait_for_code())

        self.assertEqual(self.clock.now - started, 45,
                         "the wait must last exactly the 45s budget")
        self.assertEqual(primary.cancelled, ["smspool-1"],
                         "a number that never received must be refunded")
        self.assertEqual(primary.finished, [])
        self.assertEqual(self.failures(router), 1)

    def test_an_unused_lease_is_refunded_when_the_block_exits(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code="123456")
        router = self.build(primary, FakeProvider(PROVIDER_5SIM))

        with router.lease():          # never waited on -- the flow gave up
            pass
        self.assertEqual(primary.cancelled, ["smspool-1"])


class SwitchTest(RouterTestCase):
    def burn(self, router, times):
        for _ in range(times):
            with router.lease() as lease:
                lease.wait_for_code()

    def test_ten_consecutive_failures_switch_to_the_secondary(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)
        secondary = FakeProvider(PROVIDER_5SIM, code="999111")
        router = self.build(primary, secondary)

        self.assertIs(router.active_provider(), primary)
        self.burn(router, MAX_CONSECUTIVE_FAILURES)

        self.assertIs(router.active_provider(), secondary,
                      "the 10th failure must switch the active provider")
        self.assertEqual(self.failures(router), 0,
                         "the counter resets when it trips")
        self.assertIn(SWITCH_WARNING, self.logger.text())

    def test_nine_failures_do_not_switch(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)
        router = self.build(primary, FakeProvider(PROVIDER_5SIM))

        self.burn(router, MAX_CONSECUTIVE_FAILURES - 1)
        self.assertIs(router.active_provider(), primary)
        self.assertEqual(self.failures(router), MAX_CONSECUTIVE_FAILURES - 1)

    def test_a_success_in_between_prevents_the_switch(self):
        """The counter is *consecutive* -- one code resets it."""
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)
        router = self.build(primary, FakeProvider(PROVIDER_5SIM))

        self.burn(router, 9)
        primary.code = "424242"          # the pool recovers for one request
        primary.polls = 0
        with router.lease() as lease:
            lease.wait_for_code()
        primary.code = None
        self.burn(router, 9)

        self.assertIs(router.active_provider(), primary,
                      "18 failures with a success in the middle is not 10 in a row")

    def test_the_primary_comes_back_after_the_cooldown(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)
        secondary = FakeProvider(PROVIDER_5SIM, code="999111")
        router = self.build(primary, secondary)

        self.burn(router, MAX_CONSECUTIVE_FAILURES)
        self.assertIs(router.active_provider(), secondary)

        self.clock.now += COOLDOWN_SECONDS - 1
        self.assertIs(router.active_provider(), secondary,
                      "still benched one second before the 30 minutes are up")

        self.clock.now += 2
        self.assertIs(router.active_provider(), primary,
                      "the primary is tried again once the cooldown expires")

    def test_every_provider_cooling_still_yields_one(self):
        """A degraded service must not become a stopped one."""
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)
        secondary = FakeProvider(PROVIDER_5SIM, code=None)
        router = self.build(primary, secondary)

        self.burn(router, MAX_CONSECUTIVE_FAILURES)      # benches the primary
        self.burn(router, MAX_CONSECUTIVE_FAILURES)      # benches the secondary
        self.assertIsNotNone(router.active_provider())


class PersistenceTest(RouterTestCase):
    def test_the_counter_survives_a_new_process(self):
        """Each loop is a separate systemd run; an in-memory counter never trips."""
        path = self.tmp / "breaker.json"
        primary = FakeProvider(PROVIDER_SMSPOOL, code=None)

        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            # A brand-new router each time, as if the loop had exited and rerun.
            router = SmsRouter([primary, FakeProvider(PROVIDER_5SIM)],
                               store=BreakerStore(path=path, clock=self.clock.time),
                               logger=self.logger,
                               clock=self.clock.time, sleep=self.clock.sleep)
            with router.lease() as lease:
                lease.wait_for_code()

        fresh = SmsRouter([primary, FakeProvider(PROVIDER_5SIM)],
                          store=BreakerStore(path=path, clock=self.clock.time),
                          logger=self.logger,
                          clock=self.clock.time, sleep=self.clock.sleep)
        self.assertEqual(fresh.store.load().consecutive_failures,
                         MAX_CONSECUTIVE_FAILURES - 1)

        with fresh.lease() as lease:            # the tenth, in yet another run
            lease.wait_for_code()
        self.assertEqual(fresh.active_provider().name, PROVIDER_5SIM)

    def test_a_corrupt_state_file_does_not_break_the_run(self):
        path = self.tmp / "breaker.json"
        path.write_text("{ this is not json", encoding="utf-8")
        store = BreakerStore(path=path, clock=self.clock.time)
        self.assertEqual(store.load().consecutive_failures, 0)


class PurchaseFailureTest(RouterTestCase):
    def test_a_provider_that_cannot_sell_falls_through_to_the_other(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, can_sell=False)
        secondary = FakeProvider(PROVIDER_5SIM, code="777777")
        router = self.build(primary, secondary)

        with router.lease() as lease:
            self.assertEqual(lease.provider.name, PROVIDER_5SIM)
            self.assertEqual(lease.wait_for_code(), "777777")

    def test_a_burned_pool_counts_toward_the_switch(self):
        primary = FakeProvider(PROVIDER_SMSPOOL, can_sell=False)
        secondary = FakeProvider(PROVIDER_5SIM, code="777777")
        router = self.build(primary, secondary)

        with router.lease() as lease:
            lease.wait_for_code()
        self.assertEqual(self.failures(router), 0,
                         "the secondary's success clears the primary's failure")

    def test_no_provider_can_sell(self):
        router = self.build(FakeProvider(PROVIDER_SMSPOOL, can_sell=False),
                            FakeProvider(PROVIDER_5SIM, can_sell=False))
        with self.assertRaises(AllProvidersFailed):
            router.lease()

    def test_an_empty_wallet_is_raised_not_counted(self):
        """Switching providers cannot fix an empty wallet -- it needs a person."""
        primary = FakeProvider(
            PROVIDER_SMSPOOL, can_sell=False,
            sell_error=InsufficientBalance(PROVIDER_SMSPOOL, "insufficient balance"))
        router = self.build(primary, FakeProvider(PROVIDER_5SIM, code="1"))

        with self.assertRaises(InsufficientBalance):
            router.lease()
        self.assertEqual(self.failures(router), 0)
