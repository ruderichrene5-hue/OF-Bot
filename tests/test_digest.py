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
        def send(self, text, logger=None, category=""):
            self.sent.append(text); return True
        def allows(self, category=""): return True
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


class BlockedWarmupTest(unittest.TestCase):
    """A warm-up that has run out of things the bot is allowed to do."""

    def _p(self, name, started="2026-08-07", serial=""):
        f = {at.F_PROF_NAME: name}
        if started:
            f[at.F_PROF_WARMUP_STARTED] = started
        if serial:
            f[at.F_PROF_MLX_SERIAL] = serial
        return {"fields": f}

    def test_a_blank_in_warmup_is_waiting_on_a_model(self):
        self.assertEqual(
            digest.blocked_warmup([self._p("Blank (12)"), self._p("Blank 1 (3)")]),
            ["Blank (12)", "Blank 1 (3)"])

    def test_a_named_profile_in_warmup_is_not_blocked(self):
        """It has a model, so day 4's reel can be built for it."""
        self.assertEqual(digest.blocked_warmup([self._p("Nikki 15")]), [])

    def test_a_blank_that_never_started_warmup_is_not_reported(self):
        """Staging profiles sitting in the workspace are nobody's errand yet."""
        self.assertEqual(digest.blocked_warmup([self._p("Blank (40)", started=None)]), [])

    def test_the_digest_explains_the_fix_not_just_the_count(self):
        d = digest.build_digest([self._p("Blank (12)")], [], now=NOW)
        body = digest.format_digest(d)
        self.assertIn("cannot finish", body)
        self.assertIn("Blank (12)", body)
        # the actual remedy, or it is just another number
        self.assertIn("Model", body)

    def test_blocked_warmup_alone_means_the_day_is_not_quiet(self):
        d = digest.build_digest([self._p("Blank (12)")], [], now=NOW)
        self.assertFalse(d.quiet)

    def test_two_phones_sharing_a_name_are_told_apart_by_serial(self):
        """MLX names are not unique -- three phones are called `Blank (5)`.
        Both belong in the sample, because both are somebody's work; what must
        not happen is two identical strings, which reads as a bug rather than
        as two phones. The count stays the true number of profiles."""
        d = digest.build_digest([self._p("Blank (1)", serial="257884"),
                                 self._p("Blank (1)", serial="262899"),
                                 self._p("Blank (2)", serial="257885")], [], now=NOW)
        self.assertEqual(len(d.blocked_warmup), 3)
        body = digest.format_digest(d)
        sample = body.split("e.g. ")[1].split("\n")[0]
        self.assertEqual(sample.count("Blank (1)"), 2)
        self.assertIn("257884", sample)
        self.assertIn("262899", sample)
        self.assertIn("3 warm-up phones", body)

    def test_a_phone_with_no_serial_still_shows_its_name(self):
        d = digest.build_digest([self._p("Blank (1)")], [], now=NOW)
        self.assertEqual(d.blocked_sample, ["Blank (1)"])


class TaggedWarmupTest(unittest.TestCase):
    """Warm-up phones marked `Issue` in MultiLogin with nothing ticked here.

    The mirror runs Airtable -> MultiLogin only, so a tag a VA applies by hand
    reaches no field the bot reads. For a warming phone that is total silence:
    it has no queue rows to fail and no flag to raise, so this is the only line
    that can ever mention it.
    """

    def _tagged(self, *names):
        return [{"name": n, "serial": "1", "status": "Active"} for n in names]

    def test_the_names_are_carried_into_the_digest(self):
        d = digest.build_digest([], [], now=NOW,
                                tagged_warmup=self._tagged("Blank (12)", "Blank (8)"))
        self.assertEqual(d.tagged_warmup, ["Blank (12)", "Blank (8)"])

    def test_it_says_what_to_do_not_just_the_count(self):
        d = digest.build_digest([], [], now=NOW, tagged_warmup=self._tagged("Blank (12)"))
        body = digest.format_digest(d)
        self.assertIn("Blank (12)", body)
        self.assertIn("MultiLogin", body)
        self.assertIn("Needs Human Check", body)

    def test_tagged_alone_means_the_day_is_not_quiet(self):
        """Twenty-one marked phones and a message saying 'nothing waiting on a
        person' is the exact failure this was written to end."""
        d = digest.build_digest([], [], now=NOW, tagged_warmup=self._tagged("Blank (12)"))
        self.assertFalse(d.quiet)
        self.assertNotIn("nothing waiting on a person", digest.format_digest(d))

    def test_omitting_it_leaves_the_section_out_entirely(self):
        """MultiLogin being unreadable costs this section, not the digest."""
        d = digest.build_digest([], [], now=NOW)
        self.assertEqual(d.tagged_warmup, [])
        self.assertNotIn("tagged", digest.format_digest(d).lower())

    def test_one_phone_reads_as_singular(self):
        body = digest.format_digest(digest.build_digest(
            [], [], now=NOW, tagged_warmup=self._tagged("Blank (12)")))
        self.assertIn("1 warm-up phone tagged", body)

    def test_two_phones_sharing_a_name_are_told_apart_by_serial(self):
        """MLX names are not unique -- this workspace has three `Blank (5)`."""
        d = digest.build_digest([], [], now=NOW, tagged_warmup=[
            {"name": "Blank (1)", "serial": "257884", "launch_id": "L1"},
            {"name": "Blank (1)", "serial": "262899", "launch_id": "L2"},
            {"name": "Blank (2)", "serial": "257885", "launch_id": "L3"}])
        self.assertEqual(len(d.tagged_warmup), 3)
        sample = digest.format_digest(d).split("e.g. ")[1].split("\n")[0]
        self.assertEqual(sample.count("Blank (1)"), 2)
        self.assertIn("257884", sample)
        self.assertIn("262899", sample)

    def test_plain_strings_are_accepted_too(self):
        d = digest.build_digest([], [], now=NOW, tagged_warmup=["Blank (12)"])
        self.assertEqual(d.tagged_warmup, ["Blank (12)"])


