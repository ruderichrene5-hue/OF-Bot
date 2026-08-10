"""Mirroring Airtable's `Needs Human Check` onto the MultiLogin `Issue` tag.

The thing worth guarding here is what this pass is allowed to *remove*. It runs
against a workspace where `Issue` already has ~33 hand-applied uses, most of them
on parked profiles that were never flagged in Airtable, and where `Created` is
the selector that decides which profiles get warmed up at all. A reconciler that
reasons "I own this tag name, so I may take it off anywhere I find it" -- which
is exactly how `warmup_state` reasons about its own tags, correctly -- would
delete a person's notes and could drop profiles out of the warm-up. So most of
what follows is about the two fences on removal: the owned-tag set, and the
ledger of profiles this pass tagged itself.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from adb_bot.automation import issue_tags
from adb_bot.automation import schedule_spec

ISSUE_TAG_ID = "de2aef69-b8c1-4547-90a4-0875b1d85361"


# --------------------------------------------------------------------- fakes


class FakeTagClient:
    """Stands in for `MultiloginTagClient`, recording every call.

    `fail_on` names the profile ids whose tag calls raise, because the real
    client's `_post` raises on any status >= 400 -- one dead profile id would
    otherwise abort a whole sweep.
    """

    def __init__(self, fail_on=(), fail_ensure=False, tag_id=ISSUE_TAG_ID):
        self.assigned = []      # (profile_id, [tag_ids])
        self.unassigned = []
        self.ensured = []       # (name, colour)
        self.fail_on = set(fail_on)
        self.fail_ensure = fail_ensure
        self.tag_id = tag_id

    def ensure_tag(self, name, color="gray"):
        if self.fail_ensure:
            raise RuntimeError("MLX 503")
        self.ensured.append((name, color))
        return self.tag_id

    def _check(self, profile_id):
        if profile_id in self.fail_on:
            raise RuntimeError(f"MLX 400 unknown profile {profile_id}")

    def assign(self, profile_id, tag_ids):
        self._check(profile_id)
        self.assigned.append((profile_id, list(tag_ids)))
        return True

    def unassign(self, profile_id, tag_ids):
        self._check(profile_id)
        self.unassigned.append((profile_id, list(tag_ids)))
        return True


class FakeAirtable:
    def __init__(self, rows, raises=False):
        self.rows = rows
        self.raises = raises
        self.calls = 0

    def posting_profiles(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("Airtable 500")
        return list(self.rows)


def _row(name="Jil 3", launch_id="100000001", needs_human=False, record_id="recA"):
    return {"record_id": record_id, "name": name, "launch_id": launch_id,
            "status": "Active", "needs_human": needs_human, "flagged_at": None}


def _mlx(launch_id="100000001", serial="262894", tags=()):
    """One `/mobile_profiles/phone/list` item, as `normalize_mlx_item` wants it."""
    return {"id": launch_id, "serial_no": serial, "serial_name": "phone",
            "tags": list(tags), "equipment_info": {}, "proxy": {}}


def _sync(rows, items, tmpdir, **kwargs):
    kwargs.setdefault("dry_run", False)
    client = kwargs.pop("tag_client", None)
    if client is None:
        client = FakeTagClient()
    report = issue_tags.sync_issue_tags(
        FakeAirtable(rows), tag_client=client, mlx_items=items,
        app_dir=tmpdir, **kwargs)
    return report, client


# ------------------------------------------------------------- the predicate


class TagChangeTest(unittest.TestCase):
    """`ProfileIssue.tag_changes` on its own -- no clients, no ledger file."""

    def _changes(self, tags, flagged, owned):
        return issue_tags.ProfileIssue(
            record_id="recA", name="Jil 3", launch_id="1", flagged=flagged,
            current_tags=tuple(tags)).tag_changes(owned)

    def test_a_flagged_profile_without_the_tag_gets_it(self):
        self.assertEqual(self._changes([], True, False), (["Issue"], []))

    def test_a_flagged_profile_that_already_has_it_is_left_alone(self):
        self.assertEqual(self._changes(["Issue"], True, True), ([], []))

    def test_the_tag_is_matched_case_insensitively(self):
        """MLX tag names are typed by a person in the UI, and `tag_ids_by_name`
        lower-cases. Reading `issue` as a different tag would add a second one."""
        self.assertEqual(self._changes(["issue"], True, False), ([], []))

    def test_clearing_the_flag_removes_a_tag_the_bot_put_there(self):
        self.assertEqual(self._changes(["Issue"], False, True), ([], ["Issue"]))

    def test_it_leaves_a_hand_applied_tag_where_it_is(self):
        """The 26 parked profiles somebody tagged by hand and never flagged in
        Airtable. Removing these is the single most damaging thing this feature
        could do, and it is not recoverable -- Airtable never knew about them."""
        self.assertEqual(self._changes(["Issue"], False, False), ([], []))

    def test_an_unflagged_untagged_profile_is_a_no_op(self):
        self.assertEqual(self._changes(["Created"], False, True), ([], []))

    def test_it_never_removes_a_tag_it_does_not_own(self):
        """The invariant. `Created` selects the warm-up population and the
        `Warmup Day N` tags belong to `warmup_state`; taking either off would
        silently drop a profile out of its warm-up. Even with the ledger saying
        this profile is ours, nothing outside OWNED_TAGS may come off."""
        _, remove = self._changes(
            ["Created", "Warmup Day 2 Done", "gmail", "2 accounts", "Banned / Dead"],
            False, True)
        self.assertEqual(remove, [])

    def test_only_the_owned_tag_comes_off_a_profile_wearing_many(self):
        _, remove = self._changes(
            ["Created", "Issue", "Warmup Day 2 Done", "gmail"], False, True)
        self.assertEqual(remove, ["Issue"])

    def test_the_owned_set_is_exactly_the_issue_tag(self):
        self.assertEqual([t.lower() for t in issue_tags.owned_tags()], ["issue"])


# ------------------------------------------------------------------ the sweep


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_flagged_profile_is_tagged_and_recorded_as_ours(self):
        report, client = _sync([_row(needs_human=True)], [_mlx()], self.tmpdir)
        self.assertEqual(client.assigned, [("100000001", [ISSUE_TAG_ID])])
        self.assertEqual(client.unassigned, [])
        self.assertEqual((report.tagged, report.untagged), (1, 0))
        self.assertIn("100000001", issue_tags.load_ledger(self.tmpdir))

    def test_clearing_the_flag_takes_the_tag_off_again(self):
        """Two-way, and through the ledger the first pass wrote -- not through a
        second reading of Airtable."""
        _sync([_row(needs_human=True)], [_mlx()], self.tmpdir)
        report, client = _sync([_row(needs_human=False)],
                               [_mlx(tags=["Issue"])], self.tmpdir)
        self.assertEqual(client.unassigned, [("100000001", [ISSUE_TAG_ID])])
        self.assertEqual((report.tagged, report.untagged), (0, 1))
        self.assertEqual(issue_tags.load_ledger(self.tmpdir), {})

    def test_a_correct_profile_costs_no_calls_at_all(self):
        """Idempotence with teeth: the second sweep of a settled fleet must not
        re-assign anything, or a 15-minute timer is 96 pointless writes a day."""
        report, client = _sync([_row(needs_human=True)],
                               [_mlx(tags=["Issue", "Created"])], self.tmpdir)
        self.assertEqual(client.assigned, [])
        self.assertEqual(client.unassigned, [])
        self.assertEqual(report.unchanged, 1)

    def test_a_hand_tagged_unflagged_profile_survives_the_sweep(self):
        """The 26. No ledger entry, so nothing may come off -- and it is counted
        separately rather than filed under `unchanged`, so the population this
        pass is deliberately not managing stays visible in the log."""
        report, client = _sync([_row(needs_human=False)],
                               [_mlx(tags=["Issue"])], self.tmpdir)
        self.assertEqual(client.unassigned, [])
        self.assertEqual(report.untagged, 0)
        self.assertEqual(report.skipped_not_ours, 1)

    def test_a_lost_ledger_can_only_cost_an_extra_tag_never_a_removal(self):
        """If the state file disappears, the bot's own tags become
        un-removable until the profile is flagged and cleared again. That is the
        failure this design chooses; assert it stays the cheap one."""
        _sync([_row(needs_human=True)], [_mlx()], self.tmpdir)
        (self.tmpdir / issue_tags.STATE_FILENAME).unlink()
        report, client = _sync([_row(needs_human=False)],
                               [_mlx(tags=["Issue"])], self.tmpdir)
        self.assertEqual(client.unassigned, [])
        self.assertEqual(report.untagged, 0)

    def test_adopt_existing_is_off_by_default_and_strips_only_when_asked(self):
        rows, items = [_row(needs_human=False)], [_mlx(tags=["Issue"])]
        report, client = _sync(rows, items, self.tmpdir)
        self.assertEqual(client.unassigned, [])
        report, client = _sync(rows, items, self.tmpdir, adopt_existing=True)
        self.assertEqual(client.unassigned, [("100000001", [ISSUE_TAG_ID])])
        self.assertEqual(report.untagged, 1)

    def test_adopting_still_cannot_reach_a_tag_it_does_not_own(self):
        """The outer fence holds even with ownership forced on: `--adopt-existing`
        is about *whose* `Issue` tag it is, never about which tags are in play."""
        _, client = _sync([_row(needs_human=False)],
                          [_mlx(tags=["Created", "Warmup Day 2 Done"])],
                          self.tmpdir, adopt_existing=True)
        self.assertEqual(client.unassigned, [])

    # --------------------------------------------------------- the join key

    def test_a_row_with_no_mlx_api_id_is_skipped_and_counted(self):
        """Never fall back to the human serial: the tag endpoints take the
        18-digit API ID and reject anything else."""
        report, client = _sync([_row(launch_id=None, needs_human=True)],
                               [_mlx()], self.tmpdir)
        self.assertEqual(client.assigned, [])
        self.assertEqual(report.no_launch_id, 1)

    def test_a_row_pointing_at_a_deleted_profile_is_skipped_not_attempted(self):
        """`Jasmin 9` on the live base: flagged, and its MLX profile is gone.
        Attempting it would 400, and the real client raises -- one stale id would
        abort the rest of the sweep. Reported by name, because a flagged row
        pointing at nothing is itself worth a person's attention."""
        report, client = _sync([_row(name="Jasmin 9", launch_id="999", needs_human=True)],
                               [_mlx()], self.tmpdir)
        self.assertEqual(client.assigned, [])
        self.assertEqual(report.missing_in_mlx, 1)
        self.assertTrue(any("Jasmin 9" in line for line in report.stale))

    # ------------------------------------------------------------ outages

    def test_no_tag_client_reports_and_writes_nothing(self):
        report = issue_tags.sync_issue_tags(
            FakeAirtable([_row(needs_human=True)]), tag_client=None,
            mlx_items=[_mlx()], app_dir=self.tmpdir, dry_run=False)
        self.assertEqual(report.checked, 0)
        self.assertTrue(report.errors)

    def test_an_empty_inventory_is_refused_rather_than_reconciled_against(self):
        """An empty list is indistinguishable from MultiLogin being unreachable,
        and reconciling against it would read all 151 profiles as untagged."""
        report, client = _sync([_row(needs_human=True)], [], self.tmpdir)
        self.assertEqual(client.assigned, [])
        self.assertTrue(report.errors)

    def test_an_unresolvable_tag_id_stops_before_any_profile_call(self):
        client = FakeTagClient(fail_ensure=True)
        report, _ = _sync([_row(needs_human=True)], [_mlx()], self.tmpdir,
                          tag_client=client)
        self.assertEqual(client.assigned, [])
        self.assertTrue(any("Issue" in e for e in report.errors))

    def test_one_failing_profile_does_not_stop_the_others(self):
        rows = [_row(name="A", launch_id="1", record_id="recA", needs_human=True),
                _row(name="B", launch_id="2", record_id="recB", needs_human=True)]
        items = [_mlx("1", "s1"), _mlx("2", "s2")]
        client = FakeTagClient(fail_on={"1"})
        report, _ = _sync(rows, items, self.tmpdir, tag_client=client)
        self.assertEqual(client.assigned, [("2", [ISSUE_TAG_ID])])
        self.assertEqual(report.tagged, 1)
        self.assertEqual(len(report.errors), 1)
        # The one that failed is not recorded as ours, so a later clear will not
        # try to remove a tag that was never applied.
        self.assertEqual(sorted(issue_tags.load_ledger(self.tmpdir)), ["2"])

    def test_a_total_outage_gives_up_instead_of_walking_the_fleet(self):
        n = issue_tags.MAX_CONSECUTIVE_FAILURES + 4
        ids = [str(i) for i in range(1, n + 1)]
        rows = [_row(name=f"P{i}", launch_id=i, record_id=f"rec{i}", needs_human=True)
                for i in ids]
        items = [_mlx(i, f"s{i}") for i in ids]
        client = FakeTagClient(fail_on=set(ids))
        report, _ = _sync(rows, items, self.tmpdir, tag_client=client)
        self.assertEqual(report.tagged, 0)
        self.assertLess(report.checked, n)
        self.assertIn("stopping this sweep", report.errors[-1])

    def test_an_airtable_failure_is_reported_not_raised(self):
        report = issue_tags.sync_issue_tags(
            FakeAirtable([], raises=True), tag_client=FakeTagClient(),
            mlx_items=[_mlx()], app_dir=self.tmpdir, dry_run=False)
        self.assertTrue(any("Airtable" in e for e in report.errors))

    def test_an_unwritable_ledger_is_reported_rather_than_silently_lost(self):
        with mock.patch.object(issue_tags, "save_ledger", return_value=False):
            report, _ = _sync([_row(needs_human=True)], [_mlx()], self.tmpdir)
        self.assertTrue(any(issue_tags.STATE_FILENAME in e for e in report.errors))

    # ------------------------------------------------------------ dry run

    def test_dry_run_plans_without_touching_multilogin(self):
        """Not even `ensure_tag`: resolving the name is a write on a workspace
        that has not got the tag, and a plan-only run must not create one."""
        report, client = _sync([_row(needs_human=True)], [_mlx()], self.tmpdir,
                               dry_run=True)
        self.assertEqual((client.assigned, client.unassigned, client.ensured),
                         ([], [], []))
        self.assertEqual(report.tagged, 1)
        self.assertTrue(report.changes)
        self.assertFalse((self.tmpdir / issue_tags.STATE_FILENAME).exists())

    def test_dry_run_reports_the_removal_it_would_make(self):
        _sync([_row(needs_human=True)], [_mlx()], self.tmpdir)
        report, client = _sync([_row(needs_human=False)], [_mlx(tags=["Issue"])],
                               self.tmpdir, dry_run=True)
        self.assertEqual(client.unassigned, [])
        self.assertEqual(report.untagged, 1)
        # ...and the ledger still says the profile is ours, so the apply run
        # that follows the plan makes the same decision.
        self.assertIn("100000001", issue_tags.load_ledger(self.tmpdir))

    # ------------------------------------------------------------ batching

    def test_the_tag_id_is_resolved_once_for_the_whole_sweep(self):
        """One name, ~150 profiles. Resolving per profile would be 150 extra
        `tag/search` calls a tick."""
        ids = [str(i) for i in range(1, 6)]
        rows = [_row(name=f"P{i}", launch_id=i, record_id=f"rec{i}", needs_human=True)
                for i in ids]
        _, client = _sync(rows, [_mlx(i, f"s{i}") for i in ids], self.tmpdir)
        self.assertEqual(client.ensured, [("Issue", "purple")])
        self.assertEqual(len(client.assigned), 5)

    def test_no_call_exceeds_the_documented_per_call_tag_ceiling(self):
        from adb_bot.clients.multilogin import tags as tag_mod

        _, client = _sync([_row(needs_human=True)], [_mlx()], self.tmpdir)
        for _profile, tag_ids in client.assigned + client.unassigned:
            self.assertLessEqual(len(tag_ids), tag_mod.MAX_TAGS_PER_CALL)


