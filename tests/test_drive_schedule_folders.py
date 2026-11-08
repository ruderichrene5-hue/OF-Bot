from datetime import date
from unittest import TestCase

from adb_bot.automation.drive_schedule_folders import plan


def f(name, i=None):
    return {"id": i or name, "name": name}


class PlanTest(TestCase):
    def test_creates_the_next_days_and_archives_older_than_two_days_back(self):
        subs = [f("OLD"), f("2026-09-26"), f("2026-09-27"), f("2026-09-28"), f("2026-09-29")]
        create, archive, arch = plan(subs, date(2026, 9, 29), ahead=2)
        self.assertEqual(create, ["2026-09-30", "2026-10-01"])
        self.assertEqual([s["name"] for s in archive], ["2026-09-26"])
        self.assertEqual(arch["name"], "OLD")

    def test_finds_an_archiv_folder_with_trailing_space(self):
        _, _, arch = plan([f("archiv ")], date(2026, 9, 29), ahead=0)
        self.assertEqual(arch["name"], "archiv ")

    def test_ignores_non_date_folders(self):
        _, archive, arch = plan([f("notes"), f("2020-01-01 (1)")], date(2026, 9, 29), ahead=0)
        self.assertEqual(archive, [])
        self.assertIsNone(arch)


class FakeDrive:
    def __init__(self, tree):
        self.tree = tree          # id -> list of {"id","name"}
        self.n = 0

    def list_subfolders(self, fid):
        return list(self.tree.get(fid, []))

    def create_folder(self, parent, name):
        self.n += 1
        nid = f"n{self.n}"
        self.tree.setdefault(parent, []).append({"id": nid, "name": name})
        self.tree[nid] = []
        return nid


class FillTest(TestCase):
    def test_flat_template_only_adds_missing_slots(self):
        from adb_bot.automation.drive_schedule_folders import fill_date_folder, SLOTS
        d = FakeDrive({"day": [{"id": "a", "name": "15 UHR"}]})
        made = fill_date_folder(d, "day", SLOTS, apply=True)
        self.assertEqual(made, SLOTS[1:])
        self.assertEqual(fill_date_folder(d, "day", SLOTS, apply=True), [])

    def test_language_split_day_is_left_alone_by_flat_template(self):
        from adb_bot.automation.drive_schedule_folders import fill_date_folder, SLOTS
        d = FakeDrive({"day": [{"id": "de", "name": "DE"}]})
        self.assertEqual(fill_date_folder(d, "day", SLOTS, apply=True), [])

    def test_nikki_template_builds_languages_and_slots(self):
        from adb_bot.automation.drive_schedule_folders import fill_date_folder, template_for
        d = FakeDrive({"day": []})
        made = fill_date_folder(d, "day", template_for("Reels Instagram - Nikki"), apply=True)
        self.assertIn("DE/23 UHR", made)
        self.assertIn("EN/19 UHR", made)
        self.assertNotIn("EN/21 UHR", made)
