"""Recording a login result on the phone it happened to.

Geelark's remark is the only per-phone note there is, and it is what a person
opens the phone to read -- so the result belongs there, next to the credentials
it was produced from.
"""

import unittest

from adb_bot.clients.geelark.outcomes import (
    CONNECTED_TAG,
    GeelarkOutcomeWriter,
    describe,
    remark_with_outcome,
)
from adb_bot.clients.geelark.transport import GeelarkTransport

REMARK = "IG:@someone | MAIL:a@b.com | PW:secret"


class RemarkTest(unittest.TestCase):
    def test_the_outcome_is_appended_to_the_credentials(self):
        out = remark_with_outcome(REMARK, "email_code_required")
        self.assertTrue(out.startswith(REMARK))
        self.assertIn("LOGIN:OK-needs-email-code", out)

    def test_a_rerun_replaces_rather_than_stacks(self):
        """A phone tried three times must still read cleanly."""
        once = remark_with_outcome(REMARK, "stuck")
        twice = remark_with_outcome(once, "wrong_password")
        thrice = remark_with_outcome(twice, "email_code_required")
        self.assertEqual(thrice.count("LOGIN:"), 1)
        self.assertIn("OK-needs-email-code", thrice)
        self.assertNotIn("STUCK", thrice)

    def test_the_credentials_survive_the_rewrite(self):
        """Overwriting the note must not lose the thing it was written for."""
        out = remark_with_outcome(remark_with_outcome(REMARK, "stuck"), "logged_in")
        self.assertIn("PW:secret", out)
        self.assertIn("MAIL:a@b.com", out)

    def test_a_phone_with_no_remark_still_gets_one(self):
        self.assertEqual(remark_with_outcome("", "logged_in"), "LOGIN:OK-on-feed")

    def test_a_needed_code_counts_as_credentials_accepted(self):
        """A security code means the device is unrecognised, not that the
        password is wrong -- so it is a working account, and must be told apart
        from a genuinely bad one."""
        self.assertTrue(describe("email_code_required")[1])
        self.assertTrue(describe("logged_in")[1])
        self.assertFalse(describe("wrong_password")[1])
        self.assertFalse(describe("account_no_longer_exists")[1])
        self.assertFalse(describe("stuck")[1])

    def test_an_unrecognised_result_still_records_something(self):
        """A new flow result must not silently write nothing."""
        label, accepted = describe("some_new_result")
        self.assertEqual(label, "SOME-NEW-RESULT")
        self.assertFalse(accepted)


class FakeTransport(GeelarkTransport):
    def __init__(self):
        super().__init__(app_id="a", api_key="k")
        self.calls = []

    def paged(self, path, page_size=100, extra=None):
        return [{"id": "tag1", "name": CONNECTED_TAG}]

    def post(self, path, payload=None):
        self.calls.append((path, payload))
        return {}


class WriterTest(unittest.TestCase):
    def test_a_connected_phone_is_tagged(self):
        transport = FakeTransport()
        GeelarkOutcomeWriter(transport).record("p1", REMARK, "email_code_required")
        _path, payload = transport.calls[-1]
        self.assertIn("tag1", payload["tagIDs"])

    def test_a_failed_phone_is_not_tagged(self):
        transport = FakeTransport()
        GeelarkOutcomeWriter(transport).record("p1", REMARK, "wrong_password")
        _path, payload = transport.calls[-1]
        self.assertNotIn("tagIDs", payload)

    def test_existing_tags_are_carried_back(self):
        """`tagIDs` REPLACES a phone's tags rather than adding to them, so
        anything already on the phone is dropped unless it is passed back."""
        transport = FakeTransport()
        GeelarkOutcomeWriter(transport).record(
            "p1", REMARK, "logged_in", existing_tag_ids=["keepme"])
        _path, payload = transport.calls[-1]
        self.assertIn("keepme", payload["tagIDs"])
        self.assertIn("tag1", payload["tagIDs"])

    def test_the_tag_is_not_added_twice(self):
        transport = FakeTransport()
        GeelarkOutcomeWriter(transport).record(
            "p1", REMARK, "logged_in", existing_tag_ids=["tag1"])
        _path, payload = transport.calls[-1]
        self.assertEqual(payload["tagIDs"].count("tag1"), 1)


if __name__ == "__main__":
    unittest.main()
