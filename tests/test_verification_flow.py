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
from adb_bot.clients.sms.base import NumberOrder
from adb_bot.clients.sms.breaker import BreakerStore
from adb_bot.clients.sms.router import SmsRouter

# --- screens, roughly as Instagram words them --------------------------------
SCREEN_CHOOSE = "we need to confirm it's you. how do you want to get the code?"
SCREEN_PHONE = "enter your mobile number to get a confirmation code"
SCREEN_CODE = "enter the code we sent to +1 555 010 0001"
SCREEN_PHOTO = "we need a photo of yourself to confirm you're a real person"
SCREEN_CAPTCHA = "type the characters you see in the image below"
SCREEN_FEED = "your story  reels  suggested for you  liked by"
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

    def run_chain(self, screens, provider=None, solver=None, **kwargs):
        provider = provider or FakeProvider("smspool", code="885485")
        driver = FakeDriver(screens)
        result = run_verification(driver, self.router(provider),
                                  solver=solver or FakeSolver(), **kwargs)
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
                         logger=Logger())
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

        result = run_verification(driver, router, solver=FakeSolver())
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
