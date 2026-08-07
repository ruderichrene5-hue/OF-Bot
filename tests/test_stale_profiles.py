"""Profiles that are still trying to post and no longer landing anything.

The rule has to be sharp in both directions. Too loose and the first quiet
weekend flags the whole fleet, which trains everyone to ignore the flag; too
tight and an account that has silently stopped posting keeps looking healthy
because every individual row was retryable.
"""

import unittest
from types import SimpleNamespace

from adb_bot.automation import post_ledger, stale_profiles
from adb_bot.clients import airtable as at

NOW = 1_786_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR


def _profile(recid="recP1", name="Jil 1", launch_id="111", status="Active",
             needs_human=False):
    return {"record_id": recid, "name": name, "launch_id": launch_id,
            "status": status, "needs_human": needs_human, "flagged_at": None}


def _share(profile_id="111", status=post_ledger.STATUS_CONFIRMED, ago=HOUR):
    return SimpleNamespace(profile_id=profile_id, status=status,
                           shared_at=NOW - ago, resolved_at=NOW - ago)


def _row(status="Failed", ago=HOUR, profile="recP1"):
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(NOW - ago, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"id": "q1", "fields": {"Post Status": status, "Scheduled DateTime": when,
                                   "Target Profile": [profile]}}


def _find(profiles=None, rows=(), ledger=(), now=NOW, **kw):
    return stale_profiles.find_stale_profiles(
        profiles if profiles is not None else [_profile()], list(rows), list(ledger), now, **kw)


class QuietIsNotBrokenTest(unittest.TestCase):
    """The failure mode that would make this feature worthless."""

    def test_a_profile_with_no_attempts_is_never_flagged(self):
        """An empty queue is not a fault. Without this the first quiet weekend
        flags every account on the fleet and the flag stops meaning anything."""
        self.assertEqual(_find(), [])

    def test_an_old_success_alone_does_not_flag(self):
        self.assertEqual(_find(ledger=[_share(ago=5 * DAY)]), [])

    def test_an_attempt_older_than_the_window_does_not_count_as_trying(self):
        self.assertEqual(_find(rows=[_row(ago=10 * DAY)]), [])


class StalenessTest(unittest.TestCase):
    def test_failing_for_over_a_day_is_flagged(self):
        stale = _find(rows=[_row(ago=2 * HOUR)], ledger=[_share(ago=2 * DAY)])
        self.assertEqual([s.name for s in stale], ["Jil 1"])
        self.assertEqual(stale[0].failed, 1)

    def test_a_recent_success_clears_it_however_many_failures_follow(self):
        """A profile that landed a post two hours ago is working; the failures
        around it are the ordinary noise of a fleet this size."""
        rows = [_row(ago=HOUR), _row(ago=2 * HOUR)]
        self.assertEqual(_find(rows=rows, ledger=[_share(ago=2 * HOUR)]), [])

    def test_a_profile_that_has_never_landed_anything_is_flagged(self):
        stale = _find(rows=[_row(ago=HOUR)])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].last_success, 0.0)

    def test_an_unconfirmed_post_is_not_a_success(self):
        """Share tapped, reel never seen, ledger stuck on `shared` -- the single
        most common symptom of what this looks for. Counting it as landed would
        blind the check to its main case."""
        stale = _find(ledger=[_share(status=post_ledger.STATUS_SHARED, ago=2 * HOUR)])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].uncertain, 1)

    def test_a_disproved_post_counts_as_an_attempt_not_a_success(self):
        stale = _find(ledger=[_share(status=post_ledger.STATUS_DISPROVED, ago=2 * HOUR)])
        self.assertEqual(len(stale), 1)

    def test_a_verifying_row_counts_as_trying(self):
        self.assertEqual(len(_find(rows=[_row(status="Verifying", ago=HOUR)])), 1)

    def test_the_threshold_is_configurable(self):
        rows, ledger = [_row(ago=HOUR)], [_share(ago=3 * HOUR)]
        self.assertEqual(_find(rows=rows, ledger=ledger), [])
        self.assertEqual(len(_find(rows=rows, ledger=ledger, stale_after=2 * HOUR)), 1)


class GateOrderTest(unittest.TestCase):
    """Same gates the planners apply, and for the same reasons."""

    def test_a_parked_profile_is_not_failing(self):
        self.assertEqual(_find([_profile(status="Inactive")], rows=[_row()]), [])

    def test_an_already_flagged_profile_is_left_alone(self):
        """Re-flagging would overwrite the notes a person is working from, and
        clearing the checkbox has to stay their signal alone."""
        self.assertEqual(_find([_profile(needs_human=True)], rows=[_row()]), [])

    def test_a_profile_with_no_launch_id_is_not_its_own_fault(self):
        self.assertEqual(_find([_profile(launch_id=None)], rows=[_row()]), [])


