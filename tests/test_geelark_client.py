"""The Geelark client, against the habits the live API actually has.

Every test here encodes something found by hitting the live API rather than by
reading docs, and each one is a *silent* failure if got wrong:

* Geelark answers **HTTP 200 for failures**, with the real status in the body's
  `code`. A client that trusts the HTTP status reads every error as success.
* Batch endpoints answer envelope ``code: 0, msg: "success"`` **even when every
  item failed**. This fleet has already lost posts to that exact shape of silent
  no-op, so `BatchOutcome` exists to make it unmissable.
* `pageSize` above 100 returns ``data: null`` rather than an error, so paging
  must cap itself.
* Rows arrive under `items` on some endpoints and `list` on others.
* ADB is off per phone by default, so `/adb/getData` routinely returns a
  per-item `49001` that must not become a `Profile` the bot then tries to drive.
"""

import json
import unittest
from unittest.mock import patch

from adb_bot.clients.geelark.adb_enable import GeelarkAdbEnableClient
from adb_bot.clients.geelark.api import GeelarkApiClient
from adb_bot.clients.geelark.launcher import GeelarkLauncherClient
from adb_bot.clients.geelark.phones import GeelarkPhoneClient, status_label
from adb_bot.clients.geelark.transport import (
    BatchOutcome,
    GeelarkError,
    GeelarkTransport,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok(data):
    return FakeResponse({"traceId": "t", "code": 0, "msg": "success", "data": data})


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _post(self, responses):
        queue = list(responses)

        def fake_post(url, headers=None, json=None, timeout=None):
            self.calls.append((url, json, headers))
            return queue.pop(0) if queue else _ok({})
        return fake_post

    def _transport(self, responses=()):
        transport = GeelarkTransport(app_id="app", api_key="key")
        return transport, patch("adb_bot.clients.geelark.transport.requests.post",
                                self._post(responses))

    def test_signature_is_uppercase_sha256_of_the_five_parts(self):
        """The sign is SHA256(appId+traceId+ts+nonce+apiKey), upper-cased.

        Get the order or the case wrong and every call is rejected, so this
        pins the exact recipe rather than trusting it.
        """
        import hashlib

        transport, ctx = self._transport([_ok({})])
        with ctx:
            transport.post("/phone/list", {})

        _url, _body, headers = self.calls[0]
        expected = hashlib.sha256(
            f"app{headers['traceId']}{headers['ts']}{headers['nonce']}key".encode()
        ).hexdigest().upper()
        self.assertEqual(headers["sign"], expected)
        self.assertEqual(headers["appId"], "app")
        # ts is epoch milliseconds, not seconds: a seconds value is ~10 digits
        # and is rejected as stale.
        self.assertGreaterEqual(len(headers["ts"]), 13)

    def test_a_non_zero_code_raises_even_though_http_is_200(self):
        """The whole point of the transport: HTTP 200 means nothing here."""
        body = FakeResponse({"code": 40004, "msg": "wrong argument"}, status_code=200)
        transport, ctx = self._transport([body])
        with ctx, self.assertRaises(GeelarkError) as caught:
            transport.post("/phone/add", {})
        self.assertEqual(caught.exception.code, 40004)

    def test_paging_caps_at_a_hundred(self):
        """`pageSize` above 100 returns `data: null`, which reads as an empty
        account rather than an error."""
        transport, ctx = self._transport([_ok({"total": 1, "items": [{"id": "1"}]})])
        with ctx:
            transport.paged("/phone/list", page_size=500)
        _url, body, _headers = self.calls[0]
        self.assertEqual(body["pageSize"], 100)

    def test_paging_reads_both_items_and_list(self):
        """Phones come back under `items`, proxies and tags under `list`."""
        transport, ctx = self._transport([_ok({"total": 1, "list": [{"id": "p1"}]})])
        with ctx:
            rows = transport.paged("/proxy/list")
        self.assertEqual(rows, [{"id": "p1"}])

    def test_paging_walks_every_page(self):
        first = _ok({"total": 3, "items": [{"id": "1"}, {"id": "2"}]})
        second = _ok({"total": 3, "items": [{"id": "3"}]})
        transport, ctx = self._transport([first, second])
        with ctx:
            rows = transport.paged("/phone/list", page_size=2)
        self.assertEqual([r["id"] for r in rows], ["1", "2", "3"])
        self.assertEqual([body["page"] for _u, body, _h in self.calls], [1, 2])

    def test_an_unconfigured_client_refuses_rather_than_calling(self):
        transport = GeelarkTransport(app_id="", api_key="")
        self.assertFalse(transport.is_configured)
        with self.assertRaises(GeelarkError):
            transport.post("/phone/list", {})


class BatchOutcomeTest(unittest.TestCase):
    def test_all_items_failing_is_not_success(self):
        """The envelope said `code: 0, msg: "success"` while starting nothing.

        This is the regression that matters most in this file: a bool here would
        have said True.
        """
        outcome = BatchOutcome({
            "totalAmount": 1, "successAmount": 0, "failAmount": 1,
            "failDetails": [{"code": 42001, "id": "0", "msg": "env not found"}],
        })
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failures(), {"0": "env not found"})

    def test_a_partial_batch_is_not_ok(self):
        """One phone of three starting is not a start."""
        outcome = BatchOutcome({"totalAmount": 3, "successAmount": 1, "failAmount": 2})
        self.assertFalse(outcome.ok)

    def test_a_clean_batch_is_ok(self):
        outcome = BatchOutcome({"totalAmount": 2, "successAmount": 2, "failAmount": 0})
        self.assertTrue(outcome.ok)

    def test_an_empty_payload_is_not_success(self):
        """No detail at all must not read as "everything worked"."""
        self.assertFalse(BatchOutcome({}).ok)


