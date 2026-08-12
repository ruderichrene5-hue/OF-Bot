"""The verification chain: naming each screen, and working through them in any order.

The point of the loop under test is that Instagram's screens arrive in no fixed
order, so most of these tests are the same chain shuffled.
"""

import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.flows.verification import (
    CHALLENGE_BANNED,
    CHALLENGE_CHOOSE_METHOD,
    CHALLENGE_CODE,
    CHALLENGE_CONSENT,
    CHALLENGE_IMAGE_CAPTCHA,
    CHALLENGE_NONE,
    CHALLENGE_PHONE,
    CHALLENGE_PHOTO,
    CHALLENGE_SIGNED_OUT,
    RESULT_BANNED,
    RESULT_NEEDS_HUMAN,
    RESULT_SIGNED_OUT,
    RESULT_SOLVED,
    RESULT_STUCK,
    classify_challenge,
    run_verification,
)
from adb_bot.automation.flows import verification as verification_module
from adb_bot.clients.sms.base import NumberOrder
from adb_bot.clients.sms.breaker import BreakerStore
from adb_bot.clients.sms.router import SmsRouter

# --- screens, roughly as Instagram words them --------------------------------
SCREEN_CHOOSE = "we need to confirm it's you. how do you want to get the code?"
SCREEN_PHONE = "enter your mobile number to get a confirmation code"
SCREEN_CODE = "enter the code we sent to +1 555 010 0001"
SCREEN_PHOTO = "we need a photo of yourself to confirm you're a real person"
SCREEN_CAPTCHA = "type the characters you see in the image below"
# Verbatim from a real healthy feed (`Jil 23`, 2026-08-11). Not a paraphrase,
# because the flow now requires *positive* evidence that Instagram is working
# before it will report success -- a made-up feed string would pass the tests
# and fail on a phone, which is exactly backwards.
SCREEN_FEED = ("reels tray container jil_456xx's story, 0 of 1, unseen. add to "
               "story your story for you home reels message search and explore "
               "profile")
SCREEN_BANNED = "your account has been suspended"
# The one screen here that is not paraphrased: this is the real text read off
# `Jil 2` on 2026-08-11, verbatim from its UI dump.
SCREEN_SIGNED_OUT = ("english (us) join instagram share what you're into with "
                     "the people who get you. get started i already have a "
                     "profile meta logo")


class ClassifyTest(TestCase):
    def test_each_screen_is_named(self):
        cases = [
            (SCREEN_CHOOSE, CHALLENGE_CHOOSE_METHOD),
            (SCREEN_PHONE, CHALLENGE_PHONE),
            (SCREEN_CODE, CHALLENGE_CODE),
            (SCREEN_PHOTO, CHALLENGE_PHOTO),
            (SCREEN_CAPTCHA, CHALLENGE_IMAGE_CAPTCHA),
            (SCREEN_FEED, CHALLENGE_NONE),
            (SCREEN_BANNED, CHALLENGE_BANNED),
        ]
        for text, expected in cases:
            self.assertEqual(classify_challenge(text), expected, text)

    def test_a_code_screen_quoting_a_number_is_not_a_phone_screen(self):
        """The regression this ordering exists for: the code screen repeats the
        number it texted, so a naive match asks for a second number and loses the
        one that is about to receive."""
        text = "enter the confirmation code we sent to your phone number +15550100001"
        self.assertEqual(classify_challenge(text), CHALLENGE_CODE)

    def test_a_suspended_account_beats_every_challenge_marker(self):
        text = "your account has been suspended. confirm your phone number to appeal"
        self.assertEqual(classify_challenge(text), CHALLENGE_BANNED)

    def test_empty_and_unknown_screens_are_none(self):
        self.assertEqual(classify_challenge(""), CHALLENGE_NONE)
        self.assertEqual(classify_challenge(None), CHALLENGE_NONE)
        self.assertEqual(classify_challenge("settings and privacy"), CHALLENGE_NONE)


# --- fakes --------------------------------------------------------------------
class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProvider:
    """Sells numbers; delivers a code only if one was configured."""

    def __init__(self, name, code=None):
        self.name = name
        self.code = code
        self.purchases = 0
        self.cancelled = []
        self.finished = []

    def purchase(self, service="instagram", country="US"):
        self.purchases += 1
        return NumberOrder(provider=self.name, order_id=f"{self.name}-{self.purchases}",
                           phone="15550100001", country=country,
                           country_code="1", national_number="5550100001")

    def poll_code(self, order):
        return self.code

    def cancel(self, order):
        self.cancelled.append(order.order_id)
        return True

    def finish(self, order):
        self.finished.append(order.order_id)
        return True

    def balance(self):
        return 10.0


class FakeDriver:
    """Walks a scripted list of screens; every successful action advances one."""

    def __init__(self, screens, can_upload=True, captcha_image="/tmp/fake.png"):
        self.screens = list(screens)
        self.can_upload = can_upload
        self.captcha_image = captcha_image
        self.actions = []

    def read_screen(self):
        return self.screens[0] if self.screens else SCREEN_FEED

    def _advance(self):
        if self.screens:
            self.screens.pop(0)
        return True

    def choose_sms_method(self):
        self.actions.append(("choose", None))
        return self._advance()

    def enter_phone(self, number):
        self.actions.append(("phone", number))
        return self._advance()

    def enter_code(self, code):
        self.actions.append(("code", code))
        return self._advance()

    def request_new_number(self):
        self.actions.append(("new_number", None))
        # Instagram drops back to the phone screen to accept another number.
        self.screens.insert(0, SCREEN_PHONE)
        return True

    def upload_photo(self):
        self.actions.append(("photo", None))
        return self._advance() if self.can_upload else False

    def capture_captcha_image(self):
        return self.captcha_image

    def enter_captcha(self, text):
        self.actions.append(("captcha", text))
        return self._advance()


