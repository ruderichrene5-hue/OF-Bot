"""The daily re-ask: do the phones parked as `Human Verification Required`
really show a checkpoint?

A flag here stops an account posting until a person looks at it, and nothing
ever re-asked, so a flag raised by a misfire cost exactly as much as a real
checkpoint. On 2026-08-11, 17 of 29 flagged profiles carried this reason.

The asymmetry these tests are mostly about: clearing a flag needs *positive*
proof the account works, never merely the absence of checkpoint markers. A blank
screen, a phone that never booted and a failed dump all look like "no markers".
"""

from unittest import TestCase

from adb_bot.automation import ban_detection, verification_audit
from adb_bot.automation.verification_audit import (
    ProbeResult, VERDICT_LOOKS_CLEAR, VERDICT_STILL_BLOCKED, VERDICT_UNKNOWN,
    VERDICT_WRONG_REASON, audit_verification_flags, decide_verification,
)


class DecideVerificationTest(TestCase):
    def verdict(self, **kwargs):
        return decide_verification(ProbeResult(**kwargs))[0]

    def test_a_checkpoint_on_screen_keeps_the_flag(self):
        self.assertEqual(
            self.verdict(reachable=True,
                         screen_kind=ban_detection.KIND_HUMAN_VERIFICATION),
            VERDICT_STILL_BLOCKED)

    def test_a_clean_screen_with_a_rendered_profile_clears_it(self):
        self.assertEqual(
            self.verdict(reachable=True, screen_kind=None, post_count=41),
            VERDICT_LOOKS_CLEAR)

    def test_zero_posts_is_still_proof_the_profile_rendered(self):
        # A brand-new account has 0 posts. Treating that as "unreadable" would
        # keep every fresh profile parked forever.
        self.assertEqual(
            self.verdict(reachable=True, screen_kind=None, post_count=0),
            VERDICT_LOOKS_CLEAR)

    def test_a_clean_screen_with_no_post_count_is_unknown(self):
        """The heart of it. No markers is not proof of health -- it is also what
        a phone that never finished booting looks like."""
        self.assertEqual(
            self.verdict(reachable=True, screen_kind=None, post_count=None),
            VERDICT_UNKNOWN)

    def test_an_unreachable_phone_is_unknown(self):
        self.assertEqual(self.verdict(reachable=False), VERDICT_UNKNOWN)

    def test_a_banned_screen_is_the_wrong_reason_not_a_clear(self):
        self.assertEqual(
            self.verdict(reachable=True, screen_kind=ban_detection.KIND_BANNED),
            VERDICT_WRONG_REASON)

    def test_an_action_block_is_the_wrong_reason_too(self):
        # A throttle clears itself; nobody needs to tap through it.
        self.assertEqual(
            self.verdict(reachable=True, screen_kind=ban_detection.KIND_ACTION_BLOCK),
            VERDICT_WRONG_REASON)

    def test_a_checkpoint_outranks_a_readable_post_count(self):
        self.assertEqual(
            self.verdict(reachable=True, post_count=41,
                         screen_kind=ban_detection.KIND_HUMAN_VERIFICATION),
            VERDICT_STILL_BLOCKED)


class FakeAuditClient:
    def __init__(self, profiles, clear_fails=False):
        self._profiles = profiles
        self.cleared = []
        self.notes = []
        self.clear_fails = clear_fails

    def profiles_needing_verification(self):
        return list(self._profiles)

    def clear_profile_verification_flag(self, record_id, note, **kwargs):
        if self.clear_fails:
            return False
        self.cleared.append((record_id, note))
        return True

    def append_profile_note(self, record_id, note, **kwargs):
        self.notes.append((record_id, note))
        return True


def _profile(record_id="recP1", name="Nikki 12", launch_id="6257272675", **kwargs):
    out = {"record_id": record_id, "name": name, "launch_id": launch_id,
           "handle": "nikki_lat", "flagged_at": "2026-08-09T01:20:18.000Z"}
    out.update(kwargs)
    return out


