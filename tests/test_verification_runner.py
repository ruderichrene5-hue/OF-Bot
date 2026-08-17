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


class DiagnosedElsewhereTest(unittest.TestCase):
    """Tags a person wrote that already say verification will not help.

    On the live workspace these cover 14 of the 69 flagged profiles --
    `logged out` x10, `unable to verify` x3, `Banned / Dead` x1. Each one the
    runner works costs a two-minute launch, and sometimes a rented number, to
    rediscover what the tag already says.
    """

    def test_a_logged_out_profile_is_not_worked(self):
        plan = vr.plan_verification(
            [_item("jil 2", tags=("Issue", "logged out"))], now=NOW)
        self.assertEqual(plan.to_run, [])
        self.assertEqual(plan.diagnosed, [("jil 2", "logged out")])

    def test_unable_to_verify_is_not_worked(self):
        """Somebody already tried this one by hand and could not do it."""
        plan = vr.plan_verification(
            [_item("x", tags=("Issue", "unable to verify"))], now=NOW)
        self.assertEqual(plan.to_run, [])

    def test_a_dead_account_is_not_worked(self):
        plan = vr.plan_verification(
            [_item("x", tags=("Issue", "Banned / Dead"))], now=NOW)
        self.assertEqual(plan.to_run, [])

    def test_the_match_survives_hand_typed_casing(self):
        plan = vr.plan_verification(
            [_item("x", tags=("Issue", "Logged Out"))], now=NOW)
        self.assertEqual(plan.to_run, [])

    def test_ordinary_tags_do_not_skip_a_profile(self):
        """`Second Account`, `Task B` and the warm-up tags say nothing about
        whether a challenge can be answered."""
        plan = vr.plan_verification(
            [_item("a", tags=("Issue", "Second Account", "Task B", "Created"))],
            now=NOW)
        self.assertEqual([p.name for p in plan.to_run], ["a"])

    def test_the_check_can_be_overridden_for_a_stale_tag(self):
        plan = vr.plan_verification(
            [_item("x", tags=("Issue", "logged out"))], now=NOW,
            respect_diagnosis=False)
        self.assertEqual([p.name for p in plan.to_run], ["x"])


class TerminalOutcomeTest(unittest.TestCase):
    """Results that will read the same tomorrow do not deserve a six-hour retry.

    `Blank (10)` is what pays for this: it rented a number, received the code,
    and *then* hit a video-selfie request -- so every retry costs a launch and
    a number to reach the same wall.
    """

    # Not `_outcome` -- `unittest.TestCase` owns that name for its own
    # bookkeeping (see the note in FleetLevelFailureTest).
    def _result(self, status="needs_human", detail=""):
        return vr.ProfileOutcome(name="x", launch_id="L1", status=status,
                                 detail=detail)

    def test_the_video_selfie_is_terminal(self):
        self.assertTrue(vr.is_terminal_outcome(self._result(
            detail="the photo challenge could not be completed")))

    def test_a_code_for_someone_elses_number_is_terminal(self):
        self.assertTrue(vr.is_terminal_outcome(self._result(
            detail="a code was requested for a number the bot does not control")))

    def test_signed_out_and_banned_are_terminal(self):
        self.assertTrue(vr.is_terminal_outcome(
            self._result(status=verification.RESULT_SIGNED_OUT)))
        self.assertTrue(vr.is_terminal_outcome(
            self._result(status=verification.RESULT_BANNED)))

    def test_a_bad_sms_pool_is_not_terminal(self):
        """Pool luck varies by the hour -- this one is worth another go."""
        self.assertFalse(vr.is_terminal_outcome(self._result(
            detail="no code arrived for 3 numbers")))

    def test_an_unrecognised_screen_is_not_terminal(self):
        """It may be a marker gap, but it is cheap to look again and the screen
        may simply have moved on."""
        self.assertFalse(vr.is_terminal_outcome(self._result(
            detail="the screen shows no verification challenge, but does not "
                   "look like a working Instagram either")))

    def test_a_terminal_result_benches_a_profile_for_far_longer(self):
        attempts = {"La": {"at": (NOW - timedelta(hours=12)).isoformat(),
                           "result": "needs_human", "terminal": True}}
        plan = vr.plan_verification([_item("a")], attempts=attempts, now=NOW,
                                    cooloff_hours=6)
        self.assertEqual(plan.to_run, [], "12h later, a week-long bench holds")

    def test_a_transient_result_comes_back_after_the_short_cooloff(self):
        attempts = {"La": {"at": (NOW - timedelta(hours=12)).isoformat(),
                           "result": "needs_human", "terminal": False}}
        plan = vr.plan_verification([_item("a")], attempts=attempts, now=NOW,
                                    cooloff_hours=6)
        self.assertEqual([p.name for p in plan.to_run], ["a"])