class FakeSolver:
    name = "fake"

    def __init__(self, answer=None, answers=None):
        self.answer = answer
        self.answers = list(answers) if answers is not None else None
        self.calls = 0
        self.reported = 0

    def solve_text(self, image_path, hint=""):
        self.calls += 1
        if self.answers is not None:
            return self.answers.pop(0) if self.answers else None
        return self.answer

    def report_incorrect(self):
        self.reported += 1
        return True


class FlowTestCase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adbbot-verify-"))
        self.clock = FakeClock()

    def router(self, primary, secondary=None):
        providers = [primary] + ([secondary] if secondary else [])
        store = BreakerStore(path=self.tmp / "breaker.json", clock=self.clock.time)
        return SmsRouter(providers, store=store, clock=self.clock.time,
                         sleep=self.clock.sleep)

    def run_chain(self, screens, provider=None, solver=None, driver=None, **kwargs):
        provider = provider or FakeProvider("smspool", code="885485")
        driver = driver if driver is not None else FakeDriver(screens)
        # The fake clock, not the real one: every chain ends on a clear screen,
        # and `_confirm_clear` deliberately doubts that for fifteen seconds.
        # Left real, each case here would pay that in wall-clock.
        kwargs.setdefault("sleep", self.clock.sleep)
        kwargs.setdefault("clock", self.clock.time)
        router = self.router(provider)
        result = run_verification(driver, router,
                                  solver=solver or FakeSolver(), **kwargs)
        # Kept so a test can assert on what the breaker learned -- the router is
        # built in here, so callers have no other handle on it.
        self.breaker_failures = router.store.load().consecutive_failures
        return result, driver, provider


class OrderIndependenceTest(FlowTestCase):
    def test_phone_then_code_then_photo(self):
        result, driver, provider = self.run_chain(
            [SCREEN_PHONE, SCREEN_CODE, SCREEN_PHOTO, SCREEN_FEED])

        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual([a[0] for a in driver.actions], ["phone", "code", "photo"])
        self.assertTrue(result.code_received)
        self.assertEqual(provider.finished, ["smspool-1"])

    def test_photo_first_then_phone_and_code(self):
        """Same steps, different order -- no code change needed."""
        result, driver, _ = self.run_chain(
            [SCREEN_PHOTO, SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])

        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual([a[0] for a in driver.actions], ["photo", "phone", "code"])

    def test_chooser_first(self):
        result, driver, _ = self.run_chain(
            [SCREEN_CHOOSE, SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])

        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual([a[0] for a in driver.actions], ["choose", "phone", "code"])

    def test_the_typed_number_is_the_national_part(self):
        _, driver, _ = self.run_chain([SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])
        self.assertEqual(driver.actions[0], ("phone", "5550100001"))

    def test_a_clear_screen_is_already_solved(self):
        result, driver, _ = self.run_chain([SCREEN_FEED])
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(driver.actions, [])

    def test_a_banned_account_stops_immediately(self):
        result, driver, provider = self.run_chain([SCREEN_BANNED])
        self.assertEqual(result.status, RESULT_BANNED)
        self.assertEqual(provider.purchases, 0, "a banned account must not cost a number")


class RealScreenTest(FlowTestCase):
    """Classification checked against text read off real phones, not paraphrase.

    Two screens have been captured so far. Both are here verbatim so that a
    later edit to the marker lists cannot quietly stop recognising the only
    screens anyone has actually seen.
    """

    # `Blank (10)`, 2026-08-11 -- the first real challenge screen.
    PHONE = ("get support menu enter your mobile number enter your mobile "
             "number you'll need to confirm this mobile number with a code via "
             "sms or whatsapp. de +49 de +49 phone number we use phone numbers "
             "added here to help you log in, protect our community, accurately "
             "count people who use our services, and assist you in accessing "
             "instagram and opt-in programs, but not for purposes such as "
             "suggesting friends or providing ads. send code send code")

    # `Jil 23`, same evening -- a healthy feed on a profile carrying the tag.
    FEED = ("reels tray container jil_456xx's story, 0 of 1, unseen. add to "
            "story your story for you home reels message search and explore "
            "profile")

    # `Laila 3`, 2026-08-12 -- the first real image captcha, verbatim. The one
    # that mattered most to get wrong: Instagram words it "enter the code from
    # the image", which contains `_CODE_STRONG_MARKERS`' "enter the code", so
    # before its real markers existed this classified as the *SMS code* screen
    # and the loop would have waited 45 seconds for a text nobody asked for.
    CAPTCHA = ("get support menu confirm you're human confirm you're human "
               "can't read this text? hear this code or get a new code can't "
               "read this text? hear this code or get a new code hear this code "
               "get a new code enter the code from the image next next")

    def test_the_real_image_captcha_is_recognised(self):
        self.assertEqual(classify_challenge(self.CAPTCHA), CHALLENGE_IMAGE_CAPTCHA)

    def test_the_captcha_is_not_mistaken_for_the_sms_code_screen(self):
        """The exact regression: `enter the code from the image` is a captcha,
        and treating it as the code screen waits on an SMS that was never
        requested -- on a chain where no number has been rented at all."""
        self.assertNotEqual(classify_challenge(self.CAPTCHA), CHALLENGE_CODE)

    def test_the_real_sms_code_screen_still_wins_its_own_markers(self):
        """The other direction: adding captcha wording must not swallow the
        code screen, whose text also mentions codes throughout."""
        code = ("get support menu enter confirmation code enter the 6-digit "
                "confirmation code we sent via sms to +491787298035. it may "
                "take up to a minute for you to receive this code. 6-digit "
                "code request new code next next update mobile number")
        self.assertEqual(classify_challenge(code), CHALLENGE_CODE)

    def test_the_real_phone_challenge_is_recognised(self):
        self.assertEqual(classify_challenge(self.PHONE), CHALLENGE_PHONE)

    def test_the_real_phone_challenge_is_not_read_as_a_code_screen(self):
        """It says 'a code via SMS' -- which must not outvote 'enter your
        mobile number'. Reading this as a code screen would wait 45 seconds for
        an SMS nobody asked for."""
        self.assertNotEqual(classify_challenge(self.PHONE), CHALLENGE_CODE)

    # `Blank (13)`, same sweep -- the real code step.
    CODE = ("get support menu enter confirmation code enter the 6-digit "
            "confirmation code we sent via sms to +4967870390593. it may take "
            "up to a minute for you to receive this code. 6-digit code request "
            "new code next update mobile number")

    def test_a_healthy_feed_is_still_nothing(self):
        self.assertEqual(classify_challenge(self.FEED), CHALLENGE_NONE)

    def test_the_real_code_challenge_is_recognised(self):
        self.assertEqual(classify_challenge(self.CODE), CHALLENGE_CODE)

    def test_the_real_code_screen_is_not_read_as_a_phone_screen(self):
        """It says 'update mobile number' -- the weak phone marker 'mobile
        number' must not outvote 'enter confirmation code'. Reading this as a
        phone screen would abandon a number seconds from receiving and rent
        another."""
        self.assertNotEqual(classify_challenge(self.CODE), CHALLENGE_PHONE)

    def test_the_two_real_screens_are_told_apart(self):
        """The pair that motivated the strong/weak split, now on real text."""
        self.assertEqual(classify_challenge(self.PHONE), CHALLENGE_PHONE)
        self.assertEqual(classify_challenge(self.CODE), CHALLENGE_CODE)


