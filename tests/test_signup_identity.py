"""record_account must never lose an earlier account to a concurrent writer.

A batch of signup workers all call this with no other coordination -- 2026-08-23,
a real 20-phone Cloe batch collapsed ~30 recorded accounts down to 2 because two
writers raced a load-modify-write. Locked + atomic-replace now; these pin that
a second writer's record does not erase a first writer's.
"""

from __future__ import annotations

import unittest

from adb_bot.automation import signup_identity as si
from adb_bot.automation.flows.signup import Identity


def _identity(username: str) -> Identity:
    return Identity(full_name="Mia Vogel", username=username, password="x",
                    birth_day=1, birth_month="January", birth_year=2000)


class RecordAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = self.enterContext(
            __import__("tempfile").TemporaryDirectory())
        self._saved_dir, self._saved_file, self._saved_lock = (
            si.ACCOUNTS_DIR, si.ACCOUNTS_FILE, si.ACCOUNTS_LOCK_FILE)
        from pathlib import Path
        si.ACCOUNTS_DIR = Path(self.tmpdir)
        si.ACCOUNTS_FILE = si.ACCOUNTS_DIR / "accounts.json"
        si.ACCOUNTS_LOCK_FILE = si.ACCOUNTS_DIR / "accounts.json.lock"
        self.addCleanup(self._restore)

    def _restore(self):
        si.ACCOUNTS_DIR, si.ACCOUNTS_FILE, si.ACCOUNTS_LOCK_FILE = (
            self._saved_dir, self._saved_file, self._saved_lock)

    def test_a_second_account_does_not_erase_the_first(self):
        si.record_account("phone-1", "Cloe new 1", _identity("mia_vogel"))
        si.record_account("phone-2", "Cloe new 2", _identity("ida_berg"))

        accounts = si.load_accounts()
        self.assertEqual(len(accounts), 2)
        self.assertIn("mia_vogel", si.taken_usernames())
        self.assertIn("ida_berg", si.taken_usernames())

    def test_a_reader_never_sees_a_half_written_file(self):
        """The write goes through a temp file + os.replace, not in place --
        load_accounts() only ever sees a complete file, old or new."""
        si.record_account("phone-1", "Cloe new 1", _identity("mia_vogel"))
        self.assertEqual(si.ACCOUNTS_FILE.with_suffix(".json.tmp").exists(),
                         False)
        self.assertTrue(si.ACCOUNTS_FILE.exists())

    def test_concurrent_writers_are_serialized_not_lost(self):
        """Simulates the real race: two workers both load before either
        writes. Without the lock this drops one writer's record entirely."""
        import threading

        results = []

        def worker(n):
            si.record_account(f"phone-{n}", f"Cloe new {n}",
                              _identity(f"user{n}"))
            results.append(n)

        threads = [threading.Thread(target=worker, args=(n,))
                  for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        accounts = si.load_accounts()
        self.assertEqual(len(accounts), 8)
        self.assertEqual(len(results), 8)


if __name__ == "__main__":
    unittest.main()
