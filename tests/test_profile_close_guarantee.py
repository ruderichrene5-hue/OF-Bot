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

if __name__ == "__main__":
    unittest.main()
