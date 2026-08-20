"""Standing the fleet up on Geelark: folders, phones, and the remark.

The remark is the whole point of the exercise -- it is what a person opens the
phone to read. So its shape is pinned here rather than left to drift, and the
two guards that stop a rerun doing damage are pinned with it.
"""

import unittest
from unittest.mock import patch

from adb_bot.clients.geelark.provision import (
    GeelarkProvisioner,
    ProfileRow,
    ProvisionResult,
)
from adb_bot.clients.geelark.transport import GeelarkError, GeelarkTransport


class RemarkTest(unittest.TestCase):
    def test_an_account_we_can_log_in_carries_its_credentials(self):
        row = ProfileRow(name="Nikki 3", model="Nikki", handle="nikki_lat",
                         email="a@b.com", password="s3cret")
        self.assertEqual(row.remark(),
                         "IG:@nikki_lat | MAIL:a@b.com | PW:s3cret")

    def test_an_account_we_cannot_carries_a_pointer_to_look_it_up(self):
        """The whole reason the no-credential rows exist: somebody has to be
        able to find the MultiLogin profile and check it by hand."""
        row = ProfileRow(name="Nikki 12", model="Nikki",
                         mlx_id="628660154778255398", handle="nikki_lat")
        self.assertEqual(
            row.remark(),
            "NOCREDS | MLX:Nikki 12 | ID:628660154778255398 | IG:@nikki_lat")

    def test_a_missing_handle_does_not_leave_a_dangling_separator(self):
        row = ProfileRow(name="Jil 5", model="Jil", mlx_id="123456789012")
        self.assertEqual(row.remark(), "NOCREDS | MLX:Jil 5 | ID:123456789012")

    def test_the_remark_is_greppable_both_ways(self):
        """Written so a later pass can parse these back rather than only read
        them: every row starts with either IG: or NOCREDS."""
        with_creds = ProfileRow(name="a", model="m", handle="h",
                                email="e@x", password="p").remark()
        without = ProfileRow(name="a", model="m", mlx_id="123456789012").remark()
        self.assertTrue(with_creds.startswith("IG:"))
        self.assertTrue(without.startswith("NOCREDS"))

    def test_credentials_win_over_the_pointer(self):
        """A row with a password must never be filed as needing manual work."""
        row = ProfileRow(name="a", model="m", mlx_id="123456789012",
                         handle="h", email="e@x", password="p")
        self.assertTrue(row.has_credentials)
        self.assertNotIn("NOCREDS", row.remark())


class FakeTransport(GeelarkTransport):
    def __init__(self, phones=(), proxies=(), details=None):
        super().__init__(app_id="a", api_key="k")
        self._phones = list(phones)
        self._proxies = list(proxies)
        self._details = details
        self.calls = []

    def paged(self, path, page_size=100, extra=None):
        if path == "/phone/list":
            return self._phones
        if path == "/proxy/list":
            return self._proxies
        return []

    def post(self, path, payload=None):
        self.calls.append((path, payload))
        rows = (payload or {}).get("data") or []
        if self._details is not None:
            return {"details": self._details}
        return {"details": [{"index": i, "code": 0, "id": f"id{i}",
                             "profileName": r["profileName"]}
                            for i, r in enumerate(rows)]}


PROXIES = [{"id": "1", "serialNo": 5}, {"id": "2", "serialNo": 6}]


class ProvisionTest(unittest.TestCase):
    def _rows(self, n):
        return [ProfileRow(name=f"Nikki {i}", model="Nikki",
                           mlx_id=f"63000000000{i}") for i in range(n)]

    def test_a_rerun_does_not_duplicate_the_fleet(self):
        """Geelark does not enforce unique names, so nothing but this stops a
        second run building a whole parallel set of phones."""
        transport = FakeTransport(
            phones=[{"serialName": "Nikki 0"}, {"serialName": "Nikki 1"}],
            proxies=PROXIES)
        prov = GeelarkProvisioner(transport)
        fresh = prov.plan_rows(self._rows(3), free_slots=100)
        self.assertEqual([r.name for r in fresh], ["Nikki 2"])

    def test_it_refuses_to_exceed_the_plan_allowance(self):
        """Creating past the allowance fails per-row and the envelope still
        says success, so the check belongs before the call."""
        transport = FakeTransport(proxies=PROXIES)
        prov = GeelarkProvisioner(transport)
        with self.assertRaises(GeelarkError):
            prov.plan_rows(self._rows(10), free_slots=3)

    def test_creating_without_a_proxy_is_refused_up_front(self):
        """A row with no proxy fails with 45006 while the envelope reports
        success -- better to refuse than to create nothing quietly."""
        transport = FakeTransport(proxies=[])
        prov = GeelarkProvisioner(transport)
        with self.assertRaises(GeelarkError):
            prov.create(self._rows(2))

    def test_proxies_are_spread_round_robin(self):
        transport = FakeTransport(proxies=PROXIES)
        prov = GeelarkProvisioner(transport)
        prov.create(self._rows(4))
        sent = transport.calls[0][1]["data"]
        self.assertEqual([row["proxyNumber"] for row in sent], [5, 6, 5, 6])

    def test_the_group_and_remark_are_sent_on_creation(self):
        """Both auto-create/attach on /phone/addNew, which is why no separate
        group call is needed."""
        transport = FakeTransport(proxies=PROXIES)
        GeelarkProvisioner(transport).create(
            [ProfileRow(name="Jil 1", model="Jil", handle="h",
                        email="e@x", password="p")])
        row = transport.calls[0][1]["data"][0]
        self.assertEqual(row["profileGroup"], "Jil")
        self.assertIn("PW:p", row["profileNote"])

    def test_per_row_failures_are_reported_not_swallowed(self):
        """/phone/addNew answers code 0 while individual rows fail."""
        transport = FakeTransport(proxies=PROXIES, details=[
            {"index": 0, "code": 0, "id": "x", "profileName": "Nikki 0"},
            {"index": 1, "code": 45006, "msg": "proxy information error",
             "profileName": "Nikki 1"},
        ])
        result = GeelarkProvisioner(transport).create(self._rows(2))
        self.assertEqual(len(result.created), 1)
        self.assertEqual(len(result.failed), 1)
        self.assertFalse(result.ok)

    def test_large_builds_are_batched(self):
        """Geelark takes at most 100 rows per call."""
        transport = FakeTransport(proxies=PROXIES)
        with patch("adb_bot.clients.geelark.provision.time.sleep",
                   lambda *_: None):
            result = GeelarkProvisioner(transport).create(self._rows(150))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(result.created), 150)


if __name__ == "__main__":
    unittest.main()