class OnlyListTest(unittest.TestCase):
    """Working a named list instead of the whole flagged population."""

    def test_it_works_only_the_named_profiles(self):
        plan = vr.plan_verification(
            [_item("luisa 2"), _item("luisa 3"), _item("jil 1")],
            now=NOW, only=["luisa 2", "jil 1"], limit=10)
        self.assertEqual({p.name for p in plan.to_run}, {"luisa 2", "jil 1"})

    def test_names_match_regardless_of_casing_and_spacing(self):
        plan = vr.plan_verification([_item("Luisa 2")], now=NOW,
                                    only=["luisa  2"], limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["Luisa 2"])

    def test_an_explicit_name_overrides_the_issue_tag_filter(self):
        """Somebody asking for a profile by name has a reason; refusing because
        the tag is missing would just be unhelpful."""
        plan = vr.plan_verification([_item("luisa 9", tags=("Created",))],
                                    now=NOW, only=["luisa 9"], limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["luisa 9"])

    def test_an_explicit_name_does_not_override_a_persons_diagnosis(self):
        """That is another person's finding, not a filter."""
        plan = vr.plan_verification(
            [_item("jil 2", tags=("Issue", "logged out"))],
            now=NOW, only=["jil 2"], limit=10)
        self.assertEqual(plan.to_run, [])
        self.assertEqual(len(plan.diagnosed), 1)

    def test_a_name_that_matches_nothing_is_reported(self):
        plan = vr.plan_verification([_item("luisa 2")], now=NOW,
                                    only=["luisa 2", "nobody"], limit=10)
        self.assertEqual(len(plan.not_found), 1)
        self.assertIn("nobody", plan.not_found[0])

    def test_a_duplicated_name_is_refused_not_guessed(self):
        """Profile names on this workspace are not unique -- `Blank (10)`,
        `(11)` and `(13)` each name two different profiles."""
        plan = vr.plan_verification(
            [_item("blank (10)", launch_id="L1"),
             _item("blank (10)", launch_id="L2")],
            now=NOW, only=["blank (10)"], limit=10)
        self.assertTrue(any("matches 2" in m for m in plan.not_found),
                        plan.not_found)


class ConnectSettingsTest(unittest.TestCase):
    """The ADB connection is a separate failure from readiness.

    MultiLogin can report a phone ready while adb sits in `error: device
    offline`. `Luisa 3` (2026-08-12) did exactly that: readiness passed on
    attempt 4, then three connection attempts all found the device offline and
    the profile was lost. The probe has always used 5 attempts.
    """

    def test_the_runner_matches_the_probe(self):
        self.assertGreaterEqual(vr.CONNECT_ATTEMPTS, 5)

    def test_the_connect_settings_reach_the_call(self):
        import inspect
        source = inspect.getsource(vr._work_one)
        self.assertIn("max_attempts=CONNECT_ATTEMPTS", source)

    def test_an_unreachable_phone_says_which_failure_it_was(self):
        """"Never became ADB-ready" is a slow boot; this is a phone MultiLogin
        called ready that adb cannot use. Same abort behaviour, different fix."""
        import inspect
        source = inspect.getsource(vr._work_one)
        self.assertIn("adb never saw a usable device", source)

    def test_both_launch_failures_still_abort_the_pass(self):
        for error in ("never became ADB-ready",
                      "could not reach it over ADB (MultiLogin reported it "
                      "ready, but adb never saw a usable device)"):
            self.assertTrue(vr.is_fleet_level_failure(
                vr.ProfileOutcome(name="x", launch_id="L1", error=error)), error)