class GermanNumbersTest(FlowTestCase):
    """Germany is the default, because the phones and the form are German.

    Approved 2026-08-11 after the real challenge screen turned out to have its
    country picker fixed at `DE +49`. The US pool is cheaper and more reliable
    ($0.42/71% vs $0.60/56% at SMSPool), but a US number under a +49 prefix is
    a different number and can never receive its code.
    """

    def test_the_default_country_is_germany(self):
        from adb_bot.clients.sms import base
        self.assertEqual(base.DEFAULT_COUNTRY, base.COUNTRY_DE)

    def test_both_providers_can_sell_a_german_number(self):
        from adb_bot.clients.sms import fivesim, smspool
        self.assertIn("DE", smspool._COUNTRY_IDS)
        self.assertIn("DE", fivesim._COUNTRIES)

    def test_a_german_number_is_split_for_the_form(self):
        """Instagram's box wants the national part only; +49 comes from the picker."""
        from adb_bot.clients.sms.fivesim import _split_number
        self.assertEqual(_split_number("4967870390593", "DE"),
                         ("49", "67870390593"))

    def test_a_us_number_still_splits(self):
        from adb_bot.clients.sms.fivesim import _split_number
        self.assertEqual(_split_number("15550100001", "US"), ("1", "5550100001"))

    def test_a_us_number_of_the_wrong_length_is_left_whole(self):
        """Better to type the full international number than a mangled one."""
        from adb_bot.clients.sms.fivesim import _split_number
        self.assertEqual(_split_number("1555", "US"), (None, None))

    def test_an_unmapped_country_is_left_whole(self):
        from adb_bot.clients.sms.fivesim import _split_number
        self.assertEqual(_split_number("33612345678", "FR"), (None, None))

    def test_a_number_that_does_not_match_its_country_is_left_whole(self):
        from adb_bot.clients.sms.fivesim import _split_number
        self.assertEqual(_split_number("15550100001", "DE"), (None, None))


class CountryPickerTest(FlowTestCase):
    """The picker beside the phone box decides what number is really submitted."""

    class PickerDriver(FakeDriver):
        def __init__(self, screens, code):
            super().__init__(screens)
            self._code = code

        def read_country_code(self):
            return self._code

    def _warnings(self, on_screen):
        logged = []

        class Logger:
            def info(self, message, *args):
                pass

            def warning(self, message, *args):
                logged.append(message % args if args else message)

        driver = self.PickerDriver([SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED],
                                   on_screen)
        provider = FakeProvider("smspool", code="885485")
        run_verification(driver, self.router(provider), solver=FakeSolver(),
                         logger=Logger(), sleep=self.clock.sleep,
                         clock=self.clock.time)
        return " | ".join(logged)

    def test_a_mismatched_picker_is_called_out(self):
        """A US number under a +49 prefix can never receive its code."""
        self.assertIn("country picker", self._warnings("49"))

    def test_a_matching_picker_says_nothing(self):
        self.assertNotIn("country picker", self._warnings("1"))

    def test_a_driver_without_the_method_still_runs(self):
        result, _driver, _provider = self.run_chain(
            [SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])
        self.assertEqual(result.status, RESULT_SOLVED)


