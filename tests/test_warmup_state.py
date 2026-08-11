"""Publishing the warm-up campaign into Airtable and MultiLogin.

The thing worth guarding here is the distinction the whole feature rests on:
the day a profile is *on* and the day it has *finished* are different numbers,
and a profile that has stopped moving is the one where they disagree. Publish
the wrong one and MultiLogin says "Warmup Day 4 Done" about a phone that has
failed every night since day 1 -- which is worse than saying nothing.
"""

import unittest

from adb_bot.automation import warmup_state
from adb_bot.clients import airtable as at

PLAN_DAYS = 4


def _row(name="Blank (5)", serial="262894", launch_id="111", record_id="recP1",
         day=2, day_done=1, runs_done=1, last_at_iso="2026-08-08T08:50:29.000Z",
         last_result="Done"):
    return {"name": name, "serial": serial, "launch_id": launch_id,
            "record_id": record_id, "day": day, "day_done": day_done,
            "runs_done": runs_done, "last_at_iso": last_at_iso,
            "last_at": "2026-08-08 08:50", "last_result": last_result}


def _progress(*rows, plan_days=PLAN_DAYS, error="", finish_day=None):
    out = {"profiles": list(rows) or [_row()], "plan_days": plan_days, "error": error}
    if finish_day is not None:
        out["finish_day"] = finish_day
    return out


class StageTest(unittest.TestCase):
    def test_nothing_completed_is_not_started(self):
        self.assertEqual(warmup_state.stage_for(1, 0, PLAN_DAYS),
                         warmup_state.TAG_NOT_STARTED)

    def test_the_stage_is_the_day_completed_not_the_day_reached(self):
        """The failure this feature exists to make visible: five calendar days
        in, one run landed. Tagging it day 5 would hide it."""
        self.assertEqual(warmup_state.stage_for(5, 1, PLAN_DAYS), "Warmup Day 1 Done")

    def test_the_last_plan_day_completed_is_finished(self):
        self.assertEqual(warmup_state.stage_for(4, 4, PLAN_DAYS),
                         warmup_state.TAG_FINISHED)

    def test_past_the_plan_without_the_runs_is_not_finished(self):
        self.assertEqual(warmup_state.stage_for(9, 2, PLAN_DAYS), "Warmup Day 2 Done")

    def test_an_unreadable_plan_never_calls_anything_finished(self):
        """plan_days 0 means the Warmup Plan table would not read. Declaring a
        profile ready off a plan of unknown length is the one unrecoverable
        mistake here -- a person acts on it and moves the account to posting."""
        self.assertEqual(warmup_state.stage_for(9, 9, 0), "Warmup Day 4 Done")

    def test_a_plan_longer_than_the_tags_stops_at_the_last_tag(self):
        self.assertEqual(warmup_state.stage_for(6, 6, 8), "Warmup Day 4 Done")


class FinishDayStageTest(unittest.TestCase):
    """The day that *finishes* the warm-up is not always the length of the plan.

    Only the days asking for warm-up activity gate; a trailing picture/bio day
    cannot, because the bot has no picture to set until a person supplies one.
    `stage_for` therefore takes the finish day separately, and keeps treating
    `plan_days` as the answer to a different question -- which day tags the
    workspace actually has.
    """

    def test_short_of_the_finish_day_is_still_a_day_tag(self):
        self.assertEqual(warmup_state.stage_for(6, 3, 4, 4), "Warmup Day 3 Done")

    def test_reaching_the_finish_day_is_finished(self):
        self.assertEqual(warmup_state.stage_for(6, 4, 4, 4),
                         warmup_state.TAG_FINISHED)

    def test_an_unreadable_plan_finishes_nobody_however_high_day_done_is(self):
        """finish_day 0 is `warmup_completion`'s "the Warmup Plan would not
        read". Tagging off it would tell a VA to retire `Created`, and nothing
        puts a profile back into the campaign after that."""
        self.assertNotEqual(warmup_state.stage_for(6, 4, 5, 0),
                            warmup_state.TAG_FINISHED)

    def test_a_finish_day_before_the_end_of_the_plan_still_finishes(self):
        """A 5-day plan whose day 5 asks only for a bio finishes on day 4."""
        self.assertEqual(warmup_state.stage_for(5, 4, 5, 4),
                         warmup_state.TAG_FINISHED)

    def test_build_states_carries_the_finish_day_through(self):
        state = warmup_state.build_states(
            _progress(_row(day=9, day_done=4), plan_days=5, finish_day=4))[0]
        self.assertEqual(state.stage, warmup_state.TAG_FINISHED)

    def test_without_a_finish_day_the_plan_length_still_decides(self):
        """Progress dicts written before the key existed must not suddenly
        declare nobody finished."""
        state = warmup_state.build_states(_progress(_row(day=5, day_done=4)))[0]
        self.assertEqual(state.stage, warmup_state.TAG_FINISHED)


