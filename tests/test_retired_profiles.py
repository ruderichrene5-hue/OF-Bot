"""Profiles whose MultiLogin phone no longer exists.

`Issue Reason = Profile Deleted From MLX` means the MLX profile is gone, so no
loop can launch, post or recover the phone. Two things have to hold for that to
stay true: the label must survive the loops that keep re-flagging the profile
(the queue rows it left behind go on failing), and the dashboard must drop it
from the tabs people work off rather than showing impossible work.
"""

from unittest import TestCase
from unittest.mock import Mock, patch

from adb_bot.automation import report, report_html
from adb_bot.clients import airtable as at
from adb_bot.clients.airtable import AirtableClient


def profile(name="Jil 10", reason=at.PROFILE_ISSUE_DELETED, **extra):
    row = {"name": name, "reason": reason, "status": "Inactive",
           "needs_human": False, "serial": "", "launch_id": "1", "handoff": {},
           "queue_rows": 0, "accounts": 0}
    row.update(extra)
    return row


class ProfileRetiredTest(TestCase):
    def test_the_deleted_reason_retires_a_profile(self):
        self.assertTrue(report.profile_retired(profile()))

    def test_any_other_reason_does_not(self):
        # Every other reason describes an account that still has a phone to fix.
        for reason in (at.PROFILE_ISSUE_VERIFICATION, at.PROFILE_ISSUE_BANNED,
                       at.PROFILE_ISSUE_UNREACHABLE, at.PROFILE_ISSUE_NO_SUCCESS, ""):
            self.assertFalse(report.profile_retired(profile(reason=reason)), reason)

    def test_whitespace_and_missing_rows_are_tolerated(self):
        self.assertTrue(report.profile_retired(
            profile(reason=f"  {at.PROFILE_ISSUE_DELETED}  ")))
        self.assertFalse(report.profile_retired({}))
        self.assertFalse(report.profile_retired(None))

    def test_drop_retired_splits_and_names(self):
        kept, retired = report.drop_retired([
            profile(name="Jil 10"),
            profile(name="Katja 6", reason=""),
            profile(name="Jasmin 5"),
        ])
        self.assertEqual([p["name"] for p in kept], ["Katja 6"])
        self.assertEqual(retired, ["Jasmin 5", "Jil 10"])

    def test_drop_retired_handles_nothing(self):
        self.assertEqual(report.drop_retired([]), ([], []))
        self.assertEqual(report.drop_retired(None), ([], []))


class NeedsHumanDropsRetiredTest(TestCase):
    """The VA worklist is the tab this mattered most on.

    A retired profile stays *ticked* in Airtable on purpose -- the flag protects
    the diagnosis written into Issue Notes -- so the worklist has to filter it
    out itself rather than by clearing the box.
    """

    def _needs_human(self, records):
        airtable = Mock()
        airtable.list_failed_posts.return_value = []
        airtable._list_table.return_value = records
        return report.needs_human(airtable)

    def test_a_retired_profile_is_off_the_worklist_but_still_counted(self):
        out = self._needs_human([
            {"id": "rec1", "fields": {at.F_PROF_NAME: "Jil 10",
                                      at.F_PROF_ISSUE_REASON: at.PROFILE_ISSUE_DELETED,
                                      at.F_PROF_NEEDS_HUMAN: True}},
            {"id": "rec2", "fields": {at.F_PROF_NAME: "Luisa 2",
                                      at.F_PROF_ISSUE_REASON: at.PROFILE_ISSUE_VERIFICATION,
                                      at.F_PROF_NEEDS_HUMAN: True}},
        ])
        self.assertEqual([p["name"] for p in out["profiles"]], ["Luisa 2"])
        self.assertEqual(out["retired"], ["Jil 10"])

    def test_nothing_retired_leaves_the_worklist_alone(self):
        out = self._needs_human([
            {"id": "rec2", "fields": {at.F_PROF_NAME: "Luisa 2",
                                      at.F_PROF_ISSUE_REASON: at.PROFILE_ISSUE_VERIFICATION,
                                      at.F_PROF_NEEDS_HUMAN: True}},
        ])
        self.assertEqual([p["name"] for p in out["profiles"]], ["Luisa 2"])
        self.assertEqual(out["retired"], [])


class RetiredNoteTest(TestCase):
    """Dropped rows are reported as a number, never in silence."""

    def test_nothing_retired_renders_nothing(self):
        self.assertEqual(report_html._retired_note([]), "")
        self.assertEqual(report_html._retired_note(None), "")

    def test_the_note_counts_and_names_them(self):
        html = report_html._retired_note(["Jil 10", "Jasmin 5"])
        self.assertIn("2 retired", html)
        self.assertIn("Jil 10", html)
        self.assertIn("Jasmin 5", html)
        self.assertIn(at.PROFILE_ISSUE_DELETED, html)

    def test_a_long_list_is_summarised_not_truncated_silently(self):
        html = report_html._retired_note([f"Blank ({n})" for n in range(20)])
        self.assertIn("20 retired", html)
        self.assertIn("and 8 more", html)