class SignedOutTest(FlowTestCase):
    """A phone with nobody logged in must never be reported as solved.

    Not hypothetical: `Jil 2` -- flagged with the MultiLogin `Issue` tag, which
    is exactly the population this flow is meant to work through -- turned out
    to be signed out, showing Instagram's welcome screen (captured 2026-08-11,
    `~/.adb_bot/verification/Jil-2-20260811-221938/`). That screen carries no
    challenge marker, so before this was handled the loop read it as "nothing
    left to answer" and returned SOLVED. A runner acting on that would clear the
    `Issue` tag and hand a dead profile back to the posting loop.
    """

    def test_the_real_welcome_screen_is_recognised(self):
        self.assertEqual(classify_challenge(SCREEN_SIGNED_OUT),
                         CHALLENGE_SIGNED_OUT)

    def test_it_is_not_reported_as_solved(self):
        result, _driver, _provider = self.run_chain([SCREEN_SIGNED_OUT])
        self.assertEqual(result.status, RESULT_SIGNED_OUT)
        self.assertFalse(result.ok)

    def test_no_number_is_rented_for_a_signed_out_phone(self):
        result, _driver, provider = self.run_chain([SCREEN_SIGNED_OUT])
        self.assertEqual(result.numbers_used, 0)
        self.assertEqual(provider.purchases, 0)

    def test_a_real_challenge_still_wins_over_a_stray_password_link(self):
        """A code screen that also offers 'forgot password' is still a code screen."""
        self.assertEqual(
            classify_challenge(SCREEN_CODE + " forgot password"),
            CHALLENGE_CODE)


class RetryTest(FlowTestCase):
    def test_a_number_that_never_receives_is_replaced(self):
        """The fallback path end to end: dead primary, working secondary."""
        dead = FakeProvider("smspool", code=None)
        alive = FakeProvider("5sim", code="424242")
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])

        # The primary is already benched, so the lease comes from the secondary.
        router = self.router(dead, alive)
        with router.store.mutate() as state:
            state.cooldowns["smspool"] = self.clock.now + 1_800

        result = run_verification(driver, router, solver=FakeSolver(),
                                  sleep=self.clock.sleep, clock=self.clock.time)
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(alive.purchases, 1)
        self.assertEqual(dead.purchases, 0)

    def test_a_silent_number_is_refunded_and_another_is_tried(self):
        provider = FakeProvider("smspool", code=None)
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])
        result = run_verification(driver, self.router(provider),
                                  solver=FakeSolver(), max_number_attempts=2)

        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertEqual(result.numbers_used, 2)
        self.assertEqual(len(provider.cancelled), 2,
                         "every number that never received must be refunded")
        self.assertFalse(result.code_received)

    def test_the_run_gives_up_after_the_number_budget(self):
        provider = FakeProvider("smspool", code=None)
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])
        result = run_verification(driver, self.router(provider),
                                  solver=FakeSolver(), max_number_attempts=1)
        self.assertEqual(result.numbers_used, 1)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)


class UnsolvableTest(FlowTestCase):
    def test_an_image_captcha_without_a_solver_needs_a_human(self):
        result, _, provider = self.run_chain([SCREEN_CAPTCHA, SCREEN_FEED],
                                             solver=FakeSolver(answer=None))
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertIn("solver", result.detail)

    def test_an_image_captcha_is_solved_once_a_solver_exists(self):
        """The seam works: this is the only change a real solver has to make."""
        result, driver, _ = self.run_chain([SCREEN_CAPTCHA, SCREEN_FEED],
                                           solver=FakeSolver(answer="7F3KQ"))
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(driver.actions, [("captcha", "7F3KQ")])

    def test_a_rejected_captcha_answer_is_reported_and_retried(self):
        """The captcha screen coming back is the only sign an answer was wrong."""
        solver = FakeSolver(answers=["WR0NG", "R1GHT"])
        result, driver, _ = self.run_chain(
            [SCREEN_CAPTCHA, SCREEN_CAPTCHA, SCREEN_FEED], solver=solver)

        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(solver.reported, 1, "the bad solve must be reported back")
        self.assertEqual([a[1] for a in driver.actions], ["WR0NG", "R1GHT"])

    def test_the_first_captcha_answer_is_not_reported(self):
        solver = FakeSolver(answer="7F3KQ")
        self.run_chain([SCREEN_CAPTCHA, SCREEN_FEED], solver=solver)
        self.assertEqual(solver.reported, 0)

    def test_a_captcha_image_that_cannot_be_captured_needs_a_human(self):
        driver = FakeDriver([SCREEN_CAPTCHA, SCREEN_FEED], captcha_image=None)
        result = run_verification(driver, self.router(FakeProvider("smspool")),
                                  solver=FakeSolver(answer="7F3KQ"))
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_a_captcha_first_chain_runs_all_the_way_through(self):
        """The order the client reports seeing most: captcha, then phone, then code."""
        result, driver, _ = self.run_chain(
            [SCREEN_CAPTCHA, SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED],
            solver=FakeSolver(answer="7F3KQ"))
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual([a[0] for a in driver.actions],
                         ["captcha", "phone", "code"])

    def test_a_code_screen_with_no_number_of_ours_needs_a_human(self):
        result, _, provider = self.run_chain([SCREEN_CODE, SCREEN_FEED])
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertEqual(provider.purchases, 0)

    def test_a_photo_challenge_that_cannot_be_done_needs_a_human(self):
        driver = FakeDriver([SCREEN_PHOTO, SCREEN_FEED], can_upload=False)
        result = run_verification(driver, self.router(FakeProvider("smspool")),
                                  solver=FakeSolver())
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_a_screen_that_never_changes_is_reported_stuck(self):
        driver = FakeDriver([SCREEN_PHOTO])
        driver._advance = lambda: True          # the tap "works" but nothing moves
        result = run_verification(driver, self.router(FakeProvider("smspool")),
                                  solver=FakeSolver())
        self.assertEqual(result.status, RESULT_STUCK)


class LeakTest(FlowTestCase):
    def test_a_crash_mid_chain_still_returns_the_number(self):
        provider = FakeProvider("smspool", code="885485")
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])

        def boom(code):
            raise RuntimeError("device died")
        driver.enter_code = boom

        with self.assertRaises(RuntimeError):
            run_verification(driver, self.router(provider), solver=FakeSolver())
        self.assertEqual(provider.cancelled + provider.finished, ["smspool-1"],
                         "the rented number must be settled even on a crash")