class MatchFilterTest(unittest.TestCase):
    """Working a family of profiles by name -- 'every blank with an Issue tag'.

    A substring rather than an exact name on purpose: the families here share a
    prefix and differ by a number, and several of those numbers name *two*
    profiles ('Blank (10)' and 'Blank (8)' each do), so exact-name selection
    could not address them at all.
    """

    def test_it_narrows_to_matching_names(self):
        plan = vr.plan_verification(
            [_item("Blank (6)"), _item("Blank (7)"), _item("Luisa 2")],
            now=NOW, match="blank", limit=10)
        self.assertEqual({p.name for p in plan.to_run}, {"Blank (6)", "Blank (7)"})

    def test_it_is_case_insensitive(self):
        plan = vr.plan_verification([_item("Blank (6)")], now=NOW,
                                    match="BLANK", limit=10)
        self.assertEqual(len(plan.to_run), 1)

    def test_it_reaches_profiles_whose_names_are_duplicated(self):
        """The case that makes a substring necessary."""
        plan = vr.plan_verification(
            [_item("Blank (10)", launch_id="L1"),
             _item("Blank (10)", launch_id="L2")],
            now=NOW, match="blank", limit=10)
        self.assertEqual({p.launch_id for p in plan.to_run}, {"L1", "L2"})

    def test_it_still_requires_the_issue_tag(self):
        """A filter on top of the population, not instead of it."""
        plan = vr.plan_verification(
            [_item("Blank (6)", tags=("Created",)), _item("Blank (7)")],
            now=NOW, match="blank", limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["Blank (7)"])

    def test_it_still_honours_a_persons_diagnosis(self):
        plan = vr.plan_verification(
            [_item("Blank (19)", tags=("Issue", "logged out")), _item("Blank (7)")],
            now=NOW, match="blank", limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["Blank (7)"])
        self.assertEqual(len(plan.diagnosed), 1)

    def test_it_still_honours_the_cooloff(self):
        attempts = {"LBlank (7)": {"at": (NOW - timedelta(hours=1)).isoformat(),
                                   "result": "needs_human"}}
        plan = vr.plan_verification([_item("Blank (7)"), _item("Blank (9)")],
                                    attempts=attempts, now=NOW, match="blank",
                                    limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["Blank (9)"])


class NotInstalledTest(unittest.TestCase):
    """A phone with no Instagram on it is not evidence about any other phone.

    `Blank (9)` (2026-08-12) reported as "Instagram would not open", which
    `is_fleet_level_failure` reads as a fleet problem -- so two unprovisioned
    phones in a row would abort a whole pass. The app simply was not there.
    """

    def test_a_missing_app_is_not_a_fleet_problem(self):
        self.assertFalse(vr.is_fleet_level_failure(vr.ProfileOutcome(
            name="x", launch_id="L1", status=verification.RESULT_NEEDS_HUMAN,
            detail="Instagram is not installed on this phone, so there is "
                   "nothing to verify -- it needs provisioning, not a "
                   "verification run")))

    def test_a_missing_app_is_terminal(self):
        """It will not install itself, so a six-hour retry is a wasted launch."""
        self.assertTrue(vr.is_terminal_outcome(vr.ProfileOutcome(
            name="x", launch_id="L1", status=verification.RESULT_NEEDS_HUMAN,
            detail="Instagram is not installed on this phone")))

    def test_a_genuine_failure_to_start_is_still_fleet_level(self):
        """The distinction has to cut both ways."""
        self.assertTrue(vr.is_fleet_level_failure(vr.ProfileOutcome(
            name="x", launch_id="L1", error="Instagram would not open")))

    def test_the_check_runs_before_the_start_attempt(self):
        import inspect
        source = inspect.getsource(vr._work_one)
        self.assertLess(source.index("instagram_installed"),
                        source.index("_open_instagram(target"))