class FlagKeepsRetiredReasonTest(TestCase):
    """The retirement has to survive the loops that keep re-flagging.

    `Jil 2` and `Jil 10` were retired on 2026-08-18 and were back on the
    worklist as "Human Verification Required" within minutes: their leftover
    queue rows still fail, the retry pass reads the row's Issue Type and
    overwrote the profile's reason with it.
    """

    def _flag(self, current_reason, new_reason=at.PROFILE_ISSUE_VERIFICATION):
        client = AirtableClient("tok", "app123", "Profiles (Cloning)")
        record = Mock()
        record.json.return_value = {"fields": {at.F_PROF_ISSUE_NOTES: "older note",
                                               at.F_PROF_ISSUE_REASON: current_reason}}
        record.raise_for_status.return_value = None
        written = Mock()
        written.raise_for_status.return_value = None
        with patch("adb_bot.clients.airtable.requests.get", return_value=record), \
                patch("adb_bot.clients.airtable.requests.patch",
                      return_value=written) as mock_patch:
            ok = client.flag_profile_for_human("rec1", new_reason, "it failed again")
        self.assertTrue(ok)
        return mock_patch.call_args.kwargs["json"]["fields"]

    def test_a_retired_profile_keeps_its_reason(self):
        fields = self._flag(at.PROFILE_ISSUE_DELETED)
        self.assertNotIn(at.F_PROF_ISSUE_REASON, fields)

    def test_the_flag_and_the_note_are_still_written(self):
        # Only the label is protected: the flag stays ticked and the history
        # still grows, so nothing is lost by leaving the profile retired.
        fields = self._flag(at.PROFILE_ISSUE_DELETED)
        self.assertIs(fields[at.F_PROF_NEEDS_HUMAN], True)
        self.assertIn("it failed again", fields[at.F_PROF_ISSUE_NOTES])
        self.assertIn("older note", fields[at.F_PROF_ISSUE_NOTES])

    def test_an_ordinary_profile_still_gets_its_reason_written(self):
        fields = self._flag(at.PROFILE_ISSUE_UNREACHABLE)
        self.assertEqual(fields[at.F_PROF_ISSUE_REASON], at.PROFILE_ISSUE_VERIFICATION)

    def test_a_profile_with_no_reason_yet_still_gets_one(self):
        fields = self._flag("")
        self.assertEqual(fields[at.F_PROF_ISSUE_REASON], at.PROFILE_ISSUE_VERIFICATION)

    def test_retiring_a_profile_is_itself_allowed(self):
        # The guard must not block *setting* the retired reason, or nothing
        # could ever mark a profile retired through this path.
        fields = self._flag(at.PROFILE_ISSUE_VERIFICATION,
                            new_reason=at.PROFILE_ISSUE_DELETED)
        self.assertEqual(fields[at.F_PROF_ISSUE_REASON], at.PROFILE_ISSUE_DELETED)

    def test_a_failed_read_does_not_cost_the_flag(self):
        # Losing the history is better than losing the write that was the point.
        client = AirtableClient("tok", "app123", "Profiles (Cloning)")
        written = Mock()
        written.raise_for_status.return_value = None
        with patch("adb_bot.clients.airtable.requests.get",
                   side_effect=RuntimeError("boom")), \
                patch("adb_bot.clients.airtable.requests.patch",
                      return_value=written) as mock_patch:
            ok = client.flag_profile_for_human(
                "rec1", at.PROFILE_ISSUE_VERIFICATION, "it failed again")
        self.assertTrue(ok)
        fields = mock_patch.call_args.kwargs["json"]["fields"]
        self.assertEqual(fields[at.F_PROF_ISSUE_REASON], at.PROFILE_ISSUE_VERIFICATION)


class RetiredStaysBlockedTest(TestCase):
    """A retired phone's queue rows outlive it, and must stay held.

    The outlook works out when a row goes out from its due time, so a row whose
    profile is missing from the blocked map reads as "going out on the next
    posting tick". Dropping retired profiles from the *tabs* must not drop them
    from this map -- that would put ~150 rows aimed at phones that do not exist
    back into the imminent count.
    """

    def test_a_retired_profile_is_blocked_with_its_own_reason(self):
        blocked = report.profiles_blocked_from_posting([profile(name="Jil 10")])
        self.assertEqual(blocked["Jil 10"], report.BLOCKED_RETIRED)

    def test_retired_outranks_flagged_and_parked(self):
        # Nearly every retired profile is also flagged and parked. The reason
        # shown has to be the one that says what can actually be done: a flag
        # clears and a park un-parks, a deleted phone does neither.
        blocked = report.profiles_blocked_from_posting(
            [profile(name="Jil 10", needs_human=True, status="Inactive")])
        self.assertEqual(blocked["Jil 10"], report.BLOCKED_RETIRED)

    def test_ordinary_flags_and_parks_are_unchanged(self):
        blocked = report.profiles_blocked_from_posting([
            profile(name="Luisa 2", reason="", needs_human=True, status="Active"),
            profile(name="Luisa 8", reason="", needs_human=False, status="Inactive"),
            profile(name="Luisa 9", reason="", needs_human=False, status="Active"),
        ])
        self.assertEqual(blocked["Luisa 2"], report.BLOCKED_FLAGGED)
        self.assertEqual(blocked["Luisa 8"], report.BLOCKED_PARKED)
        self.assertNotIn("Luisa 9", blocked)
