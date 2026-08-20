"""Write what happened to a phone back onto the phone.

Geelark's `remark` is the only per-phone note the platform gives us, and it is
what a person sees when they open the phone. So the login result is recorded
there rather than only in a log nobody will read six weeks from now, and a tag
marks the ones whose credentials actually worked so the list can be filtered.

The remark keeps its credentials and gains a `LOGIN:` clause. Re-running
replaces that clause rather than appending a second one, so a phone tried three
times still reads cleanly.

⚠️ `/phone/detail/update` must not be called while a phone is **starting** --
Geelark's own docs say so. Record outcomes after stopping the phone.
"""

from __future__ import annotations

import re

from .tags import GeelarkTagClient
from .transport import GeelarkTransport

UPDATE_PATH = "/phone/detail/update"
CONNECTED_TAG = "IG connected"

# Any earlier LOGIN: clause, so a re-run replaces rather than stacks.
_LOGIN_CLAUSE = re.compile(r"\s*\|\s*LOGIN:[^|]*")

# What each flow result should say on the phone, and whether the account's
# credentials were accepted by Instagram.
#   accepted = Instagram took the handle and password. It may still want a
#   security code -- that is the account being unrecognised on a new device,
#   not the credentials being wrong.
OUTCOMES = {
    "logged_in": ("OK-on-feed", True),
    "email_code_required": ("OK-needs-email-code", True),
    "sms_code_required": ("OK-needs-sms-code", True),
    "two_factor_required": ("OK-needs-2fa", True),
    "wrong_password": ("WRONG-PASSWORD", False),
    "account_no_longer_exists": ("ACCOUNT-GONE", False),
    "handle_not_found": ("HANDLE-NOT-FOUND", False),
    "boot_never_completed": ("PHONE-DID-NOT-BOOT", False),
    "app_never_opened": ("INSTAGRAM-DID-NOT-OPEN", False),
    "adb_unreachable": ("ADB-UNREACHABLE", False),
    "phone_not_ready": ("PHONE-NOT-READY", False),
    "suspended": ("SUSPENDED", False),
    "unknown_screen": ("UNKNOWN-SCREEN", False),
    "stuck": ("STUCK", False),
}


def describe(result: str) -> tuple[str, bool]:
    """(label for the remark, were the credentials accepted)."""
    return OUTCOMES.get(result, (result.upper().replace("_", "-"), False))


def remark_with_outcome(remark: str, result: str, day: str = "") -> str:
    """The phone's remark with its LOGIN: clause set to this result."""
    label, _accepted = describe(result)
    base = _LOGIN_CLAUSE.sub("", remark or "").rstrip(" |")
    clause = f"LOGIN:{label}" + (f" {day}" if day else "")
    return f"{base} | {clause}" if base else clause


class GeelarkOutcomeWriter:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()
        self.tags = GeelarkTagClient(self.transport)
        self._connected_tag_id: str | None = None

    def connected_tag_id(self) -> str | None:
        if self._connected_tag_id is None:
            self._connected_tag_id = self.tags.ensure_tag(CONNECTED_TAG, "green")
        return self._connected_tag_id

    def record(self, phone_id: str, remark: str, result: str,
               existing_tag_ids: list[str] | None = None,
               day: str = "") -> dict:
        """Write the outcome onto the phone, tagging it if it connected.

        `existing_tag_ids` matters: `tagIDs` **replaces** a phone's tags rather
        than adding to them, so anything already on the phone has to be passed
        back in or it is silently dropped.
        """
        _label, accepted = describe(result)
        payload: dict[str, object] = {
            "id": phone_id,
            "remark": remark_with_outcome(remark, result, day),
        }

        tag_ids = list(existing_tag_ids or [])
        if accepted:
            tag_id = self.connected_tag_id()
            if tag_id and tag_id not in tag_ids:
                tag_ids.append(tag_id)
        if tag_ids:
            payload["tagIDs"] = tag_ids

        self.transport.post(UPDATE_PATH, payload)
        return {"phone_id": phone_id, "result": result, "tagged": accepted}