class DiagnosisTagTest(unittest.TestCase):
    """Writing what a run found back where the VAs actually work.

    The runner learns things -- this account is logged out, this one is gone,
    this one wants a video selfie -- and until now they reached a log file and
    a local JSON nobody opens. Written back as a tag, the finding is visible in
    the workspace *and* skipped by the next pass, because
    DIAGNOSED_ELSEWHERE_TAGS already covers exactly these names.
    """

    class FakeTags:
        def __init__(self, known=("logged out", "unable to verify", "banned / dead")):
            self.known = {n.lower(): f"id-{n}" for n in known}
            self.assigned = []

        def tag_ids_by_name(self, refresh=False):
            return dict(self.known)

        def assign(self, profile_id, tag_ids):
            self.assigned.append((profile_id, tuple(tag_ids)))
            return True

    class _Logger:
        def info(self, *a):
            pass
        warning = error = info

    def _apply(self, outcome, tags=None):
        tags = tags if tags is not None else self.FakeTags()
        vr._record_diagnosis_tag(tags, self._Logger(),
                                 vr.PlannedProfile(launch_id="L1", name="x"),
                                 outcome)
        return tags

    def _outcome_for(self, status="needs_human", detail=""):
        return vr.ProfileOutcome(name="x", launch_id="L1", status=status,
                                 detail=detail)

    def test_a_signed_out_profile_is_tagged_logged_out(self):
        out = self._outcome_for(status=verification.RESULT_SIGNED_OUT)
        tags = self._apply(out)
        self.assertEqual(tags.assigned, [("L1", ("id-logged out",))])
        self.assertEqual(out.diagnosis_tag, "logged out")

    def test_a_banned_profile_is_tagged_dead(self):
        tags = self._apply(self._outcome_for(status=verification.RESULT_BANNED))
        self.assertEqual(tags.assigned, [("L1", ("id-banned / dead",))])

    def test_the_video_selfie_is_tagged_unable_to_verify(self):
        tags = self._apply(self._outcome_for(
            detail="the photo challenge could not be completed"))
        self.assertEqual(tags.assigned, [("L1", ("id-unable to verify",))])

    def test_a_bad_sms_hour_is_never_tagged(self):
        """That is weather, not a diagnosis, and a tag would hide the profile
        from every future pass over something that changes by the hour."""
        tags = self._apply(self._outcome_for(detail="no code arrived for 3 numbers"))
        self.assertEqual(tags.assigned, [])

    def test_a_solve_is_never_tagged(self):
        tags = self._apply(self._outcome_for(status=verification.RESULT_SOLVED))
        self.assertEqual(tags.assigned, [])

    def test_a_tag_the_workspace_lacks_is_skipped_not_invented(self):
        """This borrows the VAs' vocabulary; it does not get to extend it."""
        tags = self._apply(self._outcome_for(status=verification.RESULT_SIGNED_OUT),
                           tags=self.FakeTags(known=()))
        self.assertEqual(tags.assigned, [])

    def test_a_tagging_failure_never_breaks_the_pass(self):
        class Exploding(self.FakeTags):
            def assign(self, profile_id, tag_ids):
                raise RuntimeError("MLX said no")

        out = self._outcome_for(status=verification.RESULT_SIGNED_OUT)
        self._apply(out, tags=Exploding())
        self.assertEqual(out.diagnosis_tag, "")

    def test_every_tag_it_writes_is_one_the_next_pass_skips(self):
        """The loop only closes if these names match DIAGNOSED_ELSEWHERE_TAGS."""
        for _status, _marker, tag in vr._DIAGNOSIS_TAGS:
            self.assertIn(vr._wanted_key(tag), vr.DIAGNOSED_ELSEWHERE_TAGS, tag)


