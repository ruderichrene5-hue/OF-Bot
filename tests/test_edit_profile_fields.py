"""Reading the Bio and Links fields off the Edit Profile screen dump.

Read-only: this is for checking whether a profile is ready to post (bio set,
link set, picture set), not for setting anything. The screen is a static
form, so it dumps reliably where the feed/profile screens do not.
"""

import xml.etree.ElementTree as ET
import unittest
from unittest import mock

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


class LinkFieldTest(unittest.TestCase):
    def test_a_filled_link_is_read(self):
        self.assertEqual(ig._adb_read_link_field_value(FILLED_SCREEN),
                         "mylink.example.com")

    def test_an_empty_link_reads_as_empty(self):
        """"Add link" is the field's own placeholder, not a real value."""
        self.assertEqual(ig._adb_read_link_field_value(EMPTY_SCREEN), "")

    def test_no_dump_reads_as_empty(self):
        self.assertEqual(ig._adb_read_link_field_value(None), "")

    def test_the_bio_value_is_never_read_as_the_link(self):
        """The two fields sit far enough apart on screen that one's value
        must never leak into the other's read."""
        link = ig._adb_read_link_field_value(FILLED_SCREEN)
        self.assertNotEqual(link, "Loving life and coffee")


class _FakeAdb:
    def __init__(self):
        self.commands = []

    def run_command(self, command):
        self.commands.append(command)
        return ""


class ProfileReadinessFlowTest(unittest.TestCase):
    """The tag-facing question: is Bio/Links already filled in, without
    setting anything or leaving the phone sitting in the editor.
    """

    def _flow_with(self, open_outcome, screen_root):
        flow = ig.InstagramProfileReadinessFlow()
        flow._open_edit_profile = mock.Mock(return_value=open_outcome)
        flow._ensure_screen = mock.Mock(return_value=screen_root)
        return flow

    def test_both_filled_in_read_true(self):
        flow = self._flow_with("ok", FILLED_SCREEN)
        adb = _FakeAdb()

        result = flow.check("host:1", adb)

        self.assertEqual(result.blocked, "")
        self.assertTrue(result.bio)
        self.assertTrue(result.link)

    def test_both_empty_read_false(self):
        flow = self._flow_with("ok", EMPTY_SCREEN)

        result = flow.check("host:1", _FakeAdb())

        self.assertEqual(result.blocked, "")
        self.assertFalse(result.bio)
        self.assertFalse(result.link)

    def test_a_blocked_navigation_never_claims_bio_or_link_are_missing(self):
        """Human verification or a failed navigation means "unknown", not
        "not ready" -- conflating the two would tag a real block as if the
        profile just needed its bio filled in."""
        flow = self._flow_with("human_verification", None)

        result = flow.check("host:1", _FakeAdb())

        self.assertEqual(result.blocked, "human_verification")

    def test_checking_leaves_the_editor_via_back_not_forward(self):
        """Every other flow expects to find the phone back on the feed, not
        sitting in the Edit Profile screen a read-only check opened."""
        flow = self._flow_with("ok", FILLED_SCREEN)
        adb = _FakeAdb()

        flow.check("host:1", adb)

        assert any("keyevent 4" in c for c in adb.commands), \
            "never backed out of the Edit Profile screen"


if __name__ == "__main__":
    unittest.main()