class TagChangeTest(unittest.TestCase):
    def _changes(self, current, day_done=2, plan_days=PLAN_DAYS):
        state = warmup_state.build_states(
            _progress(_row(day_done=day_done), plan_days=plan_days),
            {"111": tuple(current)})[0]
        return state.tag_changes(plan_days)

    def test_it_adds_the_stage_tag_and_drops_the_stale_one(self):
        add, remove = self._changes(["Created", "Warmup Day 1 Done"])
        self.assertEqual(add, ["Warmup Day 2 Done"])
        self.assertEqual(remove, ["Warmup Day 1 Done"])

    def test_a_profile_already_tagged_right_is_left_alone(self):
        self.assertEqual(self._changes(["Created", "Warmup Day 2 Done"]), ([], []))

    def test_it_never_touches_a_tag_it_does_not_own(self):
        """`Created` is the population selector and `Issue` is a person's note.
        Removing either would drop the profile out of the warm-up, or erase
        something nobody asked the bot to manage."""
        add, remove = self._changes(["Created", "Issue", "gmail", "2 accounts"])
        self.assertEqual(add, ["Warmup Day 2 Done"])
        self.assertEqual(remove, [])

    def test_matching_is_case_insensitive_because_people_type_these(self):
        self.assertEqual(self._changes(["warmup day 2 DONE"]), ([], []))

    def test_it_clears_every_stale_warmup_tag_not_just_one(self):
        _add, remove = self._changes(["Warmup Day 1 Done", "Warmup Day 3 Done",
                                      warmup_state.TAG_NOT_STARTED])
        self.assertEqual(sorted(remove), sorted(["Warmup Day 1 Done", "Warmup Day 3 Done",
                                                 warmup_state.TAG_NOT_STARTED]))


class AirtableFieldsTest(unittest.TestCase):
    def test_day_and_stage_carry_different_facts(self):
        fields = warmup_state.build_states(_progress(_row(day=4, day_done=1)))[0].airtable_fields()
        self.assertEqual(fields[at.F_PROF_WARMUP_DAY], 4)
        self.assertEqual(fields[at.F_PROF_WARMUP_STAGE], "Warmup Day 1 Done")

    def test_the_last_run_is_the_iso_stamp_not_the_display_one(self):
        """`last_at` is localised for the page and Airtable will not take it."""
        fields = warmup_state.build_states(_progress())[0].airtable_fields()
        self.assertEqual(fields[at.F_PROF_WARMUP_LAST_RUN], "2026-08-08T08:50:29.000Z")

    def test_a_row_matching_airtable_is_not_rewritten(self):
        state = warmup_state.build_states(_progress())[0]
        self.assertFalse(state.airtable_differs(state.airtable_fields()))

    def test_a_number_airtable_returned_as_a_float_still_matches(self):
        state = warmup_state.build_states(_progress())[0]
        current = dict(state.airtable_fields(), **{at.F_PROF_WARMUP_DAY: 2.0})
        self.assertFalse(state.airtable_differs(current))

    def test_a_blank_is_a_blank_however_it_is_spelled(self):
        state = warmup_state.build_states(_progress(_row(last_at_iso="", last_result="")))[0]
        current = dict(state.airtable_fields(),
                       **{at.F_PROF_WARMUP_LAST_RUN: "", at.F_PROF_WARMUP_LAST_RESULT: None})
        self.assertFalse(state.airtable_differs(current))

    def test_a_changed_stage_is_written(self):
        state = warmup_state.build_states(_progress())[0]
        current = dict(state.airtable_fields(),
                       **{at.F_PROF_WARMUP_STAGE: warmup_state.TAG_NOT_STARTED})
        self.assertTrue(state.airtable_differs(current))


class FakeAirtable:
    def __init__(self, snapshot=None):
        self._snapshot = snapshot
        self.patched = []

    def warmup_state_snapshot(self):
        return self._snapshot

    def set_warmup_state(self, record_id, fields):
        self.patched.append((record_id, fields))
        return True