class OneLinePerProfileTest(unittest.TestCase):
    """Every profile the pass touches says what happened to it, once.

    `_work_one` settles several profiles before the screen loop ever runs --
    Instagram not installed, no usable device, never ADB-ready -- and each of
    those returns from its own place. While the result line lived inside
    `_work_one`, only the profiles that reached the chain got one: on
    2026-08-13 `Blank (9)` logged `launching` and then nothing at all, while
    quietly collecting a status, a detail and a seven-day bench.

    Checked against the source because the loop needs MultiLogin, a lock file
    and a phone to run, and a test that mocked all three would be asserting on
    the mocks.
    """

    def _pass_source(self):
        import inspect
        return inspect.getsource(vr.run_verification_pass)

    def test_the_result_line_is_not_left_to_the_chain(self):
        import inspect
        source = inspect.getsource(vr._work_one)
        self.assertNotIn('"verification pass: %s -> %s (%s)"', source)

    def test_the_pass_logs_a_settled_outcome(self):
        self.assertIn('"verification pass: %s -> %s (%s)"', self._pass_source())

    def test_the_pass_still_logs_an_infrastructure_failure(self):
        self.assertIn("could not be worked", self._pass_source())

    def test_an_outcome_with_neither_is_not_silent(self):
        """The bug was silence, so the unreachable branch is the point."""
        # Split across two source lines, so matched in halves.
        self.assertIn("finished with no ", self._pass_source())
        self.assertIn("outcome recorded", self._pass_source())

    def test_every_early_return_settles_something(self):
        """A return that sets neither status nor error would log nothing.

        Walks `_work_one`'s body: every `return` must be preceded, in its own
        branch, by an assignment to `outcome.status` or `outcome.error`.
        """
        import ast, inspect, textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(vr._work_one)))
        fn = tree.body[0]
        settled = 0
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr in ("status", "error")
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "outcome"):
                    settled += 1
        returns = sum(1 for node in ast.walk(fn) if isinstance(node, ast.Return))
        self.assertGreaterEqual(settled, returns,
                                "a return path in _work_one settles nothing, so "
                                "the pass has nothing to log for it")


class OnlyByIdTest(unittest.TestCase):
    """Asking for one profile when three share its name.

    The ambiguity warning has always ended "-- use the id", and until now there
    was no way to do that: `--only` matched names alone. On 2026-08-13 the
    three `Blank (13)` profiles made it concrete -- a re-run aimed at one of
    them planned all three, and pushed the two profiles actually wanted off the
    end of the limit.
    """

    def test_an_id_picks_exactly_one_of_a_duplicated_name(self):
        plan = vr.plan_verification(
            [_item("blank (13)", launch_id="L1"),
             _item("blank (13)", launch_id="L2"),
             _item("blank (13)", launch_id="L3")],
            now=NOW, only=["L2"], limit=10)
        self.assertEqual([p.launch_id for p in plan.to_run], ["L2"])
        self.assertEqual(plan.not_found, [])

    def test_ids_and_names_mix_in_one_list(self):
        plan = vr.plan_verification(
            [_item("blank (13)", launch_id="L1"),
             _item("blank (13)", launch_id="L2"),
             _item("jasmin 5", launch_id="L9")],
            now=NOW, only=["L1", "jasmin 5"], limit=10)
        self.assertEqual({p.launch_id for p in plan.to_run}, {"L1", "L9"})

    def test_an_id_that_matches_nothing_is_reported(self):
        plan = vr.plan_verification([_item("luisa 2", launch_id="L1")],
                                    now=NOW, only=["L404"], limit=10)
        self.assertEqual(plan.to_run, [])
        self.assertTrue(any("l404" in m.lower() for m in plan.not_found),
                        plan.not_found)

    def test_an_id_still_does_not_override_a_persons_diagnosis(self):
        plan = vr.plan_verification(
            [_item("jil 2", launch_id="L1", tags=("Issue", "logged out"))],
            now=NOW, only=["L1"], limit=10)
        self.assertEqual(plan.to_run, [])
        self.assertEqual(len(plan.diagnosed), 1)

    def test_a_name_that_is_also_wanted_by_id_is_not_counted_twice(self):
        """Both forms name the same profile; it is planned once."""
        plan = vr.plan_verification(
            [_item("luisa 2", launch_id="L1")],
            now=NOW, only=["L1", "luisa 2"], limit=10)
        self.assertEqual([p.launch_id for p in plan.to_run], ["L1"])
        # The name matched no profile of its own, which is worth saying rather
        # than hiding: it is how a typo in a mixed list would show up.
        self.assertTrue(any("luisa 2" in m for m in plan.not_found),
                        plan.not_found)


