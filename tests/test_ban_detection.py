from unittest import TestCase

from adb_bot.clients import airtable as at
from adb_bot.automation import ban_detection, incidents
from adb_bot.automation.ban_detection import (
    KIND_ACTION_BLOCK,
    KIND_BANNED,
    KIND_HUMAN_VERIFICATION,
    classify_block_text,
    mapping_for,
)
from adb_bot.automation.airtable_runner import _map_terminal_status
from adb_bot.automation.flows import interruptions


class ClassifyTest(TestCase):
    def test_banned_screens(self):
        for text in (
            "Your account has been suspended",
            "We suspended your account for not following our Community Guidelines",
            "Your account has been disabled",
            "your account has been permanently disabled",
        ):
            self.assertEqual(classify_block_text(text), KIND_BANNED, text)

    def test_human_verification_screens(self):
        for text in (
            "Confirm you're human",
            "We detected unusual activity",
            "Help us confirm it's you",
            "verify it's you",
        ):
            self.assertEqual(classify_block_text(text), KIND_HUMAN_VERIFICATION, text)

    def test_action_block_screens(self):
        for text in (
            "Action Blocked. Try Again Later.",
            "We restrict certain activity to protect our community",
            "You're temporarily blocked",
        ):
            self.assertEqual(classify_block_text(text), KIND_ACTION_BLOCK, text)

    def test_no_match(self):
        self.assertIsNone(classify_block_text("Your post was shared"))
        self.assertIsNone(classify_block_text(""))
        self.assertIsNone(classify_block_text(None))

    def test_case_insensitive(self):
        self.assertEqual(classify_block_text("ACTION BLOCKED"), KIND_ACTION_BLOCK)

    def test_precedence_banned_over_others(self):
        # A screen mentioning both a suspension and a verification prompt -> banned.
        text = "Your account has been suspended. Confirm you're human to appeal."
        self.assertEqual(classify_block_text(text), KIND_BANNED)

    def test_works_on_raw_hierarchy_xml(self):
        # The u2 path feeds dump_hierarchy() XML straight in.
        xml = '<node text="Action Blocked" resource-id="foo"/><node text="Try again later"/>'
        self.assertEqual(classify_block_text(xml), KIND_ACTION_BLOCK)


class MappingTest(TestCase):
    def test_banned_mapping(self):
        m = mapping_for(KIND_BANNED)
        self.assertEqual(m.lifecycle_stage, at.STAGE_BANNED)
        self.assertFalse(m.needs_verification)
        self.assertEqual(m.event_type, at.EVENT_FULL_BAN)
        self.assertEqual(m.issue_type, at.ISSUE_BANNED_BLOCKED)

    def test_verification_mapping(self):
        m = mapping_for(KIND_HUMAN_VERIFICATION)
        self.assertIsNone(m.lifecycle_stage)
        self.assertTrue(m.needs_verification)
        self.assertEqual(m.event_type, at.EVENT_WARNING)
        self.assertEqual(m.issue_type, at.ISSUE_HUMAN_VERIFICATION)

    def test_action_block_mapping(self):
        m = mapping_for(KIND_ACTION_BLOCK)
        self.assertIsNone(m.lifecycle_stage)  # temporary -> don't change stage
        self.assertFalse(m.needs_verification)
        self.assertEqual(m.event_type, at.EVENT_ACTION_BLOCK)

    def test_unknown_kind(self):
        self.assertIsNone(mapping_for(None))
        self.assertIsNone(mapping_for("nonsense"))


class FakeIncidentClient:
    def __init__(self):
        self.flag_calls = []
        self.history_calls = []
        self.queue_calls = []

    def flag_account(self, account_id, *, lifecycle_stage=None, needs_verification=None, ban_notes=None):
        self.flag_calls.append((account_id, lifecycle_stage, needs_verification, ban_notes))
        return True

    def create_ban_flag_history(self, account_id, event_type, notes=None):
        self.history_calls.append((account_id, event_type, notes))
        return "recHist"

    def set_posting_queue_issue(self, queue_record_id, issue_type, post_status=at.POST_STATUS_FAILED):
        self.queue_calls.append((queue_record_id, issue_type, post_status))
        return True


