from datetime import datetime
from unittest import TestCase

from adb_bot.clients import airtable as at
from adb_bot.automation.posting_planner import plan_posting_queue, PostingItem
from adb_bot.automation.posting_runner import _map_post_status, apply_post_result


NOW = datetime(2026, 7, 28, 12, 0, 0)


def queue_row(rec_id="recQ1", account="recAcc1", variant="recVar1", caption="recCap1",
              status="Pending", scheduled="2026-07-28T09:00:00.000Z", retry=0, name="Post 1"):
    fields = {at.F_PQ_NAME: name, at.F_PQ_POST_STATUS: status}
    if scheduled is not None:
        fields[at.F_PQ_SCHEDULED] = scheduled
    if account is not None:
        fields[at.F_PQ_TARGET_ACCOUNT] = [account]
    if variant is not None:
        fields[at.F_PQ_SPOOF_VARIANT] = [variant]
    if caption is not None:
        fields[at.F_PQ_CAPTION] = [caption]
    if retry:
        fields[at.F_PQ_RETRY_COUNT] = retry
    return {"id": rec_id, "fields": fields}


def base_lookups(**overrides):
    accounts = {"recAcc1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["recProf1"],
                            at.F_ACC_LIFECYCLE_STAGE: "Active"}}
    profiles = {"recProf1": {"launch_id": "624354174112432228", "name": "Nikki 1"}}
    variants = {"recVar1": {"file_path": "C:/spoofed/nikki_1/v1.mp4", "status": "Ready"}}
    captions = {"recCap1": "hello world"}
    data = {"accounts": accounts, "profiles": profiles, "variants": variants, "captions": captions}
    data.update(overrides)
    return data


class PlanPostingTest(TestCase):
    def _plan(self, rows, **lk):
        d = base_lookups(**lk)
        return plan_posting_queue(rows, d["accounts"], d["profiles"], d["variants"], d["captions"], now=NOW)

    def test_due_post_is_planned(self):
        plan = self._plan([queue_row()])
        self.assertEqual(len(plan.to_post), 1)
        item = plan.to_post[0]
        self.assertEqual(item.launch_id, "624354174112432228")
        self.assertEqual(item.video_path, "C:/spoofed/nikki_1/v1.mp4")
        self.assertEqual(item.caption, "hello world")

    def test_future_scheduled_is_not_due(self):
        plan = self._plan([queue_row(scheduled="2026-07-28T18:00:00.000Z")])
        self.assertEqual(plan.to_post, [])

    def test_missing_schedule_is_due_now(self):
        plan = self._plan([queue_row(scheduled=None)])
        self.assertEqual(len(plan.to_post), 1)

    def test_needs_verification_skipped(self):
        accts = {"recAcc1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["recProf1"],
                             at.F_ACC_LIFECYCLE_STAGE: "Active", at.F_ACC_NEEDS_VERIFICATION: True}}
        plan = self._plan([queue_row()], accounts=accts)
        self.assertEqual(plan.to_post, [])
        self.assertIn("verification", plan.skipped[0].reason)

    def test_banned_stage_skipped(self):
        accts = {"recAcc1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["recProf1"],
                             at.F_ACC_LIFECYCLE_STAGE: "Banned"}}
        plan = self._plan([queue_row()], accounts=accts)
        self.assertEqual(plan.to_post, [])
        self.assertIn("Banned", plan.skipped[0].reason)

    def test_no_launch_id_skipped(self):
        plan = self._plan([queue_row()], profiles={"recProf1": {"launch_id": None, "name": "Nikki 1"}})
        self.assertIn("MLX API ID", plan.skipped[0].reason)

    def test_no_video_path_skipped(self):
        plan = self._plan([queue_row()], variants={"recVar1": {"file_path": None, "status": "Ready"}})
        self.assertIn("Spoof Variant", plan.skipped[0].reason)

    def test_missing_target_account_skipped(self):
        plan = self._plan([queue_row(account=None)])
        self.assertIn("Target Account", plan.skipped[0].reason)

    def test_caption_optional(self):
        plan = self._plan([queue_row(caption=None)])
        self.assertEqual(len(plan.to_post), 1)
        self.assertIsNone(plan.to_post[0].caption)

    def test_selected_launch_ids_filters(self):
        d = base_lookups()
        plan = plan_posting_queue([queue_row()], d["accounts"], d["profiles"], d["variants"],
                                  d["captions"], now=NOW, selected_launch_ids={"other"})
        self.assertEqual(plan.to_post, [])