class EvidenceTest(unittest.TestCase):
    def test_a_posted_row_counts_as_success_when_the_ledger_has_nothing(self):
        """A ledger entry only exists once Share was tapped. Airtable is the
        fallback so a pruned or missing ledger cannot park a working profile."""
        self.assertEqual(_find(rows=[_row(status="Posted", ago=HOUR), _row(ago=2 * HOUR)]), [])

    def test_a_launch_that_never_reached_share_is_still_an_attempt(self):
        """Every launch 500s, nothing is ever written to the ledger. Read from
        the ledger alone this profile looks idle rather than broken."""
        stale = _find(rows=[_row(ago=HOUR)], ledger=[])
        self.assertEqual(len(stale), 1)
        self.assertEqual((stale[0].failed, stale[0].uncertain), (1, 0))

    def test_the_longest_silent_profile_is_first(self):
        profiles = [_profile("recA", "Old", "111"), _profile("recB", "Never", "222")]
        rows = [_row(ago=HOUR, profile="recA"), _row(ago=HOUR, profile="recB")]
        stale = _find(profiles, rows=rows, ledger=[_share("111", ago=3 * DAY)])
        self.assertEqual([s.name for s in stale], ["Never", "Old"])


class NoteTest(unittest.TestCase):
    """The note is the whole of what the person opening Airtable is given."""

    def test_it_says_what_was_seen_and_what_to_do(self):
        note = _find(rows=[_row(ago=HOUR)], ledger=[_share(ago=2 * DAY)])[0].note(NOW)
        self.assertIn("48h", note)
        self.assertIn("1 failed", note)
        self.assertIn("by hand", note)
        self.assertIn("clear Needs Human Check", note)

    def test_a_profile_with_no_success_on_record_says_so_not_zero_hours(self):
        note = _find(rows=[_row(ago=HOUR)])[0].note(NOW)
        self.assertIn("no confirmed post on record", note)
        self.assertNotIn("0h", note)


class FlagStaleProfilesTest(unittest.TestCase):
    class Fake:
        def __init__(self, profiles, rows):
            self._profiles, self._rows, self.flagged = profiles, rows, []

        def posting_profiles(self): return self._profiles
        def list_queue_rows(self): return self._rows

        def flag_profile_for_human(self, record_id, reason, note):
            self.flagged.append((record_id, reason, note))
            return True

    class Ledger:
        def load(self): return {}

    def _run(self, **kw):
        fake = self.Fake([_profile()], [_row(ago=HOUR)])
        tally = stale_profiles.flag_stale_profiles(
            fake, ledger=self.Ledger(), now=NOW, **kw)
        return fake, tally

    def test_it_flags_with_its_own_reason(self):
        fake, tally = self._run()
        self.assertEqual(tally["flagged"], 1)
        self.assertEqual(fake.flagged[0][1], at.PROFILE_ISSUE_NO_SUCCESS)

    def test_a_dry_run_writes_nothing(self):
        fake, tally = self._run(dry_run=True)
        self.assertEqual(fake.flagged, [])
        self.assertEqual((tally["stale"], tally["flagged"]), (1, 0))

    def test_an_airtable_failure_is_reported_not_raised(self):
        class Broken:
            def posting_profiles(self): raise RuntimeError("429")
        tally = stale_profiles.flag_stale_profiles(Broken(), ledger=self.Ledger(), now=NOW)
        self.assertEqual(tally["errors"], 1)
        self.assertEqual(tally["flagged"], 0)

    def test_a_write_failure_does_not_abandon_the_rest(self):
        class Half(self.Fake):
            def flag_profile_for_human(inner, record_id, reason, note):
                if record_id == "recA":
                    raise RuntimeError("422")
                return super().flag_profile_for_human(record_id, reason, note)

        fake = Half([_profile("recA", "A", "111"), _profile("recB", "B", "222")],
                    [_row(ago=HOUR, profile="recA"), _row(ago=HOUR, profile="recB")])
        tally = stale_profiles.flag_stale_profiles(fake, ledger=self.Ledger(), now=NOW)
        self.assertEqual((tally["flagged"], tally["errors"]), (1, 1))

    def test_a_missing_ledger_does_not_stop_the_check(self):
        class NoLedger:
            def load(self): raise OSError("gone")

        fake = self.Fake([_profile()], [_row(ago=HOUR)])
        tally = stale_profiles.flag_stale_profiles(fake, ledger=NoLedger(), now=NOW)
        self.assertEqual(tally["flagged"], 1)


if __name__ == "__main__":
    unittest.main()