class LateChallengeDriver(FakeDriver):
    """Shows a clean feed for the first N reads, then walks the scripted chain.

    The behaviour that matters on this fleet: Instagram opens on the feed and
    drops the challenge in a few seconds later, so the first read of a flagged
    account looks exactly like a healthy one.

    With `refresh_reveals`, the chain stays hidden until the feed is pulled
    down -- the case the second round of doubt exists for.
    """

    def __init__(self, screens=(), clean_reads=0, refresh_reveals=False):
        super().__init__(list(screens))
        self.clean_reads = clean_reads
        self.refresh_reveals = refresh_reveals
        self.reads = 0
        self.refreshes = 0

    def read_screen(self):
        self.reads += 1
        if self.refresh_reveals:
            return SCREEN_FEED if not self.refreshes else super().read_screen()
        return SCREEN_FEED if self.reads <= self.clean_reads else super().read_screen()

    def refresh_feed(self):
        self.refreshes += 1
        return True


class LateChallengeTest(FlowTestCase):
    """A clear screen is the one reading that must not be taken at face value.

    It is the reading that ends the run as *success*, and the account we were
    sent to look at is by definition one somebody flagged. Believing the first
    look would report "solved" on an account nobody ever really looked at --
    the same shape as the feed check that counted any Instagram activity as a
    healthy feed, and as `Blank (24)`, whose "we disabled your account" screen
    only appeared on the second look.
    """

    def test_a_challenge_arriving_after_the_first_read_is_still_worked(self):
        driver = LateChallengeDriver([SCREEN_PHONE, SCREEN_CODE], clean_reads=2)
        result, _, provider = self.run_chain(None, driver=driver)

        self.assertEqual([a[0] for a in driver.actions], ["phone", "code"])
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(provider.finished, ["smspool-1"],
                         "the number must be settled on a chain that started late")

    def test_a_genuinely_clear_screen_is_still_solved(self):
        """Patience must not turn a healthy profile into a failure -- a flagged
        account whose challenge is already gone is real and common."""
        driver = LateChallengeDriver([], clean_reads=99)
        result, _, _ = self.run_chain(None, driver=driver)

        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(driver.actions, [])

    def test_the_feed_is_pulled_down_before_a_clear_screen_is_believed(self):
        driver = LateChallengeDriver([], clean_reads=99)
        self.run_chain(None, driver=driver)
        self.assertEqual(driver.refreshes, 1)

    def test_a_challenge_that_only_the_refresh_reveals_is_worked(self):
        """The second round of doubt earning its place: Instagram served the
        challenge on the refresh when it withheld it on the open."""
        driver = LateChallengeDriver([SCREEN_PHONE, SCREEN_CODE],
                                     refresh_reveals=True)
        result, _, _ = self.run_chain(None, driver=driver)

        self.assertEqual([a[0] for a in driver.actions], ["phone", "code"])
        self.assertEqual(result.status, RESULT_SOLVED)

    def test_the_feed_is_not_pulled_down_mid_chain(self):
        """Once a step has been answered, a clear screen means that step
        worked. Swiping on a screen we have not recognised could dismiss it,
        and buys nothing patience has not already bought."""
        driver = LateChallengeDriver([SCREEN_PHONE, SCREEN_CODE], clean_reads=0)
        self.run_chain(None, driver=driver)
        self.assertEqual(driver.refreshes, 0)

    def test_a_driver_without_a_refresh_still_finishes(self):
        """`refresh_feed` is optional on the protocol; a driver lacking one
        must not turn a working run into an AttributeError."""
        driver = FakeDriver([SCREEN_FEED])
        self.assertFalse(hasattr(driver, "refresh_feed"))
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual(result.status, RESULT_SOLVED)


class BlankCaptchaImageDriver(FakeDriver):
    """A captcha screen whose image never renders.

    `capture_captcha_image` returns None for that (the driver refuses to hand a
    blank crop to a paid solver), and `request_new_captcha` is the screen's own
    way out. `renders_after` is how many refreshes it takes before a readable
    image appears -- None means never.
    """

    def __init__(self, screens, renders_after=None, can_refresh=True):
        super().__init__(screens)
        self.renders_after = renders_after
        self.can_refresh = can_refresh
        self.refreshes = 0

    def capture_captcha_image(self):
        if self.renders_after is not None and self.refreshes >= self.renders_after:
            return self.captcha_image
        return None

    def request_new_captcha(self):
        self.refreshes += 1
        self.actions.append(("new_captcha", None))
        return self.can_refresh


class BlankCaptchaFlowTest(FlowTestCase):
    """A captcha nobody could read must not cost a solve.

    Confirmed on `Laila 3`, 2026-08-12: the image node was present, correctly
    sized and pure white for 90 seconds. There is nothing to re-read in that
    state, so the only recovery is asking Instagram for a different image.
    """

    def test_a_blank_image_asks_for_a_new_one_instead_of_solving_it(self):
        driver = BlankCaptchaImageDriver([SCREEN_CAPTCHA, SCREEN_FEED],
                                         renders_after=1)
        solver = FakeSolver(answer="A7K2QX")
        result, _, _ = self.run_chain(None, driver=driver, solver=solver)

        self.assertEqual([a[0] for a in driver.actions], ["new_captcha", "captcha"])
        self.assertEqual(result.status, RESULT_SOLVED)

    def test_a_solve_is_never_spent_on_an_image_that_did_not_render(self):
        driver = BlankCaptchaImageDriver([SCREEN_CAPTCHA], renders_after=None)
        solver = FakeSolver(answer="A7K2QX")
        result, _, _ = self.run_chain(None, driver=driver, solver=solver)

        self.assertEqual(solver.calls, 0,
                         "2captcha must never be paid to read a blank rectangle")
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_it_gives_up_rather_than_refreshing_for_ever(self):
        """A phone that renders no images will render none of the replacements
        either, and each round also spends one of MAX_REPEATS."""
        driver = BlankCaptchaImageDriver([SCREEN_CAPTCHA], renders_after=None)
        result, _, _ = self.run_chain(None, driver=driver, solver=FakeSolver())

        self.assertEqual(driver.refreshes, verification_module.MAX_CAPTCHA_IMAGES)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertIn("not rendering", result.detail)

    def test_a_screen_with_no_new_image_link_needs_a_person_at_once(self):
        driver = BlankCaptchaImageDriver([SCREEN_CAPTCHA], renders_after=None,
                                         can_refresh=False)
        result, _, _ = self.run_chain(None, driver=driver, solver=FakeSolver())
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_a_driver_with_no_refresh_at_all_still_ends_cleanly(self):
        """`request_new_captcha` is optional on the protocol, like refresh_feed."""
        driver = FakeDriver([SCREEN_CAPTCHA])
        driver.capture_captcha_image = lambda: None
        self.assertFalse(hasattr(driver, "request_new_captcha"))
        result, _, _ = self.run_chain(None, driver=driver, solver=FakeSolver())
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)