class AuditRunTest(TestCase):
    def test_dry_run_writes_nothing_at_all(self):
        client = FakeAuditClient([_profile()])
        report = audit_verification_flags(
            client, lambda p: ProbeResult(reachable=True, post_count=41), dry_run=True)
        self.assertEqual(report.audited[0].verdict, VERDICT_LOOKS_CLEAR)
        self.assertEqual(client.cleared, [])
        self.assertEqual(client.notes, [])
        self.assertEqual(report.cleared, 0)

    def test_a_disproved_flag_is_cleared(self):
        client = FakeAuditClient([_profile()])
        report = audit_verification_flags(
            client, lambda p: ProbeResult(reachable=True, post_count=41), dry_run=False)
        self.assertEqual([rid for rid, _ in client.cleared], ["recP1"])
        self.assertEqual(report.cleared, 1)
        self.assertTrue(report.audited[0].cleared)

    def test_a_confirmed_checkpoint_gets_a_note_and_keeps_its_flag(self):
        """The note is the point of running this daily: 'seen again' three days
        running is what tells you the flag is real."""
        client = FakeAuditClient([_profile()])
        audit_verification_flags(
            client,
            lambda p: ProbeResult(reachable=True,
                                  screen_kind=ban_detection.KIND_HUMAN_VERIFICATION),
            dry_run=False)
        self.assertEqual(client.cleared, [])
        self.assertEqual(len(client.notes), 1)
        self.assertIn("really does need a person", client.notes[0][1])

    def test_an_unknown_never_clears(self):
        client = FakeAuditClient([_profile()])
        audit_verification_flags(
            client, lambda p: ProbeResult(reachable=False), dry_run=False)
        self.assertEqual(client.cleared, [])
        self.assertEqual(len(client.notes), 1)

    def test_a_probe_that_raises_is_unknown_not_a_clear(self):
        def boom(profile):
            raise RuntimeError("MLX said no")

        client = FakeAuditClient([_profile()])
        report = audit_verification_flags(client, boom, dry_run=False)
        self.assertEqual(report.audited[0].verdict, VERDICT_UNKNOWN)
        self.assertEqual(client.cleared, [])

    def test_a_profile_with_no_mlx_id_is_unknown_and_never_probed(self):
        probed = []
        client = FakeAuditClient([_profile(launch_id="")])
        report = audit_verification_flags(
            client, lambda p: probed.append(p) or ProbeResult(reachable=True, post_count=1),
            dry_run=False)
        self.assertEqual(report.audited[0].verdict, VERDICT_UNKNOWN)
        self.assertEqual(probed, [])
        self.assertEqual(client.cleared, [])

    def test_a_failed_clear_is_reported_not_silently_counted(self):
        client = FakeAuditClient([_profile()], clear_fails=True)
        report = audit_verification_flags(
            client, lambda p: ProbeResult(reachable=True, post_count=41), dry_run=False)
        self.assertEqual(report.cleared, 0)
        self.assertTrue(report.errors)

    def test_the_limit_caps_how_many_phones_one_pass_opens(self):
        opened = []
        client = FakeAuditClient([_profile(record_id=f"rec{i}") for i in range(5)])
        audit_verification_flags(
            client,
            lambda p: opened.append(p["record_id"]) or ProbeResult(reachable=True, post_count=1),
            dry_run=True, limit=2)
        self.assertEqual(opened, ["rec0", "rec1"])

    def test_an_airtable_failure_reports_rather_than_looking_healthy(self):
        class Broken:
            def profiles_needing_verification(self):
                raise RuntimeError("403")

        report = audit_verification_flags(Broken(), lambda p: ProbeResult(), dry_run=False)
        self.assertTrue(report.errors)
        self.assertEqual(report.audited, [])