class NoDoubleReportingTest(unittest.TestCase):
    """A phone named once per message.

    Every tagged warm-up phone is also a `Blank`, so both warm-up sections
    described the same twenty-one phones. The tagged section keeps them --
    "somebody marked this" is a finding a person made, "waiting for a model" is
    the default state of the whole population -- and the model backlog keeps
    its true size so the errand does not appear to shrink.
    """

    def _p(self, name, api_id, started="2026-08-07"):
        return {"fields": {at.F_PROF_NAME: name, at.F_PROF_MLX_API_ID: api_id,
                           at.F_PROF_WARMUP_STARTED: started}}

    def _tag(self, name, api_id):
        return {"name": name, "serial": "1", "status": "Active", "launch_id": api_id}

    def test_a_tagged_phone_is_dropped_from_the_blocked_list(self):
        d = digest.build_digest(
            [self._p("Blank (12)", "L1"), self._p("Blank (8)", "L2")], [], now=NOW,
            tagged_warmup=[self._tag("Blank (12)", "L1")])
        self.assertEqual(d.tagged_warmup, ["Blank (12)"])
        self.assertEqual(d.blocked_warmup, ["Blank (8)"])

    def test_the_model_backlog_keeps_its_true_size(self):
        """They still need a model; they are just filed under the sharper heading."""
        d = digest.build_digest(
            [self._p("Blank (12)", "L1"), self._p("Blank (8)", "L2")], [], now=NOW,
            tagged_warmup=[self._tag("Blank (12)", "L1")])
        self.assertEqual(d.blocked_warmup_total, 2)
        body = digest.format_digest(d)
        self.assertIn("1 more warm-up phone", body)
        self.assertIn("2 in all", body)

    def test_no_phone_appears_in_both_sections(self):
        d = digest.build_digest(
            [self._p("Blank (12)", "L1"), self._p("Blank (8)", "L2")], [], now=NOW,
            tagged_warmup=[self._tag("Blank (12)", "L1")])
        self.assertEqual(set(d.tagged_warmup) & set(d.blocked_warmup), set())

    def test_an_untagged_twin_is_not_dropped_with_its_namesake(self):
        """The workspace has two `Blank (13)` and three `Blank (5)`. Subtracting
        by name would silently drop the untagged one of every pair -- which is a
        phone waiting for a model that nothing would then mention."""
        d = digest.build_digest(
            [self._p("Blank (13)", "L1"), self._p("Blank (13)", "L2")], [], now=NOW,
            tagged_warmup=[self._tag("Blank (13)", "L1")])
        self.assertEqual(d.blocked_warmup, ["Blank (13)"])
        self.assertEqual(d.blocked_warmup_total, 2)

    def test_suppressing_everything_still_names_the_model_errand(self):
        """The thing that actually unblocks them must not vanish with the line."""
        d = digest.build_digest([self._p("Blank (12)", "L1")], [], now=NOW,
                                tagged_warmup=[self._tag("Blank (12)", "L1")])
        self.assertEqual(d.blocked_warmup, [])
        body = digest.format_digest(d)
        self.assertIn("model", body)
        self.assertIn("Blank (NN)", body)
        self.assertIn("Those 1", body)

    def test_with_nothing_tagged_the_wording_is_unchanged(self):
        """No suppression, so no 'more' and no cross-reference."""
        d = digest.build_digest([self._p("Blank (12)", "L1")], [], now=NOW)
        body = digest.format_digest(d)
        self.assertIn("1 warm-up phone cannot finish", body)
        self.assertNotIn("more warm-up", body)
        self.assertNotIn("in all", body)

    def test_a_phone_with_no_api_id_is_never_matched_away(self):
        d = digest.build_digest([self._p("Blank (12)", "")], [], now=NOW,
                                tagged_warmup=[self._tag("Blank (12)", "L1")])
        self.assertEqual(d.blocked_warmup, ["Blank (12)"])

    def test_a_name_in_both_sections_is_two_distinguishable_phones(self):
        """The case that prompted the split: `Blank (10)` is three phones, one
        tagged and two not, so the same string legitimately appears in both
        sections. Without the serial that reads as the duplication the
        suppression was supposed to have removed."""
        rows = [self._p("Blank (10)", "L1"), self._p("Blank (10)", "L2")]
        rows[0]["fields"][at.F_PROF_MLX_SERIAL] = "257884"
        rows[1]["fields"][at.F_PROF_MLX_SERIAL] = "262899"
        d = digest.build_digest(rows, [], now=NOW, tagged_warmup=[
            {"name": "Blank (10)", "serial": "257884", "launch_id": "L1"}])
        self.assertEqual(d.tagged_sample, ["Blank (10) · 257884"])
        self.assertEqual(d.blocked_sample, ["Blank (10) · 262899"])
        # Same name in both lines, and never the same phone.
        self.assertEqual(set(d.tagged_sample) & set(d.blocked_sample), set())