class ClearScreenIsNotAutomaticallySuccessTest(FlowTestCase):
    """"No challenge marker" and "the account is fine" are different claims.

    Every screen in this class was recorded off a real phone under
    ~/.adb_bot/verification and classified as `none`, and every one of them
    would have been reported as **solved** -- untagging a profile nobody fixed
    and handing it back to the posting loop.
    """

    # `Jil 2`, 2026-08-11: Instagram was never in the foreground at all. The
    # probe watched the Android launcher for two minutes.
    LAUNCHER = ("search gallery gallery play store play store home telephone "
                "telephone messaging messaging music music chrome chrome "
                "camera camera")

    # `Jil 20`, 2026-08-11: Meta's ads-consent gate. Its only control is
    # `Get started`, and what lies behind it is a consent choice.
    CONSENT = ("choose if we process your data for ads choose if we process "
               "your data for ads as part of laws in your region, you can "
               "choose whether you consent to us processing your personal data "
               "for personalised ads on meta company products.")

    # `Laila 4` / `Laila 3`, 2026-08-12: what a *cleared* chain really ends on.
    BACK_ON_INSTAGRAM = ("get support menu you're back on instagram your account "
                         "is no longer suspended. what this means we reviewed "
                         "your account and found that it does follow our "
                         "community standards.")

    def _run_ending_on(self, screen):
        return self.run_chain([screen])[0]

    def test_a_healthy_feed_is_solved(self):
        self.assertEqual(self._run_ending_on(SCREEN_FEED).status, RESULT_SOLVED)

    def test_the_un_suspension_screen_is_solved(self):
        """The real end of a cleared chain, and it is not a feed."""
        self.assertEqual(self._run_ending_on(self.BACK_ON_INSTAGRAM).status,
                         RESULT_SOLVED)

    def test_the_android_launcher_is_never_solved(self):
        """Instagram was not even running. `Jil 2` sat here for two minutes and
        the run would have called the account fixed."""
        result = self._run_ending_on(self.LAUNCHER)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_the_ads_consent_gate_is_named_not_guessed_at(self):
        """A VA should be told which screen to clear, not handed raw text -- and
        the bot must not answer a consent question on the account's behalf."""
        result = self._run_ending_on(self.CONSENT)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertIn("consent", result.detail.lower())

    def test_an_unreadable_screen_is_never_solved(self):
        """`Jil 20`'s first look returned an empty string and classified as
        "nothing wrong". An unreadable screen is not a clear screen."""
        result = self._run_ending_on("")
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_an_unrecognised_screen_hands_over_its_text(self):
        """The only way the marker lists ever grow."""
        result = self._run_ending_on("some screen nobody has ever written down")
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertIn("some screen nobody has ever written down", result.detail)

    def test_a_solved_chain_still_solves_through_the_health_check(self):
        """The check must not break the ordinary path: a real chain that ends on
        a real feed is still a success."""
        result, driver, _ = self.run_chain(
            [SCREEN_CAPTCHA, SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED],
            solver=FakeSolver(answer="106653"))
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual([a[0] for a in driver.actions], ["captcha", "phone", "code"])


class ConfirmationDismissedTest(FlowTestCase):
    def test_the_confirmation_screen_is_acknowledged(self):
        driver = FakeDriver(["get support menu you're back on instagram your "
                             "account is no longer suspended."])
        tapped = []
        driver.dismiss_confirmation = lambda: (tapped.append(1), True)[1]
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(len(tapped), 1)

    def test_a_driver_without_one_still_solves(self):
        driver = FakeDriver([SCREEN_FEED])
        self.assertFalse(hasattr(driver, "dismiss_confirmation"))
        self.assertEqual(self.run_chain(None, driver=driver)[0].status, RESULT_SOLVED)

    def test_a_dismiss_that_raises_does_not_lose_the_solve(self):
        driver = FakeDriver([SCREEN_FEED])

        def boom():
            raise RuntimeError("phone went away")
        driver.dismiss_confirmation = boom
        self.assertEqual(self.run_chain(None, driver=driver)[0].status, RESULT_SOLVED)


