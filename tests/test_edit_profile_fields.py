"""Reading the Bio field off the Edit Profile screen dump.

Used to verify a bio write actually landed (`_set_bio`), not to check
profile readiness for tagging -- that moved to polling Geelark's own
`instagramEdit` RPA task (see `adb_bot/clients/geelark/rpa.py`), which sets
Bio/Link/Profile Picture itself and reports completion, instead of us
reading the screen back over ADB.
"""

import xml.etree.ElementTree as ET
import unittest

from adb_bot.automation.flows import instagram as ig


def _node(text="", bounds="[0,0][0,0]"):
    return f'<node text="{text}" bounds="{bounds}"/>'


def _dump(*nodes) -> ET.Element:
    return ET.fromstring(f"<hierarchy>{''.join(nodes)}</hierarchy>")


# A field box is read as "the label row, down to about one field-height
# below it" -- 170px in `_adb_read_labeled_field_value`. Values here sit
# comfortably inside that.
FILLED_SCREEN = _dump(
    _node("Name", "[50,300][150,340]"),
    _node("Alina Sommer", "[200,300][500,340]"),
    _node("Bio", "[50,400][150,440]"),
    _node("Loving life and coffee", "[200,410][700,450]"),
    _node("Links", "[50,600][150,640]"),
    _node("mylink.example.com", "[200,610][700,650]"),
)

EMPTY_SCREEN = _dump(
    _node("Name", "[50,300][150,340]"),
    _node("Alina Sommer", "[200,300][500,340]"),
    _node("Bio", "[50,400][150,440]"),
    _node("Add your bio", "[200,410][700,450]"),
    _node("Links", "[50,600][150,640]"),
    _node("Add link", "[200,610][700,650]"),
)


class BioFieldTest(unittest.TestCase):
    def test_a_filled_bio_is_read(self):
        self.assertEqual(ig._adb_read_bio_field_value(FILLED_SCREEN),
                         "Loving life and coffee")

    def test_an_empty_bio_reads_as_empty(self):
        """"Add your bio" is the field's own placeholder, not a real value."""
        self.assertEqual(ig._adb_read_bio_field_value(EMPTY_SCREEN), "")

    def test_no_dump_reads_as_empty(self):
        self.assertEqual(ig._adb_read_bio_field_value(None), "")

    def test_the_links_row_below_bio_is_never_read_as_the_bio(self):
        """The two fields sit far enough apart on screen that one's value
        must never leak into the other's read."""
        bio = ig._adb_read_bio_field_value(FILLED_SCREEN)
        self.assertNotEqual(bio, "mylink.example.com")


if __name__ == "__main__":
    unittest.main()