class ProtectedReasonTest(unittest.TestCase):
    """Profiles Airtable has already settled, which this pass must not overturn.

    The failure this guards is not "wasted a launch". A suspended account goes
    on rendering a cached feed, so its screen reads healthy, so the chain
    returns `solved`, so the `Issue` tag comes off and a dead account is handed
    back to the posting loop. That is what happened to `Laila 5` on 2026-08-13,
    and an unattended pass would repeat it on every profile in this state.
    """

    class _Logger:
        def __init__(self):
            self.warnings = []
            self.infos = []

        def info(self, message, *args):
            self.infos.append(message % args if args else message)

        def warning(self, message, *args):
            self.warnings.append(message % args if args else message)

        error = warning

    def test_a_banned_profile_is_not_planned(self):
        plan = vr.plan_verification(
            [_item("laila 5", launch_id="L1")], now=NOW, limit=10,
            reasons={"L1": "banned / blocked"})
        self.assertEqual(plan.to_run, [])
        self.assertEqual(plan.protected, [("laila 5", "banned / blocked")])

    def test_a_supervised_profile_is_not_planned(self):
        plan = vr.plan_verification(
            [_item("katja 2", launch_id="L1")], now=NOW, limit=10,
            reasons={"L1": "held for supervised run"})
        self.assertEqual(plan.to_run, [])
        self.assertEqual(len(plan.protected), 1)

    def test_naming_it_explicitly_does_not_get_past_the_guard(self):
        """`--only` overrides the tag filter. It must not override this."""
        plan = vr.plan_verification(
            [_item("laila 5", launch_id="L1", tags=())], now=NOW, limit=10,
            only=["L1"], reasons={"L1": "banned / blocked"})
        self.assertEqual(plan.to_run, [])
        self.assertEqual(len(plan.protected), 1)

    def test_ignore_diagnosis_does_not_get_past_the_guard_either(self):
        plan = vr.plan_verification(
            [_item("laila 5", launch_id="L1")], now=NOW, limit=10,
            respect_diagnosis=False, reasons={"L1": "banned / blocked"})
        self.assertEqual(plan.to_run, [])

    def test_an_ordinary_reason_is_still_worked(self):
        plan = vr.plan_verification(
            [_item("jil 5", launch_id="L1")], now=NOW, limit=10,
            reasons={"L1": "human verification required"})
        self.assertEqual([p.name for p in plan.to_run], ["jil 5"])
        self.assertEqual(plan.protected, [])

    def test_no_reasons_at_all_plans_exactly_as_before(self):
        """An Airtable outage degrades to the old behaviour, not to nothing."""
        plan = vr.plan_verification([_item("jil 5", launch_id="L1")],
                                    now=NOW, limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["jil 5"])

    def test_the_write_back_refuses_to_untag_a_protected_profile(self):
        """The second line of defence, independent of the planner."""
        class FakeTags:
            def __init__(self):
                self.unassigned = []

            def tag_ids_by_name(self):
                return {"issue": "T1"}

            def unassign(self, profile_id, tag_ids):
                self.unassigned.append(profile_id)
                return True

            def assign(self, profile_id, tag_ids):
                return True

        tags, logger = FakeTags(), self._Logger()
        outcome = vr.ProfileOutcome(name="laila 5", launch_id="L1",
                                    status=verification.RESULT_SOLVED,
                                    detail="no challenge on screen")
        vr._write_back(None, tags, logger,
                       vr.PlannedProfile(launch_id="L1", name="laila 5"),
                       outcome, {"record_id": "rec1", "reason": "Banned / Blocked"})

        self.assertEqual(tags.unassigned, [])
        self.assertFalse(outcome.untagged)
        self.assertTrue(any("NOT untagging" in w for w in logger.warnings),
                        logger.warnings)

    def test_reasons_by_launch_reads_an_overview(self):
        got = vr.reasons_by_launch([
            {"launch_id": "L1", "reason": "Banned / Blocked"},
            {"launch_id": "", "reason": "ignored -- no launch id"},
            {"reason": "also ignored"},
        ])
        self.assertEqual(got, {"L1": "banned / blocked"})


