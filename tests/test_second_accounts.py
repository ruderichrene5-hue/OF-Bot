"""Second-account detection and Instagram account switching.

The switcher fixtures are real uiautomator dumps captured off Jasmin 5 (a
"Second Account" phone) -- not hand-written XML -- so a parser change that
would break on the actual app breaks here first.
"""

from pathlib import Path

import pytest

from adb_bot.automation import second_accounts as sa
from adb_bot.automation.flows import instagram_accounts as ia

DATA = Path(__file__).parent
SWITCHER_XML = (DATA / "data_switcher_sheet.xml").read_text()
PROFILE_XML = (DATA / "data_profile_screen.xml").read_text()


# --- MLX tag scanning -----------------------------------------------------

def _item(name, tags, remark="", serial="1", ident="99"):
    return {"serial_name": name, "tags": tags, "remark": remark,
            "serial_no": serial, "id": ident}


def test_both_workspace_tag_spellings_are_honoured():
    """Jil/Jasmin are tagged "Second Account", Nikki "2 accounts" -- one meaning."""
    assert sa.has_second_account_tag(_item("Jil 8", ["Active / Posting", "Second Account"]))
    assert sa.has_second_account_tag(_item("nikki 9", ["2 accounts", "Active / Posting"]))
    assert not sa.has_second_account_tag(_item("Luisa 9", ["Active / Posting"]))
    assert not sa.has_second_account_tag(_item("Blank (40)", ["Created"]))


def test_tag_matching_ignores_case_and_spacing():
    assert sa.has_second_account_tag(_item("x", ["second account"]))
    assert sa.has_second_account_tag(_item("x", ["  SECOND   ACCOUNT "]))
    assert sa.has_second_account_tag(_item("x", ["2  Accounts"]))


def test_unrelated_tags_do_not_match():
    for tag in ("Active / Posting", "Created", "Issue", "gmail", "Link", "Banned / Dead"):
        assert not sa.has_second_account_tag(_item("x", [tag])), tag


def test_scan_collects_tagged_profiles_and_counts_every_tag():
    items = [
        _item("Jil 8", ["Active / Posting", "Second Account"],
              "@jil.lena777 second account - Hazel", serial="184067", ident="626547576228806881"),
        _item("nikki 9", ["2 accounts", "Active / Posting"],
              "@nikk.iie02 second account - Hazel", serial="184090", ident="626549106931728520"),
        _item("Luisa 9", ["Active / Posting"], "@aminulchow3596 - Hazel"),
    ]
    scan = sa.scan_profiles(items)

    assert [p.name for p in scan.tagged] == ["Jil 8", "nikki 9"]
    assert scan.tagged[0].launch_id == "626547576228806881"
    assert scan.tagged[0].model_key == "jil"
    assert scan.tag_counts["Active / Posting"] == 3
    assert scan.tag_counts["Second Account"] == 1


def test_a_profile_without_a_launch_id_is_dropped():
    """No 18-digit id means nothing can be launched for it."""
    scan = sa.scan_profiles([_item("Jil 8", ["Second Account"], ident="")])
    assert scan.tagged == []


def test_untagged_profiles_whose_remark_mentions_a_second_account_are_reported():
    """Nikki 14 is the real case: two handles in the remark, no tag on the profile.

    Those phones post half as much as they could, and only a report surfaces it.
    """
    items = [_item("Nikki 14", ["Active / Posting", "Issue"],
                   "@lamgirmina - Hazel\n@minacr2914 second account\nshirin@gmail.com")]
    scan = sa.scan_profiles(items)
    assert scan.tagged == []
    assert scan.untagged_hints == [
        ("Nikki 14", "@lamgirmina - Hazel\n@minacr2914 second account\nshirin@gmail.com")]


def test_remark_handles_are_extracted_but_email_domains_are_not():
    item = _item("Nikki 12", ["2 accounts"],
                 "@kikittie22 - Hazel\nmegaaliyana2@gmail.com")
    assert sa.remark_handles(item) == ["kikittie22"]


# --- switcher sheet parsing (real device dumps) ---------------------------

def test_parses_both_handles_off_a_real_switcher_dump():
    assert ia.parse_switcher_accounts(SWITCHER_XML) == ["jasmindiecoolee", "naughty_jasminn"]


def test_switcher_parsing_never_returns_the_action_rows():
    """Tapping "Add Instagram account" on a live phone starts a signup flow."""
    handles = ia.parse_switcher_accounts(SWITCHER_XML)
    lowered = [h.lower() for h in handles]
    for forbidden in ("add instagram account", "go to accounts center", "dismiss", "meta logo"):
        assert forbidden not in lowered


def test_switcher_parsing_ignores_system_ui_nodes():
    """The status/navigation bars sit in the same dump ("Vodafone", "Back")."""
    handles = ia.parse_switcher_accounts(SWITCHER_XML)
    for forbidden in ("Vodafone", "Back", "Home", "Recents", "Search"):
        assert forbidden not in handles


def test_active_handle_is_read_from_the_profile_header():
    assert ia.parse_active_handle(PROFILE_XML) == "jasmindiecoolee"


def test_the_profile_screen_alone_lists_no_switcher_accounts():
    """Before the sheet is opened there is nothing to choose from."""
    assert ia.parse_switcher_accounts(PROFILE_XML) == []


def test_malformed_xml_yields_no_accounts_rather_than_raising():
    assert ia.parse_switcher_accounts("<node") == []
    assert ia.parse_active_handle("<node") is None


# --- handle normalisation -------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("@Jasmindiecoolee", "jasmindiecoolee"),
    ("  naughty_jasminn ", "naughty_jasminn"),
    ("@ jil.lena777", "jil.lena777"),
    (None, ""),
    ("", ""),
])
def test_handles_compare_without_at_sign_or_case(raw, expected):
    assert ia.normalize_handle(raw) == expected


