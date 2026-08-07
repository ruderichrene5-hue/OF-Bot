"""Getting a parked profile back on the air.

The two ways out of a park are deliberately different, and the dangerous
mistakes are symmetric: reactivating a profile a *person* parked by hand, and
auto-clearing a verification or ban flag. Both are covered here.
"""

from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock

from adb_bot.automation import recovery_runner as rr
from adb_bot.clients import airtable as at

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def profile(name="Nikki 1", flagged=True, reason=at.PROFILE_ISSUE_EXHAUSTED,
            status="Inactive", notes="", flagged_at=None, api_id="6249429167",
            record_id=None):
    fields = {at.F_PROF_NAME: name, at.F_PROF_NEEDS_HUMAN: flagged,
              at.F_PROF_MLX_API_ID: api_id}
    if reason is not None:
        fields[at.F_PROF_ISSUE_REASON] = reason
    if status is not None:
        fields[at.F_PROF_STATUS] = status
    if notes:
        fields[at.F_PROF_ISSUE_NOTES] = notes
    fields[at.F_PROF_FLAGGED_AT] = (
        flagged_at if flagged_at is not None else (NOW - timedelta(days=5)).isoformat())
    return {"id": record_id or f"rec{name.replace(' ', '')}", "fields": fields}


class PlanTest(TestCase):
    def plan(self, *records, now=NOW):
        return rr.plan_recovery(list(records), now=now)

    # --- a person cleared the box -------------------------------------------

    def test_an_unflagged_profile_is_reactivated(self):
        react, probe, report = self.plan(profile(flagged=False))
        self.assertEqual([e["name"] for e in react], ["Nikki 1"])
        self.assertEqual(probe, [])

    def test_a_human_parked_profile_is_left_alone(self):
        # Status Inactive, no Issue Reason, no Flagged At: somebody parked this
        # by hand and it is none of our business. Reactivating these would
        # un-park every deliberately-parked profile on the base, staging rows
        # included.
        react, probe, _ = self.plan(profile(flagged=False, reason=None, flagged_at=""))
        self.assertEqual(react, [])
        self.assertEqual(probe, [])

    def test_a_flagged_at_alone_is_still_our_fingerprint(self):
        # Somebody cleared the reason by hand but left the stamp: still a park
        # we made, so still ours to undo.
        react, _, _ = self.plan(profile(flagged=False, reason=None))
        self.assertEqual(len(react), 1)

    def test_old_issue_notes_do_not_make_a_profile_ours(self):
        # Notes survive an un-park on purpose. A profile a person parks long
        # after it was last flagged still carries them, and treating that as our
        # mark would override the person.
        react, _, _ = self.plan(profile(flagged=False, reason=None, flagged_at="",
                                        notes="[2026-01-01] Retries Exhausted: ancient history"))
        self.assertEqual(react, [])

    def test_an_unflagged_but_still_active_profile_is_tidied(self):
        # Already Active but still carrying a reason -- the write is only the
        # tidy-up, but leaving it would make the profile look bot-parked.
        react, _, _ = self.plan(profile(flagged=False, status="Active"))
        self.assertEqual(len(react), 1)

    def test_an_unflagged_verification_profile_is_also_reactivated(self):
        # The reason does not matter once a person has un-ticked the box: that
        # gesture IS the attestation. Only *automatic* clearing is restricted.
        react, _, _ = self.plan(profile(flagged=False, reason=at.PROFILE_ISSUE_VERIFICATION))
        self.assertEqual(len(react), 1)

    # --- the bot re-testing its own excuse ----------------------------------

    def test_an_exhausted_profile_is_probed(self):
        _, probe, _ = self.plan(profile(reason=at.PROFILE_ISSUE_EXHAUSTED))
        self.assertEqual([e["name"] for e in probe], ["Nikki 1"])

    def test_verification_and_ban_are_never_probed(self):
        _, probe, report = self.plan(
            profile(name="Laila 9", reason=at.PROFILE_ISSUE_VERIFICATION),
            profile(name="Viktoria 3", reason=at.PROFILE_ISSUE_BANNED),
        )
        self.assertEqual(probe, [])
        self.assertEqual(sorted(e["name"] for e in report.needs_person),
                         ["Laila 9", "Viktoria 3"])

    def test_an_unrecognised_reason_is_not_guessed_at(self):
        _, probe, report = self.plan(profile(reason="Something New"))
        self.assertEqual(probe, [])
        self.assertEqual(len(report.needs_person), 1)

    def test_a_profile_inside_its_backoff_waits(self):
        recent = (NOW - timedelta(hours=1)).isoformat()
        _, probe, report = self.plan(profile(flagged_at=recent))
        self.assertEqual(probe, [])
        self.assertEqual(len(report.waiting), 1)

    def test_the_backoff_grows_with_each_attempt(self):
        # One attempt already recorded -> the next waits 24h, not 6h.
        notes = f"[x] {rr.ATTEMPT_MARKER} 1 could not reach the phone"
        eight_hours = (NOW - timedelta(hours=8)).isoformat()
        _, probe, report = self.plan(profile(notes=notes, flagged_at=eight_hours))
        self.assertEqual(probe, [])
        self.assertEqual(len(report.waiting), 1)

        _, probe, _ = self.plan(profile(notes=notes,
                                        flagged_at=(NOW - timedelta(hours=25)).isoformat()))
        self.assertEqual(len(probe), 1)

    def test_a_profile_out_of_attempts_needs_a_person(self):
        notes = "\n".join(f"[x] {rr.ATTEMPT_MARKER} {n} could not reach the phone"
                          for n in (1, 2, 3))
        _, probe, report = self.plan(profile(notes=notes))
        self.assertEqual(probe, [])
        self.assertEqual(len(report.needs_person), 1)

    def test_a_missing_flagged_at_is_treated_as_due(self):
        # Records parked before the stamp existed must not be stranded forever.
        _, probe, _ = self.plan(profile(flagged_at=""))
        self.assertEqual(len(probe), 1)