class VisibleTextTest(TestCase):
    """Markers must be matched against what a person can read, not the XML.

    `account_flag_u2` handed the whole hierarchy dump to the classifier, so a
    marker phrase occurring in a resource-id or class name anywhere in the tree
    flagged the account -- and a false positive parks it until somebody looks.
    """

    def test_an_id_that_contains_a_marker_is_not_a_checkpoint(self):
        xml = ('<hierarchy><node resource-id="com.instagram.android:id/'
               'suspicious_activity_banner_stub" text="" content-desc=""/></hierarchy>')
        self.assertIsNone(
            ban_detection.classify_block_text(ban_detection.visible_text_from_dump(xml)))

    def test_a_real_checkpoint_still_classifies(self):
        xml = ('<hierarchy><node resource-id="com.instagram.android:id/title" '
               'text="We detected unusual activity" content-desc=""/></hierarchy>')
        self.assertEqual(
            ban_detection.classify_block_text(ban_detection.visible_text_from_dump(xml)),
            ban_detection.KIND_HUMAN_VERIFICATION)

    def test_a_content_desc_counts_as_visible(self):
        xml = ('<hierarchy><node resource-id="x" text="" '
               'content-desc="Confirm you&apos;re human"/></hierarchy>')
        self.assertEqual(
            ban_detection.classify_block_text(ban_detection.visible_text_from_dump(xml)),
            ban_detection.KIND_HUMAN_VERIFICATION)

    def test_an_escaped_apostrophe_still_matches(self):
        """Half the checkpoint phrases contain an apostrophe and uiautomator
        escapes it, so getting this wrong silently loses those detections."""
        for entity in ("&apos;", "&#39;"):
            with self.subTest(entity=entity):
                xml = f'<hierarchy><node text="Confirm you{entity}re human"/></hierarchy>'
                self.assertEqual(
                    ban_detection.classify_block_text(
                        ban_detection.visible_text_from_dump(xml)),
                    ban_detection.KIND_HUMAN_VERIFICATION)

    def test_a_malformed_dump_still_answers(self):
        # Truncated mid-node: a parser would raise, and raising here would mean
        # the caller has no verdict at all.
        xml = '<hierarchy><node text="We detected unusual activity" content-de'
        self.assertEqual(
            ban_detection.classify_block_text(ban_detection.visible_text_from_dump(xml)),
            ban_detection.KIND_HUMAN_VERIFICATION)

    def test_an_empty_dump_is_empty(self):
        self.assertEqual(ban_detection.visible_text_from_dump(""), "")
        self.assertEqual(ban_detection.visible_text_from_dump(None), "")


class MarkerBreadthTest(TestCase):
    """Two markers were short enough to catch ordinary Instagram copy."""

    def test_a_new_login_notice_is_not_a_checkpoint(self):
        self.assertIsNone(ban_detection.classify_block_text(
            "we detected a new login from a device you don't usually use"))

    def test_we_suspect_alone_is_not_a_checkpoint(self):
        self.assertIsNone(ban_detection.classify_block_text(
            "we suspect this post may contain sensitive content"))

    def test_the_real_unusual_activity_phrase_still_fires(self):
        self.assertEqual(
            ban_detection.classify_block_text("we detected unusual activity on your account"),
            ban_detection.KIND_HUMAN_VERIFICATION)

    def test_the_other_checkpoint_phrases_still_fire(self):
        for phrase in ("confirm you're human", "help us confirm it's you",
                       "suspicious activity", "verify it's you",
                       "confirm your identity", "enter the code we sent"):
            with self.subTest(phrase=phrase):
                self.assertEqual(ban_detection.classify_block_text(phrase),
                                 ban_detection.KIND_HUMAN_VERIFICATION)


class ScheduleWiringTest(TestCase):
    def test_the_loop_is_runnable_and_scheduled(self):
        from adb_bot.automation import run_loop, schedule_spec
        self.assertIn("verify-flags", run_loop.LOOPS)
        self.assertIn("verify-flags", run_loop._DISPATCH)
        self.assertIn("verify-flags", schedule_spec.RECOMMENDED_LOOPS)
        self.assertIn("verify-flags", schedule_spec.RECOMMENDED_INTERVALS)

    def test_it_runs_daily_with_a_start_time(self):
        from adb_bot.automation import schedule_spec
        self.assertEqual(schedule_spec.RECOMMENDED_INTERVALS["verify-flags"], 1440)
        # A daily task needs a start time or it fires at an arbitrary hour.
        self.assertIn("verify-flags", schedule_spec.DEFAULT_DAILY_START)

    def test_it_runs_before_the_digest_that_reports_the_backlog(self):
        """The digest tells the VAs what is waiting on them. Auditing after it
        would send them a list that was already known to be wrong."""
        from adb_bot.automation import schedule_spec
        self.assertLess(schedule_spec.DEFAULT_DAILY_START["verify-flags"],
                        schedule_spec.DEFAULT_DAILY_START["digest"])

    def test_it_is_described_for_both_systemd_and_the_dashboard(self):
        from adb_bot.automation import schedule_spec
        self.assertIn("verify-flags", schedule_spec.DESCRIPTIONS)
        self.assertIn("verify-flags", schedule_spec.WHAT_IT_DOES)

    def test_the_probe_flow_is_registered(self):
        from adb_bot.automation.bootstrap import build_automation
        automation = build_automation()
        names = {getattr(f, "name", "") for f in automation.flows.values()} \
            if hasattr(automation, "flows") else set()
        self.assertIn("instagram_verification_probe", names or
                      {f.name for f in getattr(automation, "_flows", {}).values()})