class FakePostClient:
    def __init__(self):
        self.run_logs, self.acc_results, self.post_marks, self.used, self.incidents_q = [], [], [], [], []

    def create_run_log(self, account_id, account_name, flow, result, notes=None):
        self.run_logs.append((account_id, flow, result, notes, account_name)); return "recLog"

    def set_account_result(self, account_id, last_result, needs_verification=None):
        self.acc_results.append((account_id, last_result)); return True

    def mark_post_result(self, queue_id, post_status, issue_type=None, retry_count=None):
        self.post_marks.append((queue_id, post_status, issue_type, retry_count)); return True

    def mark_variant_used(self, variant_id):
        self.used.append(variant_id); return True

    # used by incidents.apply_account_incident
    def flag_account(self, account_id, *, lifecycle_stage=None, needs_verification=None, ban_notes=None):
        return True

    def create_ban_flag_history(self, account_id, event_type, notes=None):
        return "recHist"

    def set_posting_queue_issue(self, queue_record_id, issue_type, post_status=at.POST_STATUS_FAILED):
        self.incidents_q.append((queue_record_id, issue_type, post_status)); return True


ITEM = PostingItem(queue_id="recQ1", account_id="recAcc1", account_name="nikki_1",
                   launch_id="LID", video_path="v.mp4", caption="c", variant_id="recVar1",
                   scheduled=None, retry_count=1)


PROFILE_ITEM = PostingItem(queue_id="recQ2", account_id=None, account_name="Jil 3",
                           launch_id="LID2", video_path="v2.mp4", caption=None,
                           variant_id="recVar2", scheduled=None, retry_count=0)


class PlanPostingByProfileTest(TestCase):
    """Rows targeting an MLX profile instead of an Account (targets='profiles')."""

    def _row(self, profile="recProf1", account=None):
        fields = {at.F_PQ_NAME: "Profile post", at.F_PQ_POST_STATUS: "Pending",
                  at.F_PQ_SPOOF_VARIANT: ["recVar1"]}
        if profile is not None:
            fields[at.F_PQ_TARGET_PROFILE] = [profile]
        if account is not None:
            fields[at.F_PQ_TARGET_ACCOUNT] = [account]
        return {"id": "recQ2", "fields": fields}

    def _plan(self, rows, **lk):
        d = base_lookups(**lk)
        return plan_posting_queue(rows, d["accounts"], d["profiles"], d["variants"], d["captions"], now=NOW)

    def test_profile_row_resolves_launch_id_without_an_account(self):
        plan = self._plan([self._row()])
        self.assertEqual(len(plan.to_post), 1)
        item = plan.to_post[0]
        self.assertEqual(item.launch_id, "624354174112432228")
        self.assertIsNone(item.account_id)
        self.assertEqual(item.account_name, "Nikki 1")   # profile name stands in

    def test_account_link_still_wins_when_both_are_set(self):
        """A row with both links follows the account path, guards included --
        otherwise a paused account could be posted to via its profile."""
        accounts = {"recAcc1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["recProf1"],
                                at.F_ACC_LIFECYCLE_STAGE: "Active",
                                at.F_ACC_AUTOMATION_MODE: at.MODE_PAUSED}}
        plan = self._plan([self._row(account="recAcc1")], accounts=accounts)
        self.assertEqual(plan.to_post, [])
        self.assertIn("paused", plan.skipped[0].reason)

    def test_neither_link_is_skipped(self):
        plan = self._plan([self._row(profile=None)])
        self.assertEqual(plan.to_post, [])
        self.assertIn("Target Profile", plan.skipped[0].reason)

    def test_profile_without_mlx_id_is_skipped(self):
        plan = self._plan([self._row()], profiles={"recProf1": {"launch_id": None, "name": "Nikki 1"}})
        self.assertEqual(plan.to_post, [])
        self.assertIn("no MLX API ID", plan.skipped[0].reason)