class FakeTags:
    def __init__(self):
        self.calls = []
        self.ensured = []

    def ensure_tag(self, name, color="gray"):
        self.ensured.append((name, color))
        return f"id:{name}"

    def retag(self, profile_id, add=(), remove=()):
        self.calls.append((profile_id, list(add), list(remove)))
        return True


def _mlx(tags=("Created",), api_id="111", serial="262894"):
    return [{"id": api_id, "serial_no": serial, "serial_name": "Blank (5)",
             "status": "ACTIVE", "tags": list(tags)}]


class SyncTest(unittest.TestCase):
    def test_it_writes_both_systems(self):
        air, tags = FakeAirtable({}), FakeTags()
        result = warmup_state.sync_warmup_state(air, _progress(), tag_client=tags,
                                                mlx_items=_mlx())
        self.assertEqual((result.airtable_written, result.tags_written), (1, 1))
        self.assertEqual(tags.calls, [("111", ["id:Warmup Day 1 Done"], [])])

    def test_a_second_sweep_writes_nothing(self):
        """Run after every tick *and* on a timer, so a no-op has to be free --
        otherwise every profile gets an hourly Last Modified and the column
        stops showing which one moved."""
        state = warmup_state.build_states(_progress())[0]
        air = FakeAirtable({"recP1": state.airtable_fields()})
        result = warmup_state.sync_warmup_state(
            air, _progress(), tag_client=FakeTags(),
            mlx_items=_mlx(tags=("Created", "Warmup Day 1 Done")))
        self.assertEqual(air.patched, [])
        self.assertEqual((result.airtable_written, result.tags_written), (0, 0))
        self.assertEqual(result.unchanged, 1)

    def test_a_dry_run_writes_nothing_but_still_reports(self):
        air, tags = FakeAirtable({}), FakeTags()
        result = warmup_state.sync_warmup_state(air, _progress(), tag_client=tags,
                                                mlx_items=_mlx(), dry_run=True)
        self.assertEqual((air.patched, tags.calls), ([], []))
        self.assertEqual(len(result.changes), 1)

    def test_missing_airtable_fields_still_produce_a_write(self):
        """`warmup_state_snapshot` answers None when the columns aren't there
        yet; every row must then count as differing, since the patch is what
        fills them in."""
        air = FakeAirtable(None)
        warmup_state.sync_warmup_state(air, _progress())
        self.assertEqual(len(air.patched), 1)

    def test_no_tag_client_still_does_the_airtable_half(self):
        """A MultiLogin outage must not also cost the day numbers."""
        air = FakeAirtable({})
        result = warmup_state.sync_warmup_state(air, _progress(), tag_client=None)
        self.assertEqual((result.airtable_written, result.tags_written), (1, 0))

    def test_an_mlx_failure_on_one_profile_does_not_abandon_the_rest(self):
        class Half(FakeTags):
            def retag(inner, profile_id, add=(), remove=()):
                if profile_id == "111":
                    raise RuntimeError("500")
                return super().retag(profile_id, add=add, remove=remove)

        progress = _progress(_row(), _row(name="Blank (6)", serial="262895",
                                          launch_id="222", record_id="recP2"))
        items = _mlx() + [{"id": "222", "serial_no": "262895", "serial_name": "Blank (6)",
                           "status": "ACTIVE", "tags": ["Created"]}]
        result = warmup_state.sync_warmup_state(FakeAirtable({}), progress,
                                                tag_client=Half(), mlx_items=items)
        self.assertEqual(result.tags_written, 1)
        self.assertEqual(len(result.errors), 1)

    def test_a_broken_progress_read_is_reported_not_written_through(self):
        air = FakeAirtable({})
        result = warmup_state.sync_warmup_state(air, _progress(error="429 Too Many Requests"))
        self.assertEqual(air.patched, [])
        self.assertEqual(len(result.errors), 1)

    def test_a_tag_is_resolved_once_across_the_fleet(self):
        """46 profiles share five tags; resolving per profile is 46 round trips
        for five answers."""
        rows = [_row(name=f"Blank ({i})", serial=str(i), launch_id=str(i),
                     record_id=f"rec{i}") for i in range(5)]
        items = [{"id": str(i), "serial_no": str(i), "serial_name": f"Blank ({i})",
                  "status": "ACTIVE", "tags": ["Created"]} for i in range(5)]
        tags = FakeTags()
        warmup_state.sync_warmup_state(FakeAirtable({}), _progress(*rows),
                                       tag_client=tags, mlx_items=items)
        self.assertEqual(tags.ensured, [("Warmup Day 1 Done", "green")])


