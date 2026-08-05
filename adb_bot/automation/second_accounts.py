"""Which MLX profiles carry a second Instagram account -- and which handles.

One MLX profile is one cloud phone running one Instagram install. On some of
them a second account is logged into that same install ("overview" accounts:
same model, different handle). Those phones can post twice as often, so the
posting queue wants two slots for them instead of one.

MultiLogin records this with a profile *tag*, and the workspace uses two
spellings for the same thing -- "Second Account" on the Jil/Jasmin phones and
"2 accounts" on the Nikki ones. Both mean "this phone has two accounts", so
both are honoured; matching is case- and space-insensitive so a third spelling
of the same words ("second account", "2  Accounts") lands too.

What the tag does NOT give us is the handles. The MLX `remark` free-text field
usually holds one ("@jasjasmin00 second account - Hazel"), but it is typed by
hand and we found it stale on the very first phone checked: Jasmin 5's remark
names @jasjasmin00 while the phone is actually logged into `jasmindiecoolee`
and `naughty_jasminn`. So a remark is treated as a *hint for a human*, never as
the posting target -- the handles that get written to Airtable come from
reading the account switcher on the phone itself
(:func:`adb_bot.automation.flows.instagram_accounts.discover_accounts`).

The parsing here is pure and unit-tested; the device pass lives in
:mod:`adb_bot.automation.second_account_sync`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tag spellings that all mean "two Instagram accounts on this phone".
# Compared after lower-casing and collapsing whitespace.
SECOND_ACCOUNT_TAGS = frozenset({
    "second account",
    "2 accounts",
    "two accounts",
    "second acc",
})

# Pulled out of a remark only to show a human what MultiLogin claims; never
# used as a posting target. Matches "@handle" anywhere in the text.
_REMARK_HANDLE_RE = re.compile(r"@([A-Za-z0-9._]{1,30})")

# A remark mentioning a second account without the profile being tagged is a
# tagging gap worth reporting -- those phones are silently posting half as much
# as they could.
_REMARK_SECOND_HINT_RE = re.compile(r"(?i)\bsecond\s+(account|profile)\b|\b2\s+accounts\b")


def _normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", " ", str(tag or "")).strip().lower()


def has_second_account_tag(item: dict) -> bool:
    """True when this MLX profile is tagged as carrying two IG accounts."""
    for tag in (item or {}).get("tags") or []:
        if _normalize_tag(tag) in SECOND_ACCOUNT_TAGS:
            return True
    return False


def matched_tag(item: dict) -> str | None:
    """The tag that marked it, verbatim -- for reports that show the spelling."""
    for tag in (item or {}).get("tags") or []:
        if _normalize_tag(tag) in SECOND_ACCOUNT_TAGS:
            return tag
    return None


def remark_handles(item: dict) -> list[str]:
    """Handles mentioned in the MLX remark, in order, deduped and lower-cased.

    A hint only -- see the module docstring on why these are not trusted.
    """
    remark = (item or {}).get("remark") or ""
    out: list[str] = []
    for match in _REMARK_HANDLE_RE.finditer(remark):
        handle = match.group(1).lower()
        # Skip the trailing part of an email address ("name@gmail.com" would
        # otherwise contribute "gmail").
        start = match.start()
        if start > 0 and (remark[start - 1].isalnum() or remark[start - 1] in "._"):
            continue
        if handle not in out:
            out.append(handle)
    return out


def remark_hints_second_account(item: dict) -> bool:
    """The remark talks about a second account even if no tag says so."""
    return bool(_REMARK_SECOND_HINT_RE.search((item or {}).get("remark") or ""))


@dataclass
class SecondAccountProfile:
    """One MLX profile that should post as two Instagram accounts."""

    serial_no: str
    launch_id: str
    name: str
    tag: str                       # the tag spelling that matched
    remark_handles: list = field(default_factory=list)   # hints, not targets
    remark: str = ""

    @property
    def model_key(self) -> str:
        """First word of the name -- the same model key the rest of the bot uses."""
        return (self.name or "").split()[0].lower() if self.name else ""


@dataclass
class TagScan:
    """The result of reading the MLX profile list for second accounts."""

    tagged: list = field(default_factory=list)          # [SecondAccountProfile]
    untagged_hints: list = field(default_factory=list)  # [(name, remark)] -- tagging gaps
    tag_counts: dict = field(default_factory=dict)      # every tag -> how many profiles

    def summary(self) -> str:
        return (f"second-account profiles={len(self.tagged)} "
                f"untagged-but-hinted={len(self.untagged_hints)}")

    def by_model(self) -> dict:
        out: dict = {}
        for profile in self.tagged:
            out.setdefault(profile.model_key, []).append(profile)
        return out


def scan_profiles(mlx_items: list) -> TagScan:
    """Read the MLX profile list and pick out the two-account phones.

    Pure: `mlx_items` is exactly what
    ``MultiloginMobileListClient.list_mobile_profiles()`` returns.
    """
    scan = TagScan()
    for item in mlx_items or []:
        for tag in item.get("tags") or []:
            label = str(tag).strip()
            if label:
                scan.tag_counts[label] = scan.tag_counts.get(label, 0) + 1

        name = str(item.get("serial_name") or "").strip()
        remark = str(item.get("remark") or "").strip()

        if has_second_account_tag(item):
            serial_no = str(item.get("serial_no") or "").strip()
            launch_id = str(item.get("id") or "").strip()
            if not serial_no or not launch_id:
                # Without the launch key nothing can be opened for it anyway.
                continue
            scan.tagged.append(SecondAccountProfile(
                serial_no=serial_no,
                launch_id=launch_id,
                name=name or serial_no,
                tag=matched_tag(item) or "",
                remark_handles=remark_handles(item),
                remark=remark,
            ))
        elif remark_hints_second_account(item):
            scan.untagged_hints.append((name or str(item.get("serial_no") or ""), remark))

    scan.tagged.sort(key=lambda p: p.name.lower())
    return scan
