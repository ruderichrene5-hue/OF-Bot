"""MLX's own ledger and the run_one/claim sequencing.

`signup_mlx` deliberately does NOT reuse Geelark's `already_attempted`/
`record_outcome` -- they read/write `geelark_signups.jsonl`, and MLX
profiles are a different pool with a different lifecycle. Filing MLX
results into that ledger would make a re-run think an MLX profile was
already spent based on Geelark's history, or vice versa.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adb_bot.automation import signup_mlx
from adb_bot.automation.flows import signup


class LedgerTest(unittest.TestCase):
    def _ledger(self, rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.close()
        return Path(handle.name)

    def test_a_profile_that_reached_instagram_counts_as_attempted(self):
        path = self._ledger([{"profile_id": "p1", "status": signup.RESULT_CREATED}])
        with mock.patch.object(signup_mlx, "LEDGER", path):
            self.assertEqual(signup_mlx.already_attempted(), {"p1"})

    def test_a_profile_that_never_reached_instagram_is_still_free(self):
        path = self._ledger([{"profile_id": "p1", "status": "mailbox-stuck"}])
        with mock.patch.object(signup_mlx, "LEDGER", path):
            self.assertEqual(signup_mlx.already_attempted(), set())

    def test_record_outcome_appends_a_line(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "mlx_signups.jsonl"
        with mock.patch.object(signup_mlx, "LEDGER", path):
            signup_mlx.record_outcome({"profile_id": "p1", "status": "created"})
            signup_mlx.record_outcome({"profile_id": "p2", "status": "created"})
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual([r["profile_id"] for r in rows], ["p1", "p2"])


PROFILE = {"id": "p1", "serial_name": "Blank caio 1"}
RECORD = {"id": "rec1", "fields": {"Gmail Account": "a@gmail.com",
                                   "Password": "pw", "2FA Secret Key": ""}}


class Args:
    apply = True
    screenshots = False
    verify = True
    country = None
    readiness_attempts = 10
    readiness_wait = 15
    wait_for_lease = 60.0


class RunOneTest(unittest.TestCase):
    def setUp(self):
        self.tmp_ledger = Path(tempfile.mkdtemp()) / "mlx_signups.jsonl"
        patcher = mock.patch.object(signup_mlx, "LEDGER", self.tmp_ledger)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, out, claim_calls):
        with mock.patch.object(signup_mlx, "run_phone", lambda *a, **k: dict(out)), \
             mock.patch.object(signup_mlx, "claim_mailbox",
                              lambda record, identity, profile, apply, logger:
                              claim_calls.append(record["id"])), \
             mock.patch.object(signup_mlx, "MlxSharedProxyHost",
                              lambda *a, **k: object()), \
             mock.patch.object(signup_mlx, "ADBClient", lambda: object()):
            return signup_mlx.run_one(PROFILE, RECORD, Args(), logger=None,
                                      bearer_token="tok")

    def test_a_created_account_claims_the_mailbox(self):
        claim_calls: list = []
        identity = mock.Mock(username="new.user")
        result = self._run({"status": signup.RESULT_CREATED, "identity": identity},
                           claim_calls)
        self.assertEqual(claim_calls, ["rec1"])
        self.assertEqual(result["profile_id"], "p1")

    def test_a_run_that_never_reached_instagram_leaves_the_mailbox_free(self):
        claim_calls: list = []
        identity = mock.Mock(username="")
        self._run({"status": "mailbox-wrong_password", "identity": identity},
                  claim_calls)
        self.assertEqual(claim_calls, [])

    def test_the_outcome_is_recorded_to_the_mlx_ledger(self):
        identity = mock.Mock(username="new.user")
        self._run({"status": signup.RESULT_CREATED, "identity": identity}, [])
        rows = [json.loads(l) for l in self.tmp_ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["profile_id"], "p1")
        self.assertNotIn("identity", rows[0])