def _fleet(count, snapshot_stage="Warmup Day 3 Done"):
    """`(progress, snapshot, mlx_items)` for `count` profiles Airtable already
    has a stage for -- a different one, so every row is a stage *move*."""
    rows = [_row(name=f"Blank ({i})", serial=str(i), launch_id=str(i),
                 record_id=f"rec{i}") for i in range(count)]
    progress = _progress(*rows)
    snapshot = {}
    for state in warmup_state.build_states(progress):
        snapshot[state.record_id] = dict(
            state.airtable_fields(),
            **{at.F_PROF_WARMUP_STAGE: snapshot_stage})
    items = [{"id": str(i), "serial_no": str(i), "serial_name": f"Blank ({i})",
              "status": "ACTIVE", "tags": ["Created", snapshot_stage]}
             for i in range(count)]
    return progress, snapshot, items


class StageBreakerTest(unittest.TestCase):
    """The one guard on a pass that runs every 30 minutes with nobody watching.

    `Warmup ready, need Bio and Pic` is an instruction to a VA to do the bio and
    the picture and then retire `Created` -- and once `Created` is gone,
    `collect_warmup_targets` never picks that profile up again. A wrong
    definition of "finished", or a Warmup Plan table that read oddly, would
    otherwise mass-relabel the workspace before anybody saw it. So a sweep that
    moves more stages than expected writes nothing and shows its work.
    """

    def test_above_the_limit_it_writes_nothing_at_all(self):
        progress, snapshot, items = _fleet(11)
        air, tags = FakeAirtable(snapshot), FakeTags()
        result = warmup_state.sync_warmup_state(
            air, progress, tag_client=tags, mlx_items=items, max_stage_changes=10)
        self.assertEqual((air.patched, tags.calls, tags.ensured), ([], [], []))
        self.assertEqual((result.airtable_written, result.tags_written), (0, 0))

    def test_it_still_lists_every_pending_change(self):
        """The refusal is only useful if it hands over the diff it refused."""
        progress, snapshot, items = _fleet(11)
        result = warmup_state.sync_warmup_state(
            FakeAirtable(snapshot), progress, tag_client=FakeTags(),
            mlx_items=items, max_stage_changes=10)
        self.assertEqual(len(result.changes), 11)
        self.assertIn("Warmup Day 3 Done -> Warmup Day 1 Done", result.changes[0])

    def test_the_error_names_the_count_the_limit_and_the_way_out(self):
        progress, snapshot, _items = _fleet(11)
        result = warmup_state.sync_warmup_state(
            FakeAirtable(snapshot), progress, max_stage_changes=10)
        self.assertEqual(len(result.errors), 1)
        message = result.errors[0]
        self.assertIn("11", message)
        self.assertIn("limit 10", message)
        self.assertIn("--max-stage-changes 11", message)

    def test_at_exactly_the_limit_it_goes_through(self):
        """The correction this release exists to make moves nine stages on the
        live base. A guard that blocked it would just be an outage."""
        progress, snapshot, items = _fleet(10)
        air, tags = FakeAirtable(snapshot), FakeTags()
        result = warmup_state.sync_warmup_state(
            air, progress, tag_client=tags, mlx_items=items, max_stage_changes=10)
        self.assertEqual(result.airtable_written, 10)
        self.assertEqual(len(tags.calls), 10)
        self.assertEqual(result.errors, [])

    def test_a_dry_run_never_trips(self):
        """A dry run is how a person reads the diff, so it has to produce one
        however large it is."""
        progress, snapshot, items = _fleet(40)
        air, tags = FakeAirtable(snapshot), FakeTags()
        result = warmup_state.sync_warmup_state(
            air, progress, tag_client=tags, mlx_items=items, dry_run=True,
            max_stage_changes=10)
        self.assertEqual((air.patched, tags.calls), ([], []))
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.changes), 40)

    def test_an_unreadable_snapshot_does_not_trip(self):
        """`warmup_state_snapshot` answers None when the columns do not exist.
        "No current stage" is not "the stage moved" -- reading it as one would
        block the very first sweep, the one that creates the column."""
        progress, _snapshot, items = _fleet(40)
        air = FakeAirtable(None)
        result = warmup_state.sync_warmup_state(
            air, progress, tag_client=FakeTags(), mlx_items=items,
            max_stage_changes=10)
        self.assertEqual(len(air.patched), 40)
        self.assertEqual(result.errors, [])

    def test_a_blank_stage_is_not_a_move_either(self):
        """First sweep on a fresh column: the rows are there, the field is
        empty. Every one of them "differs", none of them moved."""
        progress, snapshot, _items = _fleet(40, snapshot_stage="")
        air = FakeAirtable(snapshot)
        result = warmup_state.sync_warmup_state(air, progress, max_stage_changes=10)
        self.assertEqual(len(air.patched), 40)
        self.assertEqual(result.errors, [])

    def test_run_metadata_churn_does_not_trip_it(self):
        """The first sweep after the Run Log filter widens rewrites Runs Done /
        Last Run / Last Result on nearly every row. That is expected, harmless
        and undoable -- counting it would block exactly the sweep that fixes
        the fleet. Only the stage is counted."""
        rows = [_row(name=f"Blank ({i})", serial=str(i), launch_id=str(i),
                     record_id=f"rec{i}") for i in range(40)]
        progress = _progress(*rows)
        snapshot = {}
        for state in warmup_state.build_states(progress):
            snapshot[state.record_id] = dict(
                state.airtable_fields(),
                **{at.F_PROF_WARMUP_RUNS_DONE: 0,
                   at.F_PROF_WARMUP_LAST_RUN: "2026-07-01T00:00:00.000Z",
                   at.F_PROF_WARMUP_LAST_RESULT: "Failed"})
        air = FakeAirtable(snapshot)
        result = warmup_state.sync_warmup_state(air, progress, max_stage_changes=10)
        self.assertEqual(len(air.patched), 40)
        self.assertEqual(result.errors, [])

    def test_the_default_limit_is_the_measured_one(self):
        """Nine real stage corrections on the live base; ten lets them through
        without a flag and stops anything an order of magnitude larger."""
        self.assertEqual(warmup_state.MAX_STAGE_CHANGES_DEFAULT, 10)
        progress, snapshot, _items = _fleet(11)
        result = warmup_state.sync_warmup_state(FakeAirtable(snapshot), progress)
        self.assertEqual(len(result.errors), 1)


