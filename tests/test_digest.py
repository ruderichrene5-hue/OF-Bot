"""The once-a-day backlog message.

The composition is pure, so everything that decides what the VAs read is
testable here without Airtable or Telegram.
"""

import unittest
from datetime import datetime, timedelta, timezone

from adb_bot.automation import digest
from adb_bot.clients import airtable as at

NOW = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)


def _profile(name, flagged=False, reason=None, flagged_at=None, status=None):
    f = {at.F_PROF_NAME: name}
    if flagged:
        f[at.F_PROF_NEEDS_HUMAN] = True
    if reason:
        f[at.F_PROF_ISSUE_REASON] = reason
    if flagged_at:
        f[at.F_PROF_FLAGGED_AT] = flagged_at.isoformat()
    if status:
        f[at.F_PROF_STATUS] = status
    return {"fields": f}


def _row(status, hours_ago):
    return {"fields": {at.F_PQ_POST_STATUS: status,
                       at.F_PQ_SCHEDULED: (NOW - timedelta(hours=hours_ago)).isoformat()}}


class BuildTest(unittest.TestCase):

    def test_it_counts_flags_by_reason_commonest_first(self):
        d = digest.build_digest([
            _profile("A", flagged=True, reason="Human Verification Required"),
            _profile("B", flagged=True, reason="Human Verification Required"),
            _profile("C", flagged=True, reason="Banned / Blocked"),
            _profile("D"),
        ], [], now=NOW)
        self.assertEqual(d.flagged, 3)
        self.assertEqual(d.by_reason,
                         [("Human Verification Required", 2), ("Banned / Blocked", 1)])

    def test_a_flag_with_no_reason_is_still_counted(self):
        d = digest.build_digest([_profile("A", flagged=True)], [], now=NOW)
        self.assertEqual(d.by_reason, [("no reason recorded", 1)])

    def test_the_oldest_flag_is_named_and_aged(self):
        d = digest.build_digest([
            _profile("Recent", flagged=True, flagged_at=NOW - timedelta(days=1)),
            _profile("Jil 1", flagged=True, flagged_at=NOW - timedelta(days=5)),
        ], [], now=NOW)
        self.assertEqual(d.oldest_name, "Jil 1")
        self.assertEqual(d.oldest_days, 5)
        # Only the one past the threshold counts as stuck.
        self.assertEqual(d.stale, 1)

    def test_unflagged_but_still_parked_is_reported(self):
        """The state that looks fixed from MultiLogin and posts nothing."""
        d = digest.build_digest([
            _profile("Nikki 12", flagged=False, flagged_at=NOW - timedelta(hours=2),
                     status=at.STATUS_SELECT_INACTIVE),
            # cleared and switched back on -- nothing to say about it
            _profile("Jil 6", flagged=False, flagged_at=NOW - timedelta(hours=2),
                     status=at.STATUS_SELECT_ACTIVE),
            # never flagged, parked by hand -- not this pass's business
            _profile("Blank (3)", status=at.STATUS_SELECT_INACTIVE),
        ], [], now=NOW)
        self.assertEqual(d.parked_unflagged, ["Nikki 12"])

    def test_the_window_is_the_last_24h_not_all_of_history(self):
        d = digest.build_digest([], [
            _row(at.POST_STATUS_POSTED, hours_ago=1),
            _row(at.POST_STATUS_POSTED, hours_ago=23),
            _row(at.POST_STATUS_FAILED, hours_ago=5),
            _row(at.POST_STATUS_PENDING, hours_ago=2),
            _row(at.POST_STATUS_POSTED, hours_ago=30),      # older than the window
        ], now=NOW)
        self.assertEqual((d.due, d.posted, d.failed, d.pending), (4, 2, 1, 1))

    def test_a_row_scheduled_in_the_future_is_not_counted_yet(self):
        d = digest.build_digest([], [_row(at.POST_STATUS_PENDING, hours_ago=-3)], now=NOW)
        self.assertEqual(d.due, 0)

    def test_an_unparseable_timestamp_does_not_blow_up(self):
        d = digest.build_digest(
            [_profile("A", flagged=True, reason="x")],
            [{"fields": {at.F_PQ_POST_STATUS: at.POST_STATUS_POSTED,
                         at.F_PQ_SCHEDULED: "not a date"}}], now=NOW)
        self.assertEqual((d.flagged, d.due), (1, 0))


class FormatTest(unittest.TestCase):

    def test_one_phone_reads_as_singular(self):
        body = digest.format_digest(digest.build_digest(
            [_profile("A", flagged=True, reason="Banned / Blocked")], [], now=NOW))
        self.assertIn("1 phone waiting on a person", body)

    def test_a_quiet_day_says_so_instead_of_showing_a_zero(self):
        body = digest.format_digest(digest.build_digest([_profile("A")], [], now=NOW))
        self.assertIn("nothing waiting on a person", body)
        # and it still reports the posting numbers
        self.assertIn("Last 24h", body)

    def test_the_parked_warning_names_the_profiles_and_the_fix(self):
        body = digest.format_digest(digest.build_digest([
            _profile("Nikki 12", flagged_at=NOW - timedelta(hours=1),
                     status=at.STATUS_SELECT_INACTIVE)], [], now=NOW))
        self.assertIn("Nikki 12", body)
        self.assertIn("Status", body)
        self.assertIn("Active", body)


class SendTest(unittest.TestCase):

    class _Airtable:
        def _list_table(self, table, **kw):
            if table == at.TABLE_PROFILES:
                return [_profile("A", flagged=True, reason="Banned / Blocked")]
            return []

    class _Notifier:
        def __init__(self, configured=True):
            self.configured = configured
            self.sent = []
        def send(self, text, logger=None):
            self.sent.append(text); return True
        def describe(self): return "test notifier"

    def test_a_dry_run_sends_nothing(self):
        n = self._Notifier()
        digest.run_digest(self._Airtable(), notifier=n, dry_run=True)
        self.assertEqual(n.sent, [])

    def test_apply_sends_once(self):
        n = self._Notifier()
        digest.run_digest(self._Airtable(), notifier=n, dry_run=False)
        self.assertEqual(len(n.sent), 1)

    def test_an_unconfigured_notifier_is_not_an_error(self):
        n = self._Notifier(configured=False)
        digest.run_digest(self._Airtable(), notifier=n, dry_run=False)
        self.assertEqual(n.sent, [])