class DetectionPriorityTest(unittest.TestCase):
    """What a detector saw beats what a remark says.

    `Human Verification Required` in Airtable means something *observed*
    Instagram asking. The MultiLogin remark is hand-typed and says nothing at
    all on 34 of 70 tagged profiles, so ordering on it alone buries the
    profiles this flow exists to answer behind ones it cannot help.
    """

    def test_a_detected_profile_goes_first(self):
        plan = vr.plan_verification(
            [_item("zeta", launch_id="L1"), _item("alpha", launch_id="L2")],
            now=NOW, limit=10,
            reasons={"L1": "human verification required",
                     "L2": "no recent success"})
        self.assertEqual([p.name for p in plan.to_run], ["zeta", "alpha"])

    def test_detection_outranks_the_remark(self):
        plan = vr.plan_verification(
            [_item("zeta", launch_id="L1"),
             _item("alpha", launch_id="L2", remark="needs verification")],
            now=NOW, limit=10,
            reasons={"L1": "human verification required"})
        self.assertEqual([p.name for p in plan.to_run], ["zeta", "alpha"])

    def test_the_remark_still_orders_when_no_reason_is_known(self):
        plan = vr.plan_verification(
            [_item("zeta", launch_id="L1"),
             _item("alpha", launch_id="L2", remark="needs verification")],
            now=NOW, limit=10)
        self.assertEqual([p.name for p in plan.to_run], ["alpha", "zeta"])