# --- ensure_account -------------------------------------------------------

class _Element:
    def __init__(self, device, key, exists=True, info=None):
        self._device, self._key, self.exists, self._info = device, key, exists, info or {}

    @property
    def info(self):
        return self._info

    def click(self):
        self._device.clicks.append(self._key)
        self._device.on_click(self._key)


class _FakeDevice:
    """Just enough uiautomator2 to drive ensure_account.

    `screen` is the handle the header currently reports; clicking an account row
    changes it, which is what the real switch does.
    """

    def __init__(self, active, listed, switch_works=True):
        self.active, self.listed, self.switch_works = active, listed, switch_works
        self.clicks = []
        self.sheet_open = False

    def on_click(self, key):
        if key == "trigger":
            self.sheet_open = True
        elif key.startswith("row:"):
            if self.switch_works:
                self.active = key.split(":", 1)[1]
            self.sheet_open = False

    def __call__(self, **selector):
        rid = selector.get("resourceId", "")
        if rid == ia.ACTION_BAR_TITLE:
            return _Element(self, "title", exists=self.active is not None,
                            info={"text": self.active})
        if rid == ia.ACTION_BAR_USERNAME_CONTAINER:
            return _Element(self, "trigger")
        if rid == "com.instagram.android:id/profile_tab":
            return _Element(self, "profile_tab")
        desc = selector.get("description") or ""
        starts = selector.get("descriptionStartsWith") or ""
        for handle in self.listed:
            if desc == handle or (starts and handle.startswith(starts.rstrip(","))):
                return _Element(self, f"row:{handle}")
        return _Element(self, "missing", exists=False)

    def dump_hierarchy(self):
        if not self.sheet_open:
            return "<hierarchy></hierarchy>"
        rows = "".join(
            f'<node class="android.view.ViewGroup" clickable="true" '
            f'package="com.instagram.android" content-desc="{h}" text="">'
            f'<node class="android.view.View" clickable="false" '
            f'package="com.instagram.android" content-desc="" text="{h}"/></node>'
            for h in self.listed
        )
        return f"<hierarchy>{rows}</hierarchy>"


def _nowait(_seconds, _what):
    return None


def test_no_switch_is_attempted_when_the_wanted_account_is_already_active():
    device = _FakeDevice("naughty_jasminn", ["jasmindiecoolee", "naughty_jasminn"])
    assert ia.ensure_account(device, "naughty_jasminn", settle=_nowait) is True
    assert not any(c.startswith("row:") for c in device.clicks)


def test_an_empty_wanted_handle_is_a_no_op_for_single_account_profiles():
    device = _FakeDevice("jasmindiecoolee", ["jasmindiecoolee"])
    assert ia.ensure_account(device, None, settle=_nowait) is True
    assert ia.ensure_account(device, "", settle=_nowait) is True
    assert device.clicks == []


def test_switching_taps_the_row_and_confirms_the_header_changed():
    device = _FakeDevice("jasmindiecoolee", ["jasmindiecoolee", "naughty_jasminn"])
    assert ia.ensure_account(device, "naughty_jasminn", settle=_nowait) is True
    assert "row:naughty_jasminn" in device.clicks
    assert device.active == "naughty_jasminn"


def test_the_at_sign_form_from_airtable_still_matches():
    device = _FakeDevice("jasmindiecoolee", ["jasmindiecoolee", "naughty_jasminn"])
    assert ia.ensure_account(device, "@Naughty_Jasminn", settle=_nowait) is True
    assert device.active == "naughty_jasminn"


def test_an_account_not_logged_into_this_phone_fails_instead_of_posting():
    """The whole point: never post as whoever happens to be signed in."""
    device = _FakeDevice("jasmindiecoolee", ["jasmindiecoolee", "naughty_jasminn"])
    assert ia.ensure_account(device, "someone_else", settle=_nowait) is False
    assert not any(c.startswith("row:") for c in device.clicks)


def test_a_tap_that_does_not_take_is_reported_as_failure():
    """Tapped, but the header never changed -- must not be treated as success."""
    device = _FakeDevice("jasmindiecoolee", ["jasmindiecoolee", "naughty_jasminn"],
                         switch_works=False)
    assert ia.ensure_account(device, "naughty_jasminn", settle=_nowait) is False


# --- discover_accounts ----------------------------------------------------

class _SwitcherlessDevice(_FakeDevice):
    """A phone whose account switcher will not open (or parses to nothing)."""

    def on_click(self, key):
        if key == "trigger":
            return          # the sheet never opens
        super().on_click(key)


def test_discovery_reports_a_switcher_it_actually_read():
    device = _FakeDevice("jasmindiecoolee", ["jasmindiecoolee", "naughty_jasminn"])
    found = ia.discover_accounts(device, settle=_nowait)

    assert found["active"] == "jasmindiecoolee"
    assert found["accounts"] == ["jasmindiecoolee", "naughty_jasminn"]
    assert found["switcher_read"] is True


def test_a_switcher_that_never_opened_is_not_evidence_of_one_account():
    """The bug this guards: "listed one account" and "could not look" both leave
    the second handle empty. Recording the second as the first would clear a
    working two-account phone back to single over one flaky tap, quietly halving
    its posting until somebody noticed."""
    device = _SwitcherlessDevice("jasmindiecoolee", ["jasmindiecoolee", "naughty_jasminn"])
    found = ia.discover_accounts(device, settle=_nowait)

    assert found["switcher_read"] is False
    assert found["accounts"] == []
    # The header is still reported -- it just isn't proof of the account count.
    assert found["active"] == "jasmindiecoolee"