class ApplyPostResultTest(TestCase):
    def test_posted_marks_and_uses_variant(self):
        c = FakePostClient()
        self.assertTrue(apply_post_result(c, ITEM, "done"))
        self.assertEqual(c.post_marks[0][:2], ("recQ1", at.POST_STATUS_POSTED))
        self.assertEqual(c.used, ["recVar1"])
        self.assertEqual(c.run_logs[0][2], at.RESULT_DONE)

    def test_retryable_failure_bumps_retry(self):
        c = FakePostClient()
        apply_post_result(c, ITEM, "failed")
        qid, status, issue, retry = c.post_marks[0]
        self.assertEqual(status, at.POST_STATUS_FAILED)
        self.assertEqual(issue, at.ISSUE_NEEDS_RETRY)
        self.assertEqual(retry, 2)  # was 1
        self.assertEqual(c.used, [])

    def test_banned_routes_through_incident_no_retry_bump(self):
        c = FakePostClient()
        apply_post_result(c, ITEM, "banned")
        # incident path stamps the queue Issue Type; no plain mark_post_result retry bump
        self.assertEqual(c.incidents_q[0], ("recQ1", at.ISSUE_BANNED_BLOCKED, at.POST_STATUS_FAILED))
        self.assertEqual(c.post_marks, [])

    def test_verification_routes_through_incident(self):
        c = FakePostClient()
        apply_post_result(c, ITEM, "human_verification")
        self.assertEqual(c.incidents_q[0][1], at.ISSUE_HUMAN_VERIFICATION)

    def test_intermediate_status_no_writeback(self):
        c = FakePostClient()
        self.assertFalse(apply_post_result(c, ITEM, "running"))
        self.assertEqual(c.run_logs, [])

    def test_profile_item_writes_back_without_an_account(self):
        """A profile-driven post still records its outcome: the queue row and the
        variant are updated, and a Run Log row is written unlinked rather than
        dropped -- losing the record of a real run is worse than an unlinked one."""
        c = FakePostClient()
        self.assertTrue(apply_post_result(c, PROFILE_ITEM, "done"))
        self.assertEqual(c.post_marks[0][:2], ("recQ2", at.POST_STATUS_POSTED))
        self.assertEqual(c.used, ["recVar2"])
        self.assertIsNone(c.run_logs[0][0])           # no Account link
        self.assertEqual(c.run_logs[0][4], "Jil 3")   # profile name carries it

    def test_profile_item_incident_stamps_queue_without_flagging_an_account(self):
        c = FakePostClient()
        apply_post_result(c, PROFILE_ITEM, "banned")
        self.assertEqual(c.incidents_q, [])          # no account to flag
        qid, status, issue, retry = c.post_marks[0]
        self.assertEqual((qid, status, issue), ("recQ2", at.POST_STATUS_FAILED,
                                                at.ISSUE_BANNED_BLOCKED))
        self.assertIsNone(retry)                     # not retryable -- no bump

    def test_status_mapping_table(self):
        self.assertEqual(_map_post_status("done")[0], at.POST_STATUS_POSTED)
        self.assertEqual(_map_post_status("action_block")[2], "action_block")
        self.assertIsNone(_map_post_status("starting"))


class AlreadySharedTest(TestCase):
    """The ledger refusing a second send is a guard working, not a failure.

    Before this mapping the result fell through to the generic failed branch:
    the row landed on `Failed - Needs Retry` with the retry counter bumped, so a
    refusal to double-post was recorded as a breakage and queued for a retry
    that could never change the answer.
    """

    def test_it_is_terminal_and_not_retryable(self):
        post_status, issue_type, incident, run_result, note = _map_post_status("already_shared")
        self.assertEqual(post_status, at.POST_STATUS_FAILED)
        self.assertEqual(issue_type, at.ISSUE_OTHER)
        self.assertNotEqual(issue_type, at.ISSUE_NEEDS_RETRY)   # the retry pass must not pick it up
        self.assertIsNone(incident)                             # nothing is wrong with the account
        self.assertEqual(run_result, at.RESULT_SKIPPED)
        self.assertIn("already sent", note)

    def test_it_does_not_bump_the_retry_counter(self):
        c = FakePostClient()
        apply_post_result(c, ITEM, "already_shared")
        qid, status, issue, retry = c.post_marks[0]
        self.assertEqual((qid, status, issue), ("recQ1", at.POST_STATUS_FAILED, at.ISSUE_OTHER))
        self.assertIsNone(retry)   # a refusal is not an attempt

    def test_it_does_not_consume_the_variant(self):
        """A later recheck can disprove the original post, which makes the clip
        sendable again -- marking it Used here would throw that away."""
        c = FakePostClient()
        apply_post_result(c, ITEM, "already_shared")
        self.assertEqual(c.used, [])

    def test_the_retry_pass_will_not_requeue_it(self):
        from adb_bot.automation import retry_runner
        fields = {at.F_PQ_POST_STATUS: at.POST_STATUS_FAILED, at.F_PQ_ISSUE_TYPE: at.ISSUE_OTHER,
                  at.F_PQ_RETRY_COUNT: 0}
        outcome, _detail = retry_runner.decide_retry(fields, "LID", "hash", None)
        self.assertNotEqual(outcome, retry_runner.OUTCOME_RETRY)