class LauncherTest(unittest.TestCase):
    def test_a_transport_failure_becomes_a_failed_outcome_not_an_exception(self):
        """The launch is the flaky call, so it never raises -- callers get the
        per-phone reasons instead, exactly as the MultiLogin launcher does."""
        def boom(url, headers=None, json=None, timeout=None):
            raise GeelarkError("/phone/start", 500, "upstream down")

        transport = GeelarkTransport(app_id="a", api_key="k")
        client = GeelarkLauncherClient(transport)
        with patch("adb_bot.clients.geelark.transport.requests.post", boom):
            outcome = client.start_profiles(["p1", "p2"])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failed, 2)
        self.assertEqual(set(outcome.failures()), {"p1", "p2"})


class AdbEnableTest(unittest.TestCase):
    def test_open_flag_is_sent(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return _ok({"totalAmount": 1, "successAmount": 1, "failAmount": 0})

        transport = GeelarkTransport(app_id="a", api_key="k")
        client = GeelarkAdbEnableClient(transport)
        with patch("adb_bot.clients.geelark.transport.requests.post", fake_post):
            client.disable_adb(["p1"])
        self.assertEqual(calls[0], {"ids": ["p1"], "open": False})


class ParseProfilesTest(unittest.TestCase):
    def test_adb_not_enabled_never_becomes_a_drivable_profile(self):
        """A phone with ADB off comes back as an item, not an error.

        If that became a `Profile` with `status="active"` the bot would try to
        `adb connect` to nothing and blame the phone.
        """
        profiles = GeelarkApiClient.parse_profiles({
            "items": [{"id": "633", "code": 49001, "msg": "ADB did not opened"}]
        })
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].status, "adb-not-enabled")
        self.assertFalse(profiles[0].is_ready)
        self.assertIsNone(profiles[0].target)

    def test_a_complete_row_is_ready_to_drive(self):
        """`is_ready` gates on the literal string "active", so the mapping has
        to produce exactly that -- this is the seam the existing ADB layer and
        `prepare_profile_for_adb` are typed against."""
        profiles = GeelarkApiClient.parse_profiles({
            "items": [{"id": "633", "ip": "1.2.3.4", "port": 20899, "pwd": "secret"}]
        })
        profile = profiles[0]
        self.assertEqual(profile.status, "active")
        self.assertEqual(profile.target, "1.2.3.4:20899")
        self.assertTrue(profile.is_ready)

    def test_a_half_filled_row_is_not_ready(self):
        """Mid-enable, Geelark can answer with no port yet. That is not a
        failure and not a usable phone either."""
        profiles = GeelarkApiClient.parse_profiles({
            "items": [{"id": "633", "ip": "1.2.3.4"}]
        })
        self.assertEqual(profiles[0].status, "incomplete")
        self.assertFalse(profiles[0].is_ready)

    def test_the_full_envelope_is_accepted_too(self):
        """So a captured response can be replayed without unwrapping it."""
        profiles = GeelarkApiClient.parse_profiles({
            "code": 0, "data": {"items": [{"id": "1", "ip": "h", "port": 1, "pwd": "p"}]}
        })
        self.assertEqual(len(profiles), 1)

    def test_rows_without_an_id_are_dropped(self):
        self.assertEqual(GeelarkApiClient.parse_profiles({"items": [{"ip": "x"}]}), [])


class PhoneClientTest(unittest.TestCase):
    def test_status_zero_is_running_not_stopped(self):
        """The enum reads backwards, and it was got wrong first time.

        Established by starting a phone and watching it: 2 while off (ADB says
        42002 "phone is not running"), 1 for ~45s while booting, 0 once ADB
        answers. Reading 0 as "stopped" makes a phone that is burning minutes
        look idle, and a stopped phone look drivable.
        """
        self.assertEqual(status_label(0), "started")
        self.assertEqual(status_label(1), "starting")
        self.assertEqual(status_label(2), "stopped")

    def test_status_label_passes_unknown_values_through(self):
        """Geelark does not document these, so an unrecognised value must be
        visible rather than silently mapped to something plausible."""
        self.assertEqual(status_label(97), "unknown (97)")
        self.assertEqual(status_label(None), "unknown (None)")

    def test_delete_refuses_an_empty_id_list(self):
        """An empty list is a caller bug; sending it invites deleting nothing
        while reporting success."""
        client = GeelarkPhoneClient(GeelarkTransport(app_id="a", api_key="k"))
        with self.assertRaises(ValueError):
            client.delete_phones([])

    def test_creating_phones_requires_a_positive_amount(self):
        """Creating phones costs money, so there is no default amount."""
        client = GeelarkPhoneClient(GeelarkTransport(app_id="a", api_key="k"))
        with self.assertRaises(ValueError):
            client.create_phones(0, android_version=1)


if __name__ == "__main__":
    unittest.main()