class RecoverTest(TestCase):
    def setUp(self):
        self.airtable = Mock()
        self.airtable.clear_profile_flag.return_value = True
        self.airtable.note_on_profile.return_value = True

    def run_recovery(self, records, probe=None, dry_run=False):
        self.airtable.list_flagged_profiles.return_value = records
        return rr.recover_profiles(self.airtable, probe=probe, dry_run=dry_run, now=NOW)

    def test_reactivation_clears_the_flag(self):
        report = self.run_recovery([profile(flagged=False)])
        self.assertEqual(len(report.reactivated), 1)
        recid, note = self.airtable.clear_profile_flag.call_args[0]
        self.assertEqual(recid, "recNikki1")
        self.assertIn("cleared by a person", note)

    def test_a_probe_that_answers_un_parks_the_profile(self):
        report = self.run_recovery([profile()], probe=lambda launch_id, name: 42)
        self.assertEqual(len(report.recovered), 1)
        self.assertTrue(self.airtable.clear_profile_flag.called)

    def test_a_probe_that_fails_records_the_attempt_and_restamps(self):
        report = self.run_recovery([profile()], probe=lambda launch_id, name: None)
        self.assertEqual(len(report.still_down), 1)
        self.airtable.clear_profile_flag.assert_not_called()
        kwargs = self.airtable.note_on_profile.call_args.kwargs
        self.assertTrue(kwargs["restamp_flagged_at"],
                        "without a fresh stamp the next tick retries immediately")
        note = self.airtable.note_on_profile.call_args[0][1]
        self.assertIn(rr.ATTEMPT_MARKER, note)   # so the next pass can count it

    def test_a_probe_that_raises_is_a_failed_attempt_not_a_crash(self):
        def boom(launch_id, name):
            raise RuntimeError("phone on fire")
        report = self.run_recovery([profile()], probe=boom)
        self.assertEqual(len(report.still_down), 1)
        self.assertEqual(report.errors, [])

    def test_without_a_probe_no_phone_is_touched(self):
        report = self.run_recovery([profile(flagged=False), profile(name="Nikki 2")])
        self.assertEqual(len(report.reactivated), 1)
        self.assertEqual(len(report.waiting), 1)   # the probe candidate, not run
        self.assertTrue(self.airtable.clear_profile_flag.called)

    def test_dry_run_writes_nothing_and_launches_nothing(self):
        calls = []
        report = self.run_recovery([profile(flagged=False), profile(name="Nikki 2")],
                                   probe=lambda *a: calls.append(a), dry_run=True)
        self.assertEqual(len(report.reactivated), 1)
        self.assertEqual(calls, [])
        self.airtable.clear_profile_flag.assert_not_called()
        self.airtable.note_on_profile.assert_not_called()

    def test_a_failed_listing_is_reported_not_raised(self):
        self.airtable.list_flagged_profiles.side_effect = RuntimeError("airtable down")
        report = rr.recover_profiles(self.airtable, dry_run=False, now=NOW)
        self.assertEqual(len(report.errors), 1)

    def test_one_bad_write_does_not_stop_the_rest(self):
        self.airtable.clear_profile_flag.side_effect = [RuntimeError("nope"), True]
        report = self.run_recovery([profile(name="Nikki 1", flagged=False),
                                    profile(name="Nikki 2", flagged=False)])
        self.assertEqual(len(report.reactivated), 1)
        self.assertEqual(len(report.errors), 1)