class ProfileHealthGuardTest(TestCase):
    """A phone parked *after* its row went Pending must not still post.

    The filter that creates rows cannot reach a row that already exists, so
    this second check is the only thing standing between a freshly flagged
    phone and its outstanding slots -- a launch, a two-minute boot and an
    upload each. Through 2026-08-06 it was missing on the running branch: 22
    flagged profiles kept posting, Laila 3 burned 17 launches for 0 posts in a
    day, and Viktoria 3 was launched while flagged `Banned / Blocked`.

    Pinned here because the guard and the two-account support were built on
    different branches, and the first attempt to run one tree's posting loop
    would have silently dropped the other's fix.
    """

    ROW = {"id": "q1", "fields": {
        "Name": "Jil 5 / 18:00", "Post Status": "Pending",
        "Scheduled DateTime": "2026-08-07T10:00:00.000Z",
        "Target Profile": ["recP1"], "Spoof Variant": ["recV1"]}}

    def _plan(self, **profile):
        info = {"launch_id": "111", "name": "Jil 5", "needs_human": False,
                "status": "Active"}
        info.update(profile)
        return plan_posting_queue(
            [self.ROW], accounts_by_id={}, profiles_by_recid={"recP1": info},
            variants_by_id={"recV1": {"file_path": "/tmp/a.mp4"}}, captions_by_id={},
            now=datetime(2026, 8, 7, 12, 0))

    def test_a_healthy_profile_still_posts(self):
        self.assertEqual(len(self._plan().to_post), 1)

    def test_a_flagged_profile_is_dropped_even_though_its_row_is_pending(self):
        plan = self._plan(needs_human=True)
        self.assertEqual(plan.to_post, [])
        self.assertIn("needs a human check", plan.skipped[0].reason)

    def test_a_parked_profile_is_dropped(self):
        plan = self._plan(status="Inactive")
        self.assertEqual(plan.to_post, [])
        self.assertIn("Inactive", plan.skipped[0].reason)

    def test_a_base_with_no_status_field_is_not_read_as_parked(self):
        """`status=None` means the column was not read, not that the profile is
        parked. Treating the two the same would stop the whole fleet."""
        self.assertEqual(len(self._plan(status=None).to_post), 1)


