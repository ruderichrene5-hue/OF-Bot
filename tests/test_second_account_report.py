"""The operator dashboard: what it counts, and what it puts in front of a person."""

from unittest import TestCase

from adb_bot.automation import second_account_report as rep
from adb_bot.clients import airtable as at


def _mlx(name, tags, serial, ident, remark=""):
    return {"serial_name": name, "tags": tags, "serial_no": serial,
            "id": ident, "remark": remark}


def _row(name, serial, status="Active", primary="", second="", has_second=False,
         needs_human=False, reason=None):
    fields = {
        at.F_PROF_NAME: name,
        at.F_PROF_MLX_SERIAL: serial,
        at.F_PROF_STATUS: status,
        at.F_PROF_HAS_SECOND: has_second,
        at.F_PROF_PRIMARY_HANDLE: primary,
        at.F_PROF_SECOND_HANDLE: second,
        at.F_PROF_NEEDS_HUMAN: needs_human,
    }
    if reason:
        fields[at.F_PROF_ISSUE_REASON] = reason
        fields[at.F_PROF_FLAGGED_AT] = "2026-08-05T00:01:05.000Z"
    return {"id": f"rec{serial}", "fields": fields}


class FakeAirtable:
    base_id = "appTEST0000000000"

    def __init__(self, rows):
        self._rows = rows

    def _list_table(self, table, fields=None, **kwargs):
        return self._rows


MLX = [
    _mlx("Jasmin 5", ["Second Account"], "1", "111", "@jasjasmin00 second account"),
    _mlx("Jil 4", ["Second Account"], "2", "222"),
    _mlx("nikki 8", ["2 accounts"], "3", "333"),
    _mlx("Luisa 9", ["Active / Posting"], "4", "444"),
    _mlx("Nikki 14", ["Active / Posting"], "5", "555", "@lamgirmina\n@minacr2914 second account"),
]
ROWS = [
    _row("Jasmin 5", "1", primary="jasmindiecoolee", second="naughty_jasminn", has_second=True),
    _row("Jil 4", "2", status="Inactive", primary="helenaaacutiee", second="jill4_78", has_second=True),
    _row("nikki 8", "3", needs_human=True, reason="Retries Exhausted"),
    _row("Luisa 9", "4"),
]


class CollectTest(TestCase):
    def setUp(self):
        self.data = rep.collect(FakeAirtable(ROWS), MLX)

    def test_only_tagged_phones_are_listed(self):
        self.assertEqual([p["name"] for p in self.data["phones"]],
                         ["Jasmin 5", "Jil 4", "nikki 8"])

    def test_an_active_phone_with_both_handles_counts_as_doubled(self):
        jasmin = next(p for p in self.data["phones"] if p["name"] == "Jasmin 5")
        self.assertEqual(jasmin["state"], "doubled")
        self.assertEqual(self.data["totals"]["doubled"], 1)

    def test_a_parked_phone_is_not_counted_as_doubled(self):
        """Both handles known, but Status Inactive means nothing schedules."""
        jil = next(p for p in self.data["phones"] if p["name"] == "Jil 4")
        self.assertEqual(jil["state"], "parked")
        self.assertEqual(self.data["totals"]["parked"], 1)

    def test_a_phone_without_handles_reads_as_not_read(self):
        nikki = next(p for p in self.data["phones"] if p["name"] == "nikki 8")
        self.assertEqual(nikki["state"], "not-read")
        self.assertEqual(self.data["totals"]["not_read"], 1)

    def test_flagged_profiles_are_collected_whether_or_not_they_are_two_account(self):
        self.assertEqual([n["name"] for n in self.data["needs_human"]], ["nikki 8"])
        self.assertEqual(self.data["totals"]["needs_human"], 1)

    def test_the_tagging_gap_is_reported(self):
        self.assertEqual([h["name"] for h in self.data["untagged_hints"]], ["Nikki 14"])


class RenderTest(TestCase):
    def setUp(self):
        self.page = rep.render(rep.collect(FakeAirtable(ROWS), MLX), generated_at="2026-08-05 20:00")

    def test_it_is_a_page_fragment_not_a_whole_document(self):
        """The Artifact host supplies doctype/html/head/body."""
        lowered = self.page.lower()
        for tag in ("<!doctype", "<html", "<body"):
            self.assertNotIn(tag, lowered)
        self.assertIn("<title>", lowered)

    def test_it_pulls_in_nothing_external(self):
        """A strict CSP blocks any external host, so a stray URL renders broken."""
        self.assertNotIn("http://", self.page)
        self.assertNotIn("https://", self.page)
        self.assertNotIn("//fonts.", self.page)

    def test_both_handles_of_a_doubled_phone_appear(self):
        self.assertIn("jasmindiecoolee", self.page)
        self.assertIn("naughty_jasminn", self.page)

    def test_a_flagged_profile_appears_with_what_to_do_about_it(self):
        self.assertIn("nikki 8", self.page)
        self.assertIn("Retries Exhausted", self.page)
        self.assertIn("Open the phone", self.page)

    def test_it_styles_both_themes(self):
        """The viewer's toggle has to beat the OS preference in both directions."""
        self.assertIn("prefers-color-scheme: dark", self.page)
        self.assertIn(':root[data-theme="dark"]', self.page)
        self.assertIn(':root[data-theme="light"]', self.page)

    def test_a_handle_with_markup_in_it_is_escaped(self):
        rows = [_row("Evil", "1", primary="<script>x</script>", second="b", has_second=True)]
        page = rep.render(rep.collect(FakeAirtable(rows),
                                      [_mlx("Evil", ["Second Account"], "1", "111")]))
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_it_survives_a_base_with_no_tagged_phones(self):
        page = rep.render(rep.collect(FakeAirtable([]), []))
        self.assertIn("<title>", page.lower())