class CaptchaAnsweredResetTest(FlowTestCase):
    """A captcha we answered is only "the last one" until something else comes up.

    Instagram does re-ask. Without the reset, a second captcha later in the
    chain reports the *earlier, correct* answer to 2captcha as wrong -- which
    refunds a solve we should have paid for and feeds the service bad accuracy
    data about its own solvers.
    """

    def test_a_later_captcha_does_not_blame_the_earlier_correct_one(self):
        solver = FakeSolver(answers=["106653", "884412"])
        result, _, _ = self.run_chain(
            [SCREEN_CAPTCHA, SCREEN_PHONE, SCREEN_CODE, SCREEN_CAPTCHA, SCREEN_FEED],
            solver=solver)
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(solver.reported, 0,
                         "the first captcha was accepted -- nothing to report")

    def test_a_genuinely_repeated_captcha_is_still_reported(self):
        solver = FakeSolver(answers=["wrong1", "right2"])
        self.run_chain([SCREEN_CAPTCHA, SCREEN_CAPTCHA, SCREEN_FEED], solver=solver)
        self.assertEqual(solver.reported, 1)


class RunBudgetTest(FlowTestCase):
    """One account cannot hold an unattended pass for ever.

    MAX_STEPS bounds how many screens are worked but not how long each takes:
    three numbers at 45s, captcha images at 20s and a clear screen doubted for
    15s add up, and a pass that goes on a timer has to be predictable.
    """

    def test_a_run_that_overruns_stops_as_stuck(self):
        """A phone that answers, but slowly. Reading the screen costs fake-clock
        time here the way a dump costs real time on a cloud phone."""
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE] * 6)
        slow_clock = self.clock

        def slow_read(_inner=driver.read_screen):
            slow_clock.sleep(120)
            return _inner()

        driver.read_screen = slow_read
        result, _, _ = self.run_chain(None, driver=driver, solver=FakeSolver(),
                                      max_seconds=300.0)
        self.assertEqual(result.status, RESULT_STUCK)
        self.assertIn("gave up after", result.detail)

    def test_the_budget_does_not_cut_short_an_ordinary_chain(self):
        result, driver, _ = self.run_chain(
            [SCREEN_CAPTCHA, SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED],
            solver=FakeSolver(answer="106653"))
        self.assertEqual(result.status, RESULT_SOLVED)

    def test_a_number_is_still_settled_when_the_budget_runs_out(self):
        """The one thing an expiring run must not do is walk away from a rented
        number: that is money gone and a provider failure nobody caused."""
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE] * 6)
        slow_clock = self.clock

        def slow_read(_inner=driver.read_screen):
            slow_clock.sleep(120)
            return _inner()

        driver.read_screen = slow_read
        provider = FakeProvider("smspool", code=None)
        result, _, provider = self.run_chain(None, driver=driver, provider=provider,
                                             max_seconds=300.0)
        self.assertEqual(result.status, RESULT_STUCK)
        self.assertEqual(len(provider.cancelled) + len(provider.finished),
                         provider.purchases,
                         "every rented number must be settled")


class InstagramRefusedTheNumberTest(FlowTestCase):
    """Instagram declining to send is not the SMS provider's fault.

    Read off `Jil 10`, 2026-08-12: the phone screen came back carrying
    "code not sent: try again later or use a different mobile number". The
    provider had delivered a perfectly good number; Instagram simply would not
    text it. Two of those in one run took the breaker from 4/10 to 6/10 --
    two-thirds of the way to switching providers over Instagram's behaviour.
    """

    REFUSED = ("get support menu enter your mobile number you'll need to confirm "
               "this mobile number with a code via sms or whatsapp. de +49 phone "
               "number code not sent: try again later or use a different mobile "
               "number. we use phone numbers added here to help you log in")

    def test_the_refusal_is_recognised(self):
        from adb_bot.automation.flows.verification import phone_number_refused
        self.assertTrue(phone_number_refused(self.REFUSED))

    def test_an_ordinary_phone_screen_is_not_a_refusal(self):
        from adb_bot.automation.flows.verification import phone_number_refused
        self.assertFalse(phone_number_refused(SCREEN_PHONE))

    def test_a_refusal_is_not_counted_against_the_provider(self):
        provider = FakeProvider("smspool", code=None)
        driver = FakeDriver([SCREEN_PHONE, self.REFUSED, self.REFUSED])
        result, _, provider = self.run_chain(None, driver=driver, provider=provider)

        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertEqual(len(provider.cancelled), provider.purchases,
                         "every number must still be given back")
        self.assertEqual(self.breaker_failures, 0,
                         "Instagram's refusal must not reach the breaker")

    def test_it_stops_rather_than_renting_a_third(self):
        """"Try again later" is about the account, not the numbers. Jil 10 paid
        for a third number purely to be told the same thing."""
        provider = FakeProvider("smspool", code=None)
        driver = FakeDriver([SCREEN_PHONE, self.REFUSED, self.REFUSED, self.REFUSED])
        result, _, provider = self.run_chain(None, driver=driver, provider=provider)

        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertIn("not accepting new numbers", result.detail)
        self.assertLessEqual(provider.purchases, 2,
                             "one retry is fair; a third is buying the same answer")

    def test_one_refusal_still_gets_another_number(self):
        """Instagram's own wording offers both readings, so a single refusal is
        worth one more number."""
        provider = FakeProvider("smspool", code="885485")
        driver = FakeDriver([SCREEN_PHONE, self.REFUSED, SCREEN_PHONE,
                             SCREEN_CODE, SCREEN_FEED])
        result, _, provider = self.run_chain(None, driver=driver, provider=provider)

        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertGreaterEqual(provider.purchases, 2)

    def test_a_genuine_timeout_is_still_the_providers_failure(self):
        """The distinction has to cut both ways, or the breaker stops working."""
        provider = FakeProvider("smspool", code=None)
        router = self.router(provider)
        driver = FakeDriver([SCREEN_PHONE, SCREEN_CODE])
        run_verification(driver, router, solver=FakeSolver(),
                         sleep=self.clock.sleep, clock=self.clock.time)
        self.assertGreaterEqual(router.store.load().consecutive_failures, 1)