class NumberCeilingTest(unittest.TestCase):
    """`--limit` bounds phones; `--max-numbers` bounds money.

    They are different bounds: one profile can spend three numbers, so a
    five-profile pass is a fifteen-number worst case -- more than this wallet
    has ever held.
    """

    class _Logger:
        def info(self, message, *args):
            pass

        warning = info
        error = info

    class _Clients:
        class _Launcher:
            def start_profiles(self, ids):
                pass

        class _Shutdown:
            def shutdown_profiles(self, ids):
                pass

        def __init__(self):
            self.launcher = self._Launcher()
            self.shutdown = self._Shutdown()
            self.api = None
            self.adb_enable = None

    def _run(self, items, max_numbers, numbers_each=3, max_per_day=0,
             ledger=None):
        worked = []

        def fake_work_one(clients, adb_client, logger, planned, outcome, country,
                          **kwargs):
            worked.append(planned.name)
            outcome.status = verification.RESULT_NEEDS_HUMAN
            outcome.detail = "no code arrived"
            outcome.numbers_used = numbers_each

        original = vr._work_one
        vr._work_one = fake_work_one
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if ledger is not None:
                    vr.save_attempts(ledger, Path(tmp))
                report = vr.run_verification_pass(
                    self._Clients(), None, None, self._Logger(), items,
                    dry_run=False, limit=10, app_dir=Path(tmp),
                    max_numbers=max_numbers, max_per_day=max_per_day)
        finally:
            vr._work_one = original
        return worked, report

    def test_the_pass_stops_once_the_ceiling_is_reached(self):
        worked, report = self._run(
            [_item("a", launch_id="L1"), _item("b", launch_id="L2"),
             _item("c", launch_id="L3")], max_numbers=4)

        # Two profiles at three numbers each: the first starts having spent
        # nothing, the second starts at 3 (under 4), the third is refused at 6.
        self.assertEqual(worked, ["a", "b"])
        self.assertTrue(report.stopped, report.summary())
        # A ceiling is not a fleet failure -- the unit must not go red for it.
        self.assertEqual(report.aborted, "")

    def test_no_ceiling_when_it_is_zero(self):
        worked, report = self._run(
            [_item("a", launch_id="L1"), _item("b", launch_id="L2")],
            max_numbers=0)
        self.assertEqual(worked, ["a", "b"])
        self.assertEqual(report.stopped, "")

    def test_the_pass_records_what_each_profile_cost(self):
        """The daily ceiling can only count what the ledger writes down."""
        with tempfile.TemporaryDirectory() as tmp:
            def fake_work_one(clients, adb_client, logger, planned, outcome,
                              country, **kwargs):
                outcome.status = verification.RESULT_NEEDS_HUMAN
                outcome.numbers_used = 2

            original = vr._work_one
            vr._work_one = fake_work_one
            try:
                vr.run_verification_pass(
                    self._Clients(), None, None, self._Logger(),
                    [_item("a", launch_id="L1")], dry_run=False, limit=10,
                    app_dir=Path(tmp), max_per_day=0)
            finally:
                vr._work_one = original

            self.assertEqual(vr.load_attempts(Path(tmp))["L1"]["numbers"], 2)


class DailyCeilingTest(unittest.TestCase):
    """The ceiling that actually bounds a timer.

    `--max-numbers` bounds one tick, and a tick is not the unit anybody spends
    in: a 30-minute timer at six numbers a tick is a 288-number day against a
    wallet that has never held more than ten dollars.
    """

    def test_yesterdays_spend_does_not_count(self):
        old = (NOW - timedelta(hours=30)).isoformat()
        self.assertEqual(
            vr.numbers_spent_since({"L1": {"at": old, "numbers": 9}},
                                   NOW - timedelta(hours=24)),
            0)

    def test_todays_spend_counts(self):
        recent = (NOW - timedelta(hours=2)).isoformat()
        self.assertEqual(
            vr.numbers_spent_since({"L1": {"at": recent, "numbers": 3},
                                    "L2": {"at": recent, "numbers": 2}},
                                   NOW - timedelta(hours=24)),
            5)

    def test_an_entry_from_before_this_was_recorded_counts_as_nothing(self):
        recent = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(
            vr.numbers_spent_since({"L1": {"at": recent, "result": "solved"}},
                                   NOW - timedelta(hours=24)),
            0)

    def test_junk_in_the_ledger_does_not_raise(self):
        recent = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(
            vr.numbers_spent_since({"L1": {"at": recent, "numbers": "three"},
                                    "L2": {"at": "not a date", "numbers": 5},
                                    "L3": None},
                                   NOW - timedelta(hours=24)),
            0)

    def test_a_pass_refuses_to_start_work_once_the_day_is_spent(self):
        """Recent spend on other profiles stops this pass before it launches."""
        ceiling = NumberCeilingTest()
        recent = (vr._now() - timedelta(hours=1)).isoformat()
        worked, report = ceiling._run(
            [_item("a", launch_id="L1")], max_numbers=0, max_per_day=6,
            ledger={"L9": {"at": recent, "numbers": 6, "result": "needs_human"}})

        self.assertEqual(worked, [])
        self.assertIn("daily ceiling", report.stopped)
        self.assertEqual(report.aborted, "")