# ----------------------------------------------------------------- the wiring


class WiringTest(unittest.TestCase):
    def test_the_loop_is_a_run_loop_command(self):
        from adb_bot.automation import run_loop

        self.assertIn("issue-tags", run_loop.LOOPS)
        self.assertIn("issue-tags", run_loop._DISPATCH)

    def test_it_is_scheduled_and_has_something_to_say_for_itself(self):
        self.assertIn("issue-tags", schedule_spec.RECOMMENDED_LOOPS)
        self.assertIn("issue-tags", schedule_spec.RECOMMENDED_INTERVALS)
        self.assertTrue(schedule_spec.WHAT_IT_DOES.get("issue-tags"))
        self.assertTrue(schedule_spec.DESCRIPTIONS.get("issue-tags"))

    def _run(self, argv):
        from adb_bot.automation import run_loop

        with mock.patch.object(run_loop, "_airtable", return_value=FakeAirtable([])), \
                mock.patch.object(run_loop, "_mlx_token", return_value=""), \
                mock.patch.object(issue_tags, "sync_issue_tags") as sync:
            sync.return_value = issue_tags.IssueTagReport()
            self.assertEqual(run_loop.main(argv), 0)
        return sync.call_args.kwargs

    def test_the_cli_defaults_to_a_dry_run_that_adopts_nothing(self):
        """Both defaults matter and neither is the default argparse would give a
        positional: without --apply nothing is written, and without
        --adopt-existing no hand-applied tag is in scope."""
        kwargs = self._run(["issue-tags"])
        self.assertTrue(kwargs["dry_run"])
        self.assertFalse(kwargs["adopt_existing"])

    def test_apply_and_adopt_are_separate_switches(self):
        kwargs = self._run(["issue-tags", "--apply", "--adopt-existing"])
        self.assertFalse(kwargs["dry_run"])
        self.assertTrue(kwargs["adopt_existing"])

    def test_a_missing_multilogin_token_is_survived_not_fatal(self):
        """No token means no tag client, and the pass says so and exits clean --
        a MultiLogin outage must not turn the unit red."""
        kwargs = self._run(["issue-tags"])
        self.assertIsNone(kwargs["tag_client"])


if __name__ == "__main__":
    unittest.main()
