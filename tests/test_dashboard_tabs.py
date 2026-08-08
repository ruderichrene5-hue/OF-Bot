"""The three tabs the VAs work from: Needs human, Posts, Profiles.

What these pin is the *filing*, not the formatting. The restructure exists
because one tab was doing three jobs at once — a worklist, an inventory and a
pile of dead queue rows — and the failure mode of getting it wrong is quiet:
work that nothing shows sits undone, and a count that includes wreckage tells
somebody there are 72 things to do when there are 22.
"""

import unittest

from adb_bot.automation import report, report_html


def _profile(name="Blank (5)", serial="262894", status="Active", needs_human=False,
             queue_rows=0, accounts=0, bio=False, picture=False, first_post=False,
             stage="", day=None, launch_id="L1", record_id="rec1"):
    from adb_bot.clients import airtable as at
    return {"record_id": record_id, "name": name, "serial": serial, "launch_id": launch_id,
            "status": status, "needs_human": needs_human, "reason": "", "note": [],
            "flagged_at": "", "warmup_started": "2026-08-01", "warmup_day": day,
            "warmup_stage": stage, "warmup_last_run": "2026-08-08 08:50",
            "has_second": False, "accounts": accounts, "queue_rows": queue_rows,
            "handoff": {at.F_PROF_BIO_DONE: bio, at.F_PROF_PICTURE_DONE: picture,
                        at.F_PROF_FIRST_POST_DONE: first_post}}


def _progress(*serials, plan_days=4, day_done=4, day=5):
    return {"plan_days": plan_days,
            "profiles": [{"serial": s, "name": f"p{s}", "day": day, "day_done": day_done,
                          "last_at": "2026-08-08 08:50"} for s in serials]}


class HandoffQueueTest(unittest.TestCase):
    """Profiles off the warm-up, waiting on a bio, a picture and a first post.

    The only group here that is not a repair — and the one nothing used to ask
    for, so a phone could sit finished for a week with every page green.
    """

    def test_a_finished_profile_with_nothing_done_is_listed(self):
        out = report.handoff_queue([_profile()], _progress("262894"))
        self.assertEqual(len(out["profiles"]), 1)
        self.assertEqual(out["profiles"][0]["outstanding"],
                         ["bio", "profile picture", "first post"])

    def test_a_profile_still_warming_is_not_listed(self):
        out = report.handoff_queue([_profile()], _progress("262894", day_done=2))
        self.assertEqual(out["profiles"], [])

    def test_all_three_ticked_drops_it_off_and_counts_it_done(self):
        out = report.handoff_queue(
            [_profile(bio=True, picture=True, first_post=True)], _progress("262894"))
        self.assertEqual(out["profiles"], [])
        self.assertEqual(out["done"], 1)

    def test_a_half_finished_profile_shows_both_halves(self):
        out = report.handoff_queue([_profile(bio=True)], _progress("262894"))
        row = out["profiles"][0]
        self.assertEqual(row["outstanding"], ["profile picture", "first post"])
        self.assertEqual(row["done_tasks"], ["bio"])

    def test_untouched_profiles_come_before_started_ones(self):
        """A half-done profile is somebody's open errand; an untouched one is
        nobody's yet, so it is the one that needs picking up."""
        profiles = [_profile(name="started", serial="1", bio=True, picture=True),
                    _profile(name="untouched", serial="2")]
        out = report.handoff_queue(profiles, _progress("1", "2"))
        self.assertEqual([p["name"] for p in out["profiles"]], ["untouched", "started"])

    def test_the_airtable_stage_alone_is_enough(self):
        """The Stage is written by a loop that may not have run for half an
        hour. A profile that finished since then must still appear."""
        from adb_bot.automation import warmup_state
        out = report.handoff_queue([_profile(stage=warmup_state.TAG_FINISHED)],
                                   {"plan_days": 4, "profiles": []})
        self.assertEqual(len(out["profiles"]), 1)

    def test_an_unreadable_plan_lists_nobody(self):
        """plan_days 0 means the Warmup Plan table would not read. Telling a VA
        twenty profiles are ready off a plan of unknown length is worse than
        telling them nothing."""
        out = report.handoff_queue([_profile()], _progress("262894", plan_days=0))
        self.assertEqual(out["profiles"], [])


