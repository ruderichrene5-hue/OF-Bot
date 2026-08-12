"""Picking which flagged profiles to work, and what each result means.

Two things carry this module and neither needs a phone: which profiles a pass
picks up (getting it wrong spends real money on the same handful forever), and
what each result is allowed to write back (getting it wrong hands a broken
account to the posting loop).
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adb_bot.automation import verification_runner as vr
from adb_bot.automation.flows import verification

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


def _item(name, launch_id=None, tags=("Issue",), remark=""):
    # `launch_id=""` has to survive as empty -- it is a case under test, and an
    # `or` default would quietly hand it an id.
    return {"id": f"L{name}" if launch_id is None else launch_id,
            "serial_name": name, "tags": list(tags), "remark": remark}


class PlanTest(unittest.TestCase):
    def test_only_tagged_profiles_are_worked(self):
        plan = vr.plan_verification(
            [_item("a"), _item("b", tags=("Active / Posting",))], now=NOW)
        self.assertEqual([p.name for p in plan.to_run], ["a"])
        self.assertEqual(plan.flagged, 1)

    def test_a_profile_with_no_id_is_counted_but_not_run(self):
        """It cannot be launched, tagged or shut down -- but leaving it out of
        the count entirely would stop the numbers reconciling with MultiLogin."""
        plan = vr.plan_verification([_item("a", launch_id="")], now=NOW)
        self.assertEqual(plan.to_run, [])
        self.assertEqual(plan.flagged, 1)

    def test_profiles_remarked_verification_go_first(self):
        plan = vr.plan_verification(
            [_item("zeta"), _item("alpha", remark="needs human verification")],
            now=NOW)
        self.assertEqual([p.name for p in plan.to_run], ["alpha", "zeta"])

    def test_the_remark_is_a_priority_not_a_filter(self):
        """Only 10 of 70 tagged profiles mention verification and 34 say nothing,
        yet the sampled ones were mid-challenge regardless. Filtering on the
        remark would skip most of the real work."""
        plan = vr.plan_verification([_item("quiet"), _item("noisy", remark="verif")],
                                    now=NOW)
        self.assertEqual({p.name for p in plan.to_run}, {"quiet", "noisy"})

    def test_the_limit_caps_the_pass_and_reports_the_remainder(self):
        plan = vr.plan_verification([_item(str(n)) for n in range(9)],
                                    now=NOW, limit=3)
        self.assertEqual(len(plan.to_run), 3)
        self.assertEqual(plan.over_limit, 6)

    def test_a_recently_worked_profile_is_left_alone(self):
        attempts = {"La": {"at": (NOW - timedelta(hours=1)).isoformat(),
                           "result": "solved"}}
        plan = vr.plan_verification([_item("a")], attempts=attempts, now=NOW,
                                    cooloff_hours=6)
        self.assertEqual(plan.to_run, [])
        self.assertEqual(len(plan.cooling_off), 1)
        name, hours = plan.cooling_off[0]
        self.assertEqual(name, "a")
        self.assertAlmostEqual(hours, 5.0, places=1)

    def test_the_cooloff_expires(self):
        attempts = {"La": {"at": (NOW - timedelta(hours=7)).isoformat(),
                           "result": "solved"}}
        plan = vr.plan_verification([_item("a")], attempts=attempts, now=NOW,
                                    cooloff_hours=6)
        self.assertEqual([p.name for p in plan.to_run], ["a"])

    def test_a_failure_cools_off_the_same_as_a_success(self):
        """A profile that failed will usually fail the same way an hour later,
        and each retry is up to three rented numbers."""
        attempts = {"La": {"at": (NOW - timedelta(minutes=5)).isoformat(),
                           "result": "could not reach it over ADB"}}
        plan = vr.plan_verification([_item("a")], attempts=attempts, now=NOW)
        self.assertEqual(plan.to_run, [])

    def test_an_unparseable_stamp_does_not_bench_a_profile_for_ever(self):
        attempts = {"La": {"at": "whenever", "result": "solved"}}
        plan = vr.plan_verification([_item("a")], attempts=attempts, now=NOW)
        self.assertEqual([p.name for p in plan.to_run], ["a"])


class RouteTest(unittest.TestCase):
    """What each result is allowed to write back."""

    def test_solved_hands_the_profile_back(self):
        self.assertEqual(vr.route_result(verification.RESULT_SOLVED), (True, False))

    def test_banned_flags_and_keeps_the_tag(self):
        """Untagging would spend launches on a phone that can never post."""
        self.assertEqual(vr.route_result(verification.RESULT_BANNED), (False, True))

    def test_signed_out_changes_nothing(self):
        """The regression this result exists for: before it had its own name the
        flow called these solved, which would untag a logged-out profile and
        hand it straight back to the posting loop."""
        self.assertEqual(vr.route_result(verification.RESULT_SIGNED_OUT), (False, False))

    def test_no_other_result_hands_a_profile_back(self):
        for status in (verification.RESULT_NEEDS_HUMAN, verification.RESULT_STUCK,
                       verification.RESULT_FAILED):
            untag, flagged = vr.route_result(status)
            self.assertFalse(untag, status)
            self.assertFalse(flagged, status)


class AttemptLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adbbot-verify-runner-"))

    def test_it_round_trips(self):
        vr.save_attempts({"L1": {"at": NOW.isoformat(), "result": "solved"}},
                         app_dir=self.tmp)
        self.assertEqual(vr.load_attempts(self.tmp)["L1"]["result"], "solved")

    def test_a_missing_ledger_is_empty_not_an_error(self):
        self.assertEqual(vr.load_attempts(self.tmp / "nope"), {})

    def test_a_corrupt_ledger_costs_the_cooloff_not_the_pass(self):
        vr.ledger_path(self.tmp).write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(vr.load_attempts(self.tmp), {})


class DryRunTest(unittest.TestCase):
    """The money is behind `--apply`, not behind a config file."""

    class _Logger:
        def __init__(self):
            self.lines = []

        def info(self, message, *args):
            self.lines.append(message % args if args else message)

        warning = error = info

    def test_a_dry_run_rents_nothing_and_touches_no_phone(self):
        clients = object()      # any use of these would raise AttributeError
        report = vr.run_verification_pass(
            clients, None, None, self._Logger(), [_item("a"), _item("b")],
            dry_run=True, app_dir=tempfile.mkdtemp())

        self.assertTrue(report.dry_run)
        self.assertEqual([o.status for o in report.outcomes], ["would-run"] * 2)
        self.assertEqual(sum(o.numbers_used for o in report.outcomes), 0)

    def test_a_dry_run_does_not_write_a_cooloff(self):
        """Otherwise looking at the list would bench the whole fleet for six
        hours without having done anything."""
        app_dir = Path(tempfile.mkdtemp())
        vr.run_verification_pass(object(), None, None, self._Logger(),
                                 [_item("a")], dry_run=True, app_dir=app_dir)
        self.assertEqual(vr.load_attempts(app_dir), {})


class FleetLevelFailureTest(unittest.TestCase):
    """Knowing when to stop, rather than proving the same point five times.

    Every profile costs a launch and about four minutes. An empty wallet or a
    MultiLogin outage gives the same answer for every phone, so grinding
    through the whole limit spends half an hour to learn it twice.
    """

    # Not `_outcome`: `unittest.TestCase` already owns that name for its own
    # `_Outcome` bookkeeping, and shadowing it makes every call here fail with
    # "'_Outcome' object is not callable".
    def _failed(self, error="", detail="", status=""):
        return vr.ProfileOutcome(name="x", launch_id="L1", error=error,
                                 detail=detail, status=status)

    def test_an_empty_wallet_is_fleet_level(self):
        self.assertTrue(vr.is_fleet_level_failure(self._failed(
            status="needs_human",
            detail="could not rent a number: no provider has stock")))

    def test_multilogin_not_starting_phones_is_fleet_level(self):
        self.assertTrue(vr.is_fleet_level_failure(
            self._failed(error="never became ADB-ready")))
        self.assertTrue(vr.is_fleet_level_failure(
            self._failed(error="could not reach it over ADB")))

    def test_a_challenge_this_account_cannot_pass_is_not_fleet_level(self):
        """The next profile deserves its turn: this one is about this account."""
        self.assertFalse(vr.is_fleet_level_failure(self._failed(
            status="needs_human",
            detail="the photo challenge could not be completed")))

    def test_a_solve_is_not_a_failure_at_all(self):
        self.assertFalse(vr.is_fleet_level_failure(
            self._failed(status="solved", detail="no verification screen remaining")))

    def test_a_banned_account_is_not_fleet_level(self):
        self.assertFalse(vr.is_fleet_level_failure(
            self._failed(status="banned", detail="account is disabled, not verifiable")))


class UnmanagedProfileTest(unittest.TestCase):
    """MultiLogin profiles Airtable has never heard of.

    The staging ones -- "Default profile name (NN)" -- carry the `Issue` tag and
    launch fine, but have no Profiles (Cloning) row. `632451306307322212` is one.
    A ban on such a profile can be written nowhere, and the danger is that it is
    written nowhere *quietly*.
    """

    class _Logger:
        def __init__(self):
            self.warnings = []

        def info(self, message, *args):
            pass

        def warning(self, message, *args):
            self.warnings.append(message % args if args else message)

        error = warning

    def _planned(self):
        return vr.PlannedProfile(launch_id="L1", name="Default profile name (47)")

    def test_a_ban_with_no_airtable_row_is_announced_not_swallowed(self):
        logger = self._Logger()
        outcome = vr.ProfileOutcome(name="x", launch_id="L1",
                                    status=verification.RESULT_BANNED,
                                    detail="account is disabled")
        vr._write_back(None, None, logger, self._planned(), outcome, None)

        self.assertFalse(outcome.flagged_banned)
        self.assertTrue(any("BANNED" in w and "no Profiles" in w
                            for w in logger.warnings), logger.warnings)

    def test_a_ban_with_a_row_is_written(self):
        class FakeAirtable:
            def __init__(self):
                self.flagged = []

            def flag_profile_for_human(self, record_id, reason, note):
                self.flagged.append((record_id, reason))
                return True

        airtable = FakeAirtable()
        outcome = vr.ProfileOutcome(name="x", launch_id="L1",
                                    status=verification.RESULT_BANNED,
                                    detail="account is disabled")
        vr._write_back(airtable, None, self._Logger(), self._planned(), outcome,
                       {"record_id": "rec1"})

        self.assertTrue(outcome.flagged_banned)
        self.assertEqual(len(airtable.flagged), 1)


class ReadinessSettingsTest(unittest.TestCase):
    """How long a launched phone gets to answer over ADB.

    `prepare_profile_for_adb` ships with 2 attempts x 10s. These MultiLogin
    cloud phones take 50-65 seconds to cold-launch and MultiLogin 500s on the
    first try often enough to matter, so the shipped default gives up while the
    phone is still booting. On 2026-08-12 that failed `Blank (10)` after 49
    seconds as "never became ADB-ready" -- which `is_fleet_level_failure` reads
    as a fleet problem, so two in a row abort a whole pass over phones that
    were merely slow.
    """

    def test_the_runner_waits_longer_than_a_cold_launch_takes(self):
        budget = vr.READINESS_ATTEMPTS * vr.READINESS_WAIT_SECONDS
        self.assertGreaterEqual(
            budget, 90,
            "a cold MLX launch is 50-65s; the budget must clear it with room "
            "for a 500 on the first try")

    def test_the_readiness_settings_reach_the_launch_call(self):
        """The regression itself: the runner called `prepare_profile_for_adb`
        without these, silently inheriting the 20-second default."""
        import inspect
        source = inspect.getsource(vr._work_one)
        self.assertIn("max_attempts=readiness_attempts", source)
        self.assertIn("wait_seconds=readiness_wait", source)

    def test_a_slow_launch_is_read_as_a_fleet_problem(self):
        """Which is why the budget matters: this outcome aborts the pass."""
        self.assertTrue(vr.is_fleet_level_failure(
            vr.ProfileOutcome(name="x", launch_id="L1",
                              error="never became ADB-ready")))