class LoggedOutErrorDialogTest(FlowTestCase):
    """`Jil 3`, 2026-08-12 -- found by the safety net rather than by guessing.

    An in-app error dialog, not the signed-out welcome screen. It matched no
    marker at all, so before the positive-health check it would have been
    reported as SOLVED and would have untagged a logged-out account straight
    back into the posting loop. It stopped instead and handed over its text,
    which is what these markers were written from.
    """

    DIALOG = ("error you've been logged out of helenaisdaaa. the account owner "
              "may have changed the password. ok")

    def test_it_is_recognised_as_signed_out(self):
        self.assertEqual(classify_challenge(self.DIALOG), CHALLENGE_SIGNED_OUT)

    def test_the_run_says_it_needs_credentials_not_verification(self):
        result, _, provider = self.run_chain([self.DIALOG])
        self.assertEqual(result.status, RESULT_SIGNED_OUT)
        self.assertEqual(provider.purchases, 0,
                         "a logged-out phone must never cost a number")

    def test_a_healthy_feed_mentioning_a_password_is_not_signed_out(self):
        """The markers name the *event*, not the topic."""
        self.assertEqual(
            classify_challenge(SCREEN_FEED + " change password settings"),
            CHALLENGE_NONE)


class ConsentGateTest(FlowTestCase):
    """Meta's consent / onboarding gates, approved automatically.

    Left to a person until 2026-08-12, when they turned out to be ~20% of the
    flagged blanks and the owner decided: approve anything that costs nothing.
    The cost part is not incidental -- the ads-subscription screen has a paid
    option, and `interruptions` is what knows to pick the free one.
    """

    GATE = ("choose if we process your data for ads choose if we process your "
            "data for ads as part of laws in your region, you can choose whether "
            "you consent to us processing your personal data for personalised "
            "ads on meta company products. get started")

    SUBSCRIPTION = ("subscribe or continue using our products use free of charge "
                    "with ads subscribe for no ads continue")

    class ConsentDriver(FakeDriver):
        def __init__(self, screens, can_clear=True):
            super().__init__(screens)
            self.can_clear = can_clear
            self.cleared = 0

        def clear_blocking_prompts(self):
            self.cleared += 1
            self.actions.append(("consent", None))
            return self._advance() if self.can_clear else False

    def test_a_consent_gate_is_recognised(self):
        self.assertEqual(classify_challenge(self.GATE), CHALLENGE_CONSENT)

    def test_the_subscription_screen_is_recognised(self):
        self.assertEqual(classify_challenge(self.SUBSCRIPTION), CHALLENGE_CONSENT)

    def test_it_is_tapped_through_rather_than_handed_to_a_person(self):
        driver = self.ConsentDriver([self.GATE, SCREEN_FEED])
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(driver.cleared, 1)

    def test_a_gate_in_front_of_a_real_challenge_is_cleared_first(self):
        driver = self.ConsentDriver(
            [self.GATE, SCREEN_PHONE, SCREEN_CODE, SCREEN_FEED])
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual([a[0] for a in driver.actions],
                         ["consent", "phone", "code"])
        self.assertEqual(result.status, RESULT_SOLVED)

    def test_a_gate_that_will_not_clear_goes_to_a_person(self):
        driver = self.ConsentDriver([self.GATE], can_clear=False)
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)

    def test_a_gate_that_keeps_coming_back_stops(self):
        """Still on screen after being answered means it is not being answered."""
        driver = self.ConsentDriver([self.GATE])
        driver._advance = lambda: True          # never actually moves on
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)
        self.assertLessEqual(driver.cleared, 3)

    def test_a_real_challenge_always_outranks_a_consent_marker(self):
        """The consent words are broad; shadowing a phone or code screen with
        one would be far worse than the reverse."""
        mixed = SCREEN_PHONE + " " + self.GATE
        self.assertEqual(classify_challenge(mixed), CHALLENGE_PHONE)

    def test_a_driver_without_the_method_still_ends_cleanly(self):
        driver = FakeDriver([self.GATE])
        self.assertFalse(hasattr(driver, "clear_blocking_prompts"))
        result, _, _ = self.run_chain(None, driver=driver)
        self.assertEqual(result.status, RESULT_NEEDS_HUMAN)


class ConsentMarkersDoNotShadowAFeedTest(FlowTestCase):
    """A working Instagram is never a consent gate, whatever words are on it.

    `Blank (23)` came out of its consent chain onto a feed whose dump still
    carried "free of charge with ads". Read as a gate, that would have tapped
    at a healthy account until the run gave up on it -- turning a solve into a
    needs_human.
    """

    FEED_WITH_CONSENT_WORDS = (SCREEN_FEED + " to use our products free of "
                               "charge with ads sponsored")

    def test_a_feed_carrying_consent_words_is_not_a_gate(self):
        self.assertEqual(classify_challenge(self.FEED_WITH_CONSENT_WORDS),
                         CHALLENGE_NONE)

    def test_such_a_screen_still_solves(self):
        result, driver, _ = self.run_chain([self.FEED_WITH_CONSENT_WORDS])
        self.assertEqual(result.status, RESULT_SOLVED)
        self.assertEqual(driver.actions, [])

    def test_a_real_gate_is_still_a_gate(self):
        """The guard must not cost the feature it protects."""
        gate = ("choose if we process your data for ads as part of laws in your "
                "region, you can choose whether you consent to us processing "
                "your personal data get started")
        self.assertEqual(classify_challenge(gate), CHALLENGE_CONSENT)

    def test_a_real_challenge_on_a_feed_still_wins(self):
        """The qualification is only for consent -- every other marker names
        something the account is being asked, which a feed cannot show."""
        self.assertEqual(
            classify_challenge(SCREEN_FEED + " " + SCREEN_PHONE),
            CHALLENGE_PHONE)
