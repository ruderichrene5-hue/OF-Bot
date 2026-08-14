"""No phone outlives its run: the 7-minute close guarantee.

The ``finally`` in `_guarantee_profile_closed` covers a workflow that returns or
raises; the other tests cover that. This covers the case it cannot -- a flow
that hangs and never comes back, which is what left phones open for hours on
2026-08-04 until the box OOMed.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from adb_bot.automation import workflow
from adb_bot.automation.workflow import run_profile_workflow, MAX_PROFILE_OPEN_SECONDS
from adb_bot.core.models import Profile

class Rec:
    def __init__(self): self.calls = []
    def shutdown_profiles(self, ids): self.calls.append(list(ids)); return {"status": "ok"}

class L:
    def info(self,*a,**k): pass
    def warning(self,*a,**k): pass
    def exception(self,*a,**k): pass

class T(unittest.TestCase):
    def test_budget_is_seven_minutes(self):
        self.assertEqual(MAX_PROFILE_OPEN_SECONDS, 420)

    def test_watchdog_closes_a_hung_flow(self):
        client = Rec()
        started = time.time()
        class HangingFlow:
            def run(self, *a, **k):
                # Never returns on its own; the watchdog must close the phone.
                while client.calls == [] and time.time() - started < 5:
                    time.sleep(0.02)
                return {"success": True}
        class A:
            def __init__(self): self.flows = {"instagram_scroll": HangingFlow()}
        with patch("adb_bot.automation.workflow.prepare_profile_for_adb",
                   return_value=Profile(id="profile-1", status="ready")), \
             patch("adb_bot.automation.workflow.connect_with_retries", return_value="d"), \
             patch("adb_bot.automation.workflow.ADBClient") as m:
            m.return_value.run_command.return_value = ""
            run_profile_workflow("profile-1","tok",SimpleNamespace(),SimpleNamespace(),
                                 client, A(), L(), flow_name="instagram_scroll",
                                 shutdown_on_success=False, max_open_seconds=0.3)
        self.assertEqual(client.calls, [["profile-1"]], "watchdog did not close the hung phone")
        self.assertLess(time.time()-started, 4, "watchdog did not fire on its budget")

class FlowBudgetT(unittest.TestCase):
    """The budget belongs to the flow, not to whoever calls it.

    A warm-up takes ~14 minutes of scrolling and following; a post takes two.
    Under one shared 7-minute ceiling every warm-up was closed mid-scroll, and
    the heartbeat reported it as "profile lost mid-run" -- so it read as a dead
    phone and got retried instead of fixed. Measured 13m41s on 2026-08-06.
    """

    def test_the_warm_up_gets_the_time_it_actually_needs(self):
        self.assertGreater(workflow.open_budget_for("warm_up_process"), 13 * 60 + 41)

    def test_the_bare_scroll_gets_the_same_budget_as_its_own_superset(self):
        """`warm_up_process` IS `instagram_scroll` plus a follow pass.

        Whatever ceiling the longer flow needs, the shorter one cannot need
        more -- so pinning them equal is the only relationship that can't be
        wrong. It was the *absence* of instagram_scroll from the table, not a
        too-low number in it, that killed all 155 day-4 attempts.
        """
        self.assertEqual(workflow.open_budget_for("instagram_scroll"),
                         workflow.open_budget_for("warm_up_process"))

    def test_the_bare_scroll_clears_its_own_scroll_target_with_room(self):
        """600s is only what `_build_sequence(target_total_delay=600.0)` SLEEPS
        for. On top of it the flow pays ~148 blocking swipe round trips, the two
        launch commands, the 5s feed-load wait and a 3s tail -- ~800s in the
        worst plausible run -- and the watchdog is armed before the workflow
        even starts, so it is also timing the MLX readiness wait (8 * 10s here)
        and the ADB connect retries (5 * 5s). A budget merely above 600 would be
        the same 356s death with extra steps."""
        self.assertGreater(workflow.open_budget_for("instagram_scroll"), 800 + 105)

    def test_a_post_is_unchanged(self):
        self.assertEqual(workflow.open_budget_for("instagram_reel_upload_u2"),
                         MAX_PROFILE_OPEN_SECONDS)

    def test_an_unknown_flow_gets_the_default_rather_than_no_ceiling(self):
        self.assertEqual(workflow.open_budget_for("something_new"), MAX_PROFILE_OPEN_SECONDS)
        self.assertEqual(workflow.open_budget_for(None), MAX_PROFILE_OPEN_SECONDS)

    def _budget_seen_by(self, **kwargs):
        """The budget the watchdog is actually armed with, via Timer's interval."""
        seen = {}
        real = __import__("threading").Timer

        def spy(interval, fn, *a, **k):
            seen["budget"] = interval
            return real(interval, lambda: None)

        class Flow:
            def run(self, *a, **k): return {"success": True}
        class A:
            def __init__(self): self.flows = {"warm_up_process": Flow(),
                                              "instagram_scroll": Flow(),
                                              "instagram_reel_upload_u2": Flow()}
        with patch("adb_bot.automation.workflow.threading.Timer", side_effect=spy), \
             patch("adb_bot.automation.workflow.prepare_profile_for_adb",
                   return_value=Profile(id="p", status="ready")), \
             patch("adb_bot.automation.workflow.connect_with_retries", return_value="d"), \
             patch("adb_bot.automation.workflow.ADBClient") as m:
            m.return_value.run_command.return_value = ""
            run_profile_workflow("p", "tok", SimpleNamespace(), SimpleNamespace(),
                                 Rec(), A(), L(), shutdown_on_success=False, **kwargs)
        return seen.get("budget")

    def test_the_warm_up_flow_arms_the_watchdog_with_its_own_budget(self):
        """The wiring, not just the table: a caller passing nothing must still
        get 20 minutes, because no caller knows to ask for it."""
        self.assertEqual(self._budget_seen_by(flow_name="warm_up_process"), 20 * 60)

    def test_posting_still_gets_seven_minutes(self):
        """This used to arm on `instagram_scroll` as its stand-in for "a short
        flow". That was only ever true by accident -- the scroll is a 10-minute
        warm-up run that had been left out of FLOW_OPEN_SECONDS -- so the
        assertion was pinning the bug rather than the posting budget. Arm on an
        actual post, which is what the 420s default is dimensioned for."""
        self.assertEqual(self._budget_seen_by(flow_name="instagram_reel_upload_u2"), 420.0)

    def test_the_bare_scroll_flow_arms_the_watchdog_with_its_own_budget(self):
        """Same wiring check as the warm-up's: the warm-up runner passes no
        `max_open_seconds`, so a right number in the table that never reaches
        the Timer would leave day 4 dying exactly as before."""
        self.assertEqual(self._budget_seen_by(flow_name="instagram_scroll"), 20 * 60)

    def test_an_explicit_budget_still_wins(self):
        self.assertEqual(self._budget_seen_by(flow_name="warm_up_process",
                                              max_open_seconds=99), 99.0)
        self.assertEqual(self._budget_seen_by(flow_name="instagram_scroll",
                                              max_open_seconds=99), 99.0)

    def test_zero_still_means_no_watchdog(self):
        self.assertIsNone(self._budget_seen_by(flow_name="warm_up_process",
                                               max_open_seconds=0))
        self.assertIsNone(self._budget_seen_by(flow_name="instagram_scroll",
                                               max_open_seconds=0))


if __name__ == "__main__":
    unittest.main()