class ApplyIncidentTest(TestCase):
    def test_banned_sets_stage_and_history(self):
        c = FakeIncidentClient()
        m = incidents.apply_account_incident(c, "recAcc", "warm_up_process", KIND_BANNED)
        self.assertEqual(m.kind, KIND_BANNED)
        acc, stage, needs, notes = c.flag_calls[0]
        self.assertEqual(stage, at.STAGE_BANNED)
        self.assertIsNone(needs)  # never write False -> stays None
        self.assertIn("banned", notes)
        self.assertEqual(c.history_calls[0][:2], ("recAcc", at.EVENT_FULL_BAN))

    def test_verification_ticks_checkbox_only(self):
        c = FakeIncidentClient()
        incidents.apply_account_incident(c, "recAcc", "update_bio_u2", KIND_HUMAN_VERIFICATION)
        _acc, stage, needs, _notes = c.flag_calls[0]
        self.assertIsNone(stage)          # lifecycle untouched
        self.assertTrue(needs)            # checkbox set
        self.assertEqual(c.history_calls[0][1], at.EVENT_WARNING)

    def test_action_block_history_only_no_stage(self):
        c = FakeIncidentClient()
        incidents.apply_account_incident(c, "recAcc", "instagram_scroll", KIND_ACTION_BLOCK)
        _acc, stage, needs, _notes = c.flag_calls[0]
        self.assertIsNone(stage)
        self.assertIsNone(needs)
        self.assertEqual(c.history_calls[0][1], at.EVENT_ACTION_BLOCK)

    def test_queue_issue_written_when_record_given(self):
        c = FakeIncidentClient()
        incidents.apply_account_incident(c, "recAcc", "post", KIND_BANNED, queue_record_id="recQ")
        self.assertEqual(c.queue_calls[0], ("recQ", at.ISSUE_BANNED_BLOCKED, at.POST_STATUS_FAILED))

    def test_no_queue_write_without_record(self):
        c = FakeIncidentClient()
        incidents.apply_account_incident(c, "recAcc", "post", KIND_BANNED)
        self.assertEqual(c.queue_calls, [])

    def test_unknown_kind_writes_nothing(self):
        c = FakeIncidentClient()
        self.assertIsNone(incidents.apply_account_incident(c, "recAcc", "post", "weird"))
        self.assertEqual(c.flag_calls, [])
        self.assertEqual(c.history_calls, [])


class RunnerMappingTest(TestCase):
    def test_flag_statuses_carry_incident_kind(self):
        self.assertEqual(_map_terminal_status("banned")[2], KIND_BANNED)
        self.assertEqual(_map_terminal_status("human_verification")[2], KIND_HUMAN_VERIFICATION)
        self.assertEqual(_map_terminal_status("action_block")[2], KIND_ACTION_BLOCK)

    def test_plain_statuses_have_no_incident(self):
        self.assertIsNone(_map_terminal_status("done")[2])
        self.assertIsNone(_map_terminal_status("failed")[2])
        self.assertIsNone(_map_terminal_status("running"))


class InterruptionsOutcomeTest(TestCase):
    def test_account_flag_for(self):
        self.assertEqual(interruptions.account_flag_for(interruptions.OUTCOME_ACCOUNT_BANNED), KIND_BANNED)
        self.assertEqual(interruptions.account_flag_for(interruptions.OUTCOME_HUMAN_VERIFICATION), KIND_HUMAN_VERIFICATION)
        self.assertEqual(interruptions.account_flag_for(interruptions.OUTCOME_ACTION_BLOCK), KIND_ACTION_BLOCK)
        self.assertIsNone(interruptions.account_flag_for(interruptions.OUTCOME_HANDLED))
        self.assertIsNone(interruptions.account_flag_for(interruptions.OUTCOME_NONE))