class HandoffGateTest(TestCase):
    """A phone off the warm-up may not post until a person has set it up.

    The warm-up spends four days making a fresh account look used. An account
    whose first ever post is an automated reel is the one Instagram acts on, so
    posting the moment day 4 completes throws away the whole point of it on the
    last step.

    Scoped to the warm-up cohort by `warmup_started`, and that scoping is the
    part worth guarding: every profile posting today predates the field and has
    no start date, so a gate that ignored it would park the entire live fleet
    the day it shipped.
    """

    ROW = {"id": "q1", "fields": {
        "Name": "Blank (5) / 18:00", "Post Status": "Pending",
        "Scheduled DateTime": "2026-08-07T10:00:00.000Z",
        "Target Profile": ["recP1"], "Spoof Variant": ["recV1"]}}

    def _plan(self, **profile):
        info = {"launch_id": "111", "name": "Blank (5)", "needs_human": False,
                "status": "Active", "warmup_started": "2026-08-01",
                "handoff_outstanding": []}
        info.update(profile)
        return plan_posting_queue(
            [self.ROW], accounts_by_id={}, profiles_by_recid={"recP1": info},
            variants_by_id={"recV1": {"file_path": "/tmp/a.mp4"}}, captions_by_id={},
            now=datetime(2026, 8, 7, 12, 0))

    def test_a_warmed_profile_with_work_outstanding_does_not_post(self):
        plan = self._plan(handoff_outstanding=["bio", "first post"])
        self.assertEqual(plan.to_post, [])
        self.assertIn("waiting on a person", plan.skipped[0].reason)
        self.assertIn("bio", plan.skipped[0].reason)

    def test_one_task_left_is_still_a_hold(self):
        self.assertEqual(self._plan(handoff_outstanding=["first post"]).to_post, [])

    def test_all_three_ticked_releases_it(self):
        self.assertEqual(len(self._plan(handoff_outstanding=[]).to_post), 1)

    def test_a_profile_that_never_went_through_the_warm_up_is_untouched(self):
        """Every account posting today is one of these. If this gate reached
        them it would stop the fleet, which is why it keys on the start date
        rather than on the checkboxes alone."""
        plan = self._plan(warmup_started=None,
                          handoff_outstanding=["bio", "profile picture", "first post"])
        self.assertEqual(len(plan.to_post), 1)

    def test_a_base_without_the_fields_does_not_park_anything(self):
        """`profile_launch_map` on an older base returns neither key. Reading a
        missing column as "not done" is how a schema lag stops posting."""
        plan = plan_posting_queue(
            [self.ROW], accounts_by_id={},
            profiles_by_recid={"recP1": {"launch_id": "111", "name": "Blank (5)",
                                         "needs_human": False, "status": "Active"}},
            variants_by_id={"recV1": {"file_path": "/tmp/a.mp4"}}, captions_by_id={},
            now=datetime(2026, 8, 7, 12, 0))
        self.assertEqual(len(plan.to_post), 1)


class PostingWindowTest(TestCase):
    """Posts go out between 09:00 and 23:00 Berlin and at no other hour.

    The queue no longer schedules outside those hours, but rows arrive here from
    other routes -- they fell behind while the fleet was down, the retry pass
    moved one, a person re-dated a batch -- and "Scheduled DateTime has passed"
    on its own would fire them at 04:00.
    """

    def _plan_at(self, now):
        """One row, long overdue, so the only thing under test is the clock."""
        data = base_lookups()
        row = queue_row(scheduled="2026-01-01T00:00:00.000Z")
        return plan_posting_queue([row], data["accounts"], data["profiles"],
                                  data["variants"], data["captions"], now=now)

    def test_a_due_row_posts_inside_the_window(self):
        # 12:00 UTC is 14:00 Berlin in July.
        self.assertEqual(len(self._plan_at(datetime(2026, 7, 28, 12, 0)).to_post), 1)

    def test_an_overdue_row_does_not_go_out_at_four_in_the_morning(self):
        plan = self._plan_at(datetime(2026, 7, 28, 2, 0))     # 04:00 Berlin
        self.assertEqual(plan.to_post, [])
        self.assertIn("outside posting hours", plan.skipped[0].reason)

    def test_the_window_opens_at_nine_berlin(self):
        self.assertEqual(self._plan_at(datetime(2026, 7, 28, 6, 59)).to_post, [])   # 08:59
        self.assertEqual(len(self._plan_at(datetime(2026, 7, 28, 7, 0)).to_post), 1)  # 09:00

    def test_the_window_closes_at_eleven_berlin(self):
        self.assertEqual(len(self._plan_at(datetime(2026, 7, 28, 20, 59)).to_post), 1)  # 22:59
        self.assertEqual(self._plan_at(datetime(2026, 7, 28, 21, 0)).to_post, [])       # 23:00

    def test_the_window_is_berlin_wall_clock_not_utc(self):
        """Same UTC instant, both sides of the line: 07:30 UTC is 09:30 Berlin
        in August (open) and 08:30 in January (shut). Comparing against UTC
        would post an hour early all winter."""
        self.assertEqual(len(self._plan_at(datetime(2026, 8, 3, 7, 30)).to_post), 1)
        self.assertEqual(self._plan_at(datetime(2026, 1, 5, 7, 30)).to_post, [])