class OwnedTagsTest(unittest.TestCase):
    def test_the_owned_set_is_exactly_what_the_workspace_already_has(self):
        self.assertEqual(warmup_state.owned_tags(4), [
            "Warming up process not startet", "Warmup Day 1 Done", "Warmup Day 2 Done",
            "Warmup Day 3 Done", "Warmup Day 4 Done", "Warmup ready, need Bio and Pic"])

    def test_created_is_not_owned(self):
        self.assertNotIn("created", {t.lower() for t in warmup_state.owned_tags(4)})


if __name__ == "__main__":
    unittest.main()


class UnreadableSnapshotTest(unittest.TestCase):
    """A snapshot that could not be *read* is not a base with nothing to read.

    Both end up as `None`, and the difference decides whether this pass may
    write. Every row "differs" from a missing snapshot -- correct when the
    columns merely do not exist yet, since the first patch is what creates them
    -- while the breaker, which counts stages that move, sees no current stage
    anywhere and reports nothing moving. So on a snapshot read that *failed*,
    the guard would go quiet on exactly the pass it cannot measure: one Airtable
    429 would relabel the whole fleet and strip its MultiLogin day tags on an
    unattended 30-minute timer, and the next sweep would then refuse to put them
    back because 46 stages want to move at once.
    """

    class Exploding(FakeAirtable):
        def warmup_state_snapshot(inner):
            raise RuntimeError("429 Too Many Requests")

    def test_a_failed_snapshot_read_writes_nothing(self):
        progress, _snapshot, items = _fleet(40)
        air, tags = self.Exploding(), FakeTags()
        result = warmup_state.sync_warmup_state(
            air, progress, tag_client=tags, mlx_items=items)
        self.assertEqual((air.patched, tags.calls), ([], []))
        self.assertEqual(result.airtable_written, 0)

    def test_it_says_why_it_refused(self):
        progress, _snapshot, items = _fleet(40)
        result = warmup_state.sync_warmup_state(
            self.Exploding(), progress, tag_client=FakeTags(), mlx_items=items)
        self.assertTrue(any("refused to write" in e for e in result.errors))
        self.assertTrue(any("429" in e for e in result.errors))

    def test_a_dry_run_still_reports(self):
        """A dry run writes nothing anyway, and refusing to *report* would take
        away the one thing that says what is wrong."""
        progress, _snapshot, items = _fleet(40)
        result = warmup_state.sync_warmup_state(
            self.Exploding(), progress, tag_client=FakeTags(), mlx_items=items,
            dry_run=True)
        self.assertEqual(len(result.changes), 40)