class ClassifyProfileTest(unittest.TestCase):
    """Each phone lands in exactly one column, and the order is the precedence."""

    def _stage(self, profile, warming=("262894",), finished=()):
        return report.classify_profile(profile, warming=set(warming), finished=set(finished))

    def test_a_flagged_phone_outranks_the_fact_it_is_posting(self):
        """It is on somebody's worklist. Counting it as healthy is how it stays
        there."""
        self.assertEqual(self._stage(_profile(needs_human=True, queue_rows=6)),
                         "needs_person")

    def test_a_parked_phone_is_not_counted_as_anything_else(self):
        self.assertEqual(self._stage(_profile(status="Inactive", queue_rows=6)), "parked")

    def test_a_phone_with_queue_rows_is_posting(self):
        self.assertEqual(self._stage(_profile(queue_rows=3)), "posting")

    def test_a_finished_phone_with_work_outstanding_is_a_hand_off(self):
        self.assertEqual(self._stage(_profile(), finished=("262894",)), "handoff")

    def test_a_finished_phone_with_the_work_done_is_ready(self):
        self.assertEqual(
            self._stage(_profile(bio=True, picture=True, first_post=True),
                        finished=("262894",)), "ready")

    def test_a_tagged_phone_still_in_the_plan_is_warming(self):
        self.assertEqual(self._stage(_profile()), "warming")

    def test_an_untagged_idle_phone_is_other(self):
        self.assertEqual(self._stage(_profile(serial="999"), warming=()), "other")


class FolderBreakdownTest(unittest.TestCase):
    FOLDERS = {"f1": "Jasmin", "f2": "Nikki"}

    def _items(self, *pairs):
        return [{"serial_no": s, "serial_name": f"Blank ({s})", "id": f"L{s}",
                 "folder_id": f, "tags": ["Created"]} for s, f in pairs]

    def test_it_groups_by_the_multilogin_folder(self):
        out = report.folder_breakdown(
            [_profile(serial="1", queue_rows=2), _profile(serial="2", queue_rows=1)],
            mlx_items=self._items(("1", "f1"), ("2", "f2")), folder_names=self.FOLDERS)
        self.assertEqual({f["folder"] for f in out["folders"]}, {"Jasmin", "Nikki"})
        self.assertEqual(out["totals"]["posting"], 2)

    def test_a_phone_mlx_no_longer_has_gets_its_own_bucket(self):
        """Rather than being silently counted under a folder it is not in."""
        out = report.folder_breakdown([_profile(serial="404")],
                                      mlx_items=[], folder_names=self.FOLDERS)
        self.assertEqual(out["folders"][0]["folder"], "(no MultiLogin folder)")

    def test_every_phone_is_counted_exactly_once(self):
        profiles = [_profile(serial="1", queue_rows=2), _profile(serial="2", needs_human=True),
                    _profile(serial="3", status="Inactive"), _profile(serial="4")]
        out = report.folder_breakdown(
            profiles, mlx_items=self._items(*[(s, "f1") for s in "1234"]),
            folder_names=self.FOLDERS,
            warmup_progress=_progress("4", day_done=1))
        row = out["folders"][0]
        self.assertEqual(row["total"], 4)
        self.assertEqual(sum(row[key] for key in report.STAGE_ORDER), 4)

    def test_the_totals_row_adds_the_folders_up(self):
        out = report.folder_breakdown(
            [_profile(serial="1", queue_rows=1), _profile(serial="2", queue_rows=1)],
            mlx_items=self._items(("1", "f1"), ("2", "f2")), folder_names=self.FOLDERS)
        self.assertEqual(out["totals"]["total"], 2)

    def test_no_folder_list_still_counts_correctly(self):
        """MultiLogin can be down. The grouping is lost; the numbers are not."""
        out = report.folder_breakdown([_profile(serial="1", queue_rows=1)],
                                      mlx_items=self._items(("1", "f1")), folder_names={})
        self.assertEqual(out["totals"]["posting"], 1)
        self.assertEqual(out["known_folders"], 0)


def _queue_row(name="Jasmin 5 / 09:00", when="2026-08-08T09:00:00.000Z", status="Pending",
               variant="v1", handle=None, slot=None, issue=None, retries=0):
    fields = {"Name": name, "Scheduled DateTime": when, "Post Status": status,
              "Retry Count": retries}
    if variant:
        fields["Spoof Variant"] = [variant]
    if handle:
        fields["Target IG Handle"] = handle
    if slot:
        fields["Account Slot"] = slot
    if issue:
        fields["Issue Type"] = issue
    return {"id": "q1", "fields": fields}


VARIANTS = {"v1": {"file_path": "/data/spoofed/jasmin/clip_a.mp4"},
            "v2": {"file_path": "/data/spoofed/nikki/clip_b.mp4"}}


