"""Batch-migrating every Geelark phone off a wrong (chiefly MultiLogin
relay) proxy onto one of our 4 real proxy-seller.com modems, and cleaning up
what's left behind in the proxy book. Confirmed sequence 2026-08-29:
reassign -> validate against a fresh read -> delete orphaned entries.
"""

import unittest
from unittest.mock import patch

from adb_bot.clients.geelark import proxy_migration as migration

REBOOT_CONFIG = {54015: {"reboot": "https://x/1", "http_port": 44015},
                 54018: {"reboot": "https://x/2", "http_port": 44018}}

REAL_PROXIES = [
    {"id": "real-54015", "server": "162.55.84.35", "port": 54015},
    {"id": "real-54018", "server": "162.55.84.35", "port": 54018},
]


class FakeProxyClient:
    def __init__(self, proxies):
        self._proxies = list(proxies)
        self.delete_calls: list[list[str]] = []

    def list_proxies(self):
        return list(self._proxies)

    def delete_proxies(self, ids):
        self.delete_calls.append(list(ids))
        self._proxies = [p for p in self._proxies if str(p.get("id")) not in ids]


class FakePhoneClient:
    def __init__(self, phones):
        self._phones = phones          # dict-of-dicts, id -> row, mutated live
        self.update_calls: list[tuple[str, dict]] = []

    def list_phones(self):
        return list(self._phones.values())

    def update_phone(self, phone_id, proxy_id=None, **extra):
        self.update_calls.append((phone_id, {"proxy_id": proxy_id, **extra}))
        row = self._phones[phone_id]
        for real in REAL_PROXIES:
            if real["id"] == proxy_id:
                row["proxy"] = {"server": real["server"], "port": real["port"]}
                return
        raise AssertionError(f"unknown proxy_id {proxy_id}")


def _phone(id_, port, status=2, server="gate.multilogin.com"):
    return {"id": id_, "serialName": f"Test {id_}", "status": status,
           "proxy": {"server": server, "port": port}}


class ReassignTest(unittest.TestCase):
    def _run(self, phones):
        phone_client = FakePhoneClient({p["id"]: p for p in phones})
        proxy_client = FakeProxyClient(REAL_PROXIES)
        with patch.object(migration, "GeelarkPhoneClient", lambda transport: phone_client), \
             patch.object(migration, "GeelarkProxyClient", lambda transport: proxy_client), \
             patch.object(migration, "load_reboot_config", lambda: REBOOT_CONFIG):
            report = migration.reassign_wrong_proxies(transport=None)
        return report, phone_client, proxy_client

    def test_reassigns_every_wrong_stopped_phone(self):
        phones = [_phone("1", 1080), _phone("2", 8080)]
        report, phone_client, _ = self._run(phones)
        self.assertEqual(len(report.reassigned), 2)
        self.assertEqual(len(phone_client.update_calls), 2)

    def test_round_robins_across_the_real_proxies(self):
        phones = [_phone("1", 1080), _phone("2", 1080), _phone("3", 1080)]
        report, phone_client, _ = self._run(phones)
        assigned_proxy_ids = [kwargs["proxy_id"] for _, kwargs in phone_client.update_calls]
        self.assertEqual(assigned_proxy_ids, ["real-54015", "real-54018", "real-54015"])

    def test_already_real_phones_are_counted_not_touched(self):
        phones = [_phone("1", 54015, server="162.55.84.35")]
        report, phone_client, _ = self._run(phones)
        self.assertEqual(report.already_real, 1)
        self.assertEqual(phone_client.update_calls, [])

    def test_running_or_starting_phones_are_skipped_not_forced(self):
        """Geelark refuses proxy writes mid-start, and reassigning a phone
        that's already live doesn't move its current session anyway."""
        phones = [_phone("1", 1080, status=0), _phone("2", 1080, status=1)]
        report, phone_client, _ = self._run(phones)
        self.assertEqual(report.reassigned, [])
        self.assertEqual(report.reassign_failed, [])
        self.assertEqual(phone_client.update_calls, [])

    def test_validation_catches_a_write_that_did_not_stick(self):
        """Confirmed account behavior: an update call can answer success
        without the phone's proxy actually changing. Validation reads a
        fresh copy rather than trusting the update call's own response."""
        phones = [_phone("1", 1080)]
        phone_client = FakePhoneClient({"1": phones[0]})

        def _stubborn_update(phone_id, proxy_id=None, **extra):
            phone_client.update_calls.append((phone_id, {"proxy_id": proxy_id}))
            # deliberately does NOT change phones["1"]["proxy"]

        phone_client.update_phone = _stubborn_update
        proxy_client = FakeProxyClient(REAL_PROXIES)
        with patch.object(migration, "GeelarkPhoneClient", lambda transport: phone_client), \
             patch.object(migration, "GeelarkProxyClient", lambda transport: proxy_client), \
             patch.object(migration, "load_reboot_config", lambda: REBOOT_CONFIG):
            report = migration.reassign_wrong_proxies(transport=None)
        self.assertEqual(report.validation_failed, ["1"])
        self.assertFalse(report.clean)

    def test_raises_without_a_matching_saved_proxy(self):
        phones = [_phone("1", 1080)]
        phone_client = FakePhoneClient({"1": phones[0]})
        proxy_client = FakeProxyClient([])     # account has no real proxy saved at all
        with patch.object(migration, "GeelarkPhoneClient", lambda transport: phone_client), \
             patch.object(migration, "GeelarkProxyClient", lambda transport: proxy_client), \
             patch.object(migration, "load_reboot_config", lambda: REBOOT_CONFIG):
            with self.assertRaises(RuntimeError):
                migration.reassign_wrong_proxies(transport=None)


class DeleteOrphanedTest(unittest.TestCase):
    def test_deletes_junk_and_leaves_real_proxies_alone(self):
        junk = [{"id": "j1", "server": "gate.multilogin.com", "port": 1080}]
        proxy_client = FakeProxyClient(REAL_PROXIES + junk)
        with patch.object(migration, "GeelarkProxyClient", lambda transport: proxy_client), \
             patch.object(migration, "load_reboot_config", lambda: REBOOT_CONFIG):
            report = migration.delete_orphaned_proxies(transport=None)
        self.assertEqual(report.junk_proxies_deleted, 1)
        self.assertEqual(report.junk_proxies_stuck, 0)
        remaining_ids = {p["id"] for p in proxy_client.list_proxies()}
        self.assertEqual(remaining_ids, {"real-54015", "real-54018"})

    def test_a_stuck_proxy_geelark_refuses_to_delete_is_reported_not_raised(self):
        """Geelark error 40010 ("proxy binds to the environment") on an
        entry with no live phone using it anymore -- confirmed 2026-08-27,
        a permanent vendor-side leftover, not something to retry into."""
        junk = [{"id": "stuck", "server": "gate.multilogin.com", "port": 1080}]
        proxy_client = FakeProxyClient(REAL_PROXIES + junk)
        proxy_client.delete_proxies = lambda ids: (_ for _ in ()).throw(
            RuntimeError("40010 proxy binds to the environment"))
        with patch.object(migration, "GeelarkProxyClient", lambda transport: proxy_client), \
             patch.object(migration, "load_reboot_config", lambda: REBOOT_CONFIG):
            report = migration.delete_orphaned_proxies(transport=None)
        self.assertEqual(report.junk_proxies_deleted, 0)
        self.assertEqual(report.junk_proxies_stuck, 1)


if __name__ == "__main__":
    unittest.main()