class TodaysPostsTest(unittest.TestCase):
    DAY = "2026-08-08"

    def _posts(self, *rows, **kw):
        return report.todays_posts(list(rows), self.DAY, variants=VARIANTS, **kw)

    def test_it_names_the_clip_and_the_profile(self):
        row = self._posts(_queue_row())["posts"][0]
        self.assertEqual((row["profile"], row["clip"], row["when"]),
                         ("Jasmin 5", "clip_a.mp4", "09:00"))

    def test_yesterdays_rows_are_not_todays_posts(self):
        self.assertEqual(self._posts(_queue_row(when="2026-08-07T09:00:00.000Z"))["total"], 0)

    def test_posts_are_ordered_by_when_they_are_due(self):
        out = self._posts(_queue_row(name="B / 14:00", when="2026-08-08T14:00:00.000Z"),
                          _queue_row(name="A / 09:00"))
        self.assertEqual([p["when"] for p in out["posts"]], ["09:00", "14:00"])

    def test_it_tallies_per_profile(self):
        out = self._posts(_queue_row(name="Jasmin 5 / 09:00", status="Posted"),
                          _queue_row(name="Jasmin 5 / 14:00", when="2026-08-08T14:00:00.000Z"),
                          _queue_row(name="Nikki 7 / 09:00", status="Failed"))
        busiest = out["by_profile"][0]
        self.assertEqual((busiest["profile"], busiest["total"], busiest["posted"]),
                         ("Jasmin 5", 2, 1))

    def test_a_second_account_row_carries_its_handle(self):
        row = self._posts(_queue_row(handle="@naughty_jasminn", slot="Second"))["posts"][0]
        self.assertEqual(row["handle"], "naughty_jasminn")
        self.assertEqual(row["slot"], "Second")

    def test_one_clip_on_two_profiles_is_called_out(self):
        """The failure the whole spoof pipeline exists to prevent, and one no
        status count can show."""
        out = self._posts(_queue_row(name="Jasmin 5 / 09:00"),
                          _queue_row(name="Nikki 7 / 09:00"))
        self.assertEqual(out["reused_clips"],
                         [{"clip": "clip_a.mp4", "profiles": ["Jasmin 5", "Nikki 7"]}])

    def test_the_same_clip_twice_on_one_profile_is_not_reuse(self):
        out = self._posts(_queue_row(name="Jasmin 5 / 09:00"),
                          _queue_row(name="Jasmin 5 / 14:00", when="2026-08-08T14:00:00.000Z"))
        self.assertEqual(out["reused_clips"], [])

    def test_distinct_clips_are_counted_once_each(self):
        out = self._posts(_queue_row(name="A / 09:00", variant="v1"),
                          _queue_row(name="B / 09:00", variant="v2"))
        self.assertEqual(out["clips"], 2)

    def test_the_variant_table_is_not_read_on_an_empty_day(self):
        """A full read of Spoof Variants is ~630 records, and most renders are
        idle. Paying for it to answer "nothing" is the whole cost of this tab."""
        calls = []

        def variants_fn():
            calls.append(1)
            return VARIANTS

        report.todays_posts([_queue_row(when="2026-08-01T09:00:00.000Z")], self.DAY,
                            variants=None, variants_fn=variants_fn)
        self.assertEqual(calls, [])
        report.todays_posts([_queue_row()], self.DAY, variants=None, variants_fn=variants_fn)
        self.assertEqual(len(calls), 1)

    def test_a_variant_read_that_fails_still_lists_the_posts(self):
        def variants_fn():
            raise RuntimeError("429")

        out = report.todays_posts([_queue_row()], self.DAY, variants=None,
                                  variants_fn=variants_fn)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["posts"][0]["clip"], "")


class HandoffRenderTest(unittest.TestCase):
    def _render(self, **kw):
        base = {"profiles": [], "done": 0, "plan_days": 4}
        base.update(kw)
        return report_html._section_handoff(base)

    def _row(self, **kw):
        row = {"name": "Blank (5)", "serial": "262894", "launch_id": "L1", "status": "Active",
               "day": 5, "finished_at": "2026-08-08 08:50",
               "outstanding": ["bio", "first post"], "done_tasks": ["profile picture"]}
        row.update(kw)
        return row

    def test_it_names_the_tasks_and_the_boxes_to_tick(self):
        page = self._render(profiles=[self._row()])
        self.assertIn("262894", page)
        self.assertIn("bio", page)
        self.assertIn("Bio Done", page)
        self.assertIn("First Post Done", page)

    def test_it_says_why_a_person_has_to_do_the_first_post(self):
        self.assertIn("first ever post is an", self._render(profiles=[self._row()]))

    def test_an_empty_list_distinguishes_none_yet_from_all_done(self):
        self.assertIn("No profile has finished its warm-up", self._render())
        self.assertIn("have had their bio", self._render(done=3))

    def test_a_profile_name_is_escaped(self):
        page = self._render(profiles=[self._row(name="<script>x</script>")])
        self.assertNotIn("<script>x</script>", page)


class PostsRenderTest(unittest.TestCase):
    def _render(self, **kw):
        base = {"posts": [], "by_status": {}, "by_profile": [], "total": 0, "clips": 0,
                "day": "2026-08-08", "reused_clips": []}
        base.update(kw)
        return report_html._section_posts_today(base)

    def _post(self, **kw):
        post = {"name": "Jasmin 5 / 09:00", "profile": "Jasmin 5", "when": "09:00",
                "status": "Pending", "clip": "clip_a.mp4", "handle": "", "slot": "",
                "issue": "", "retries": 0}
        post.update(kw)
        return post

    def test_it_shows_the_clip_against_the_profile(self):
        page = self._render(posts=[self._post()], total=1, clips=1,
                            by_profile=[{"profile": "Jasmin 5", "total": 1, "posted": 0,
                                         "failed": 0, "pending": 1, "verifying": 0}])
        self.assertIn("clip_a.mp4", page)
        self.assertIn("Jasmin 5", page)

    def test_an_empty_day_says_so_rather_than_showing_a_blank_table(self):
        self.assertIn("No posts are scheduled", self._render())

    def test_a_reused_clip_is_the_loudest_thing_on_the_tab(self):
        page = self._render(posts=[self._post()], total=1,
                            reused_clips=[{"clip": "clip_a.mp4",
                                           "profiles": ["Jasmin 5", "Nikki 7"]}])
        self.assertIn("pill bad", page)
        self.assertIn("more than one", page)
        self.assertIn("gets them flagged", page)

    def test_a_second_account_row_says_which_account(self):
        page = self._render(posts=[self._post(handle="naughty_jasminn", slot="Second")],
                            total=1)
        self.assertIn("naughty_jasminn", page)

    def test_a_clip_name_is_escaped(self):
        page = self._render(posts=[self._post(clip="<script>x</script>")], total=1)
        self.assertNotIn("<script>x</script>", page)


class FolderRenderTest(unittest.TestCase):
    def _render(self, **kw):
        base = {"folders": [], "totals": {}, "known_folders": 2, "error": ""}
        base.update(kw)
        return report_html._section_folders(base)

    def _folder(self, name="Jasmin", **kw):
        row = {"folder": name, "total": 3, **{key: 0 for key in report.STAGE_ORDER}}
        row.update(kw)
        return row

    def test_it_names_every_column_a_phone_can_be_in(self):
        page = self._render(folders=[self._folder(posting=3)],
                            totals=self._folder("All folders", posting=3))
        for label in report.STAGE_LABELS.values():
            self.assertIn(label, page)

    def test_a_missing_folder_list_is_called_out_but_the_counts_stand(self):
        page = self._render(folders=[self._folder(posting=3)],
                            totals=self._folder("All folders", posting=3), known_folders=0)
        self.assertIn("no folder list", page)
        self.assertIn("only the grouping is missing", page)

    def test_an_airtable_failure_is_shown_instead_of_an_empty_table(self):
        self.assertIn("429", self._render(error="RuntimeError: 429"))

    def test_a_folder_name_is_escaped(self):
        page = self._render(folders=[self._folder("<script>x</script>")],
                            totals=self._folder("All folders"))
        self.assertNotIn("<script>x</script>", page)


class AbandonedRenderTest(unittest.TestCase):
    def _render(self, rows=(), error=""):
        return report_html._section_abandoned(
            {"needs_human": {"rows": list(rows), "retrying": [], "profiles": [],
                             "error": error}})

    def test_it_points_at_the_profile_not_the_rows(self):
        page = self._render([{"name": "Laila 9 / 19:00", "slot": "2026-08-05 17:00",
                              "issue": "Retries Exhausted", "retries": 3}])
        self.assertIn("Laila 9", page)
        self.assertIn("Needs human", page)

    def test_nothing_abandoned_reads_as_healthy(self):
        self.assertIn("No abandoned posts", self._render())

    def test_an_airtable_failure_is_shown(self):
        self.assertIn("429", self._render(error="RuntimeError: 429"))


if __name__ == "__main__":
    unittest.main()


class BannerTest(unittest.TestCase):
    """The top of the page carries more than one kind of problem at once."""

    def _data(self, **kw):
        from tests.test_report import RenderTest
        return RenderTest()._data(**kw)

    def test_a_sick_loop_does_not_hide_the_worklist(self):
        """These used to be one slot: `banner = ...` on the health branch, so on
        any day a loop was unhappy the profiles waiting on somebody vanished
        from the top of the page."""
        data = self._data(
            needs_human={"rows": [], "retrying": [], "error": "",
                         "profiles": [{"name": "Jil 1", "reason": "Banned / Blocked",
                                       "status": "Active", "flagged_at": "", "note": []}]},
            health={"loops": [], "bad": [{"loop": "posting"}]})
        page = report_html.render(data)
        self.assertIn("need a person", page)
        self.assertIn("needs attention", page)
