"""Geelark's cloud-phone inventory: list, status, create, clone, delete.

Geelark calls a phone an "env", which is why its error text says
`env not found`. Ids are opaque strings -- keep them as strings, they are long
enough to lose precision if anything ever treats them as numbers.
"""

from __future__ import annotations

from .transport import BatchOutcome, GeelarkError, GeelarkTransport

LIST_PATH = "/phone/list"
STATUS_PATH = "/phone/status"
ADD_PATH = "/phone/addNew"
CLONE_PATH = "/phone/clone"
DELETE_PATH = "/phone/delete"
DETAIL_UPDATE_PATH = "/phone/detail/update"

# `mobileType` is the human Android name, e.g. "Android 13" -- not a version
# number and not an enum. The older `/phone/add` took an integer
# `androidVersion` instead; this is the endpoint the current API documents.
DEFAULT_MOBILE_TYPE = "Android 13"

# `status` values, established by starting a phone and watching them change --
# NOT by guessing from the numbers, which read exactly backwards.
#
#   2 -> stopped   the phone is off. `/adb/getData` answers 42002
#                  "phone is not running".
#   1 -> starting  booting. Still 42002; this lasted ~45s on an Android 15
#                  phone, so anything polling for ADB has to wait it out.
#   0 -> started   running. ADB answers, once it has been switched on.
#
# Reading 0 as "stopped" is the trap here: it makes a running phone -- one that
# is being billed by the minute -- look idle, and makes a stopped phone look
# ready to drive. Anything unrecognised is passed through rather than guessed at.
STATUS_STARTED = 0
STATUS_STARTING = 1
STATUS_STOPPED = 2
STATUS_EXPIRED = 3

STATUS_LABELS = {
    STATUS_STARTED: "started",
    STATUS_STARTING: "starting",
    STATUS_STOPPED: "stopped",
    STATUS_EXPIRED: "expired",
}


def status_label(status: object) -> str:
    """Human-readable phone status, falling back to the raw value."""
    try:
        return STATUS_LABELS.get(int(status), f"unknown ({status})")
    except (TypeError, ValueError):
        return f"unknown ({status})"


class GeelarkPhoneClient:
    PAGE_SIZE = 100

    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def list_phones(self) -> list[dict]:
        """Every cloud phone on the account, across all pages."""
        return self.transport.paged(LIST_PATH, page_size=self.PAGE_SIZE)

    def phone_status(self, profile_ids: list[str]) -> BatchOutcome:
        """Current status for specific phones, without listing the whole fleet."""
        data = self.transport.post(STATUS_PATH, {"ids": list(profile_ids)})
        return BatchOutcome(data)

    def create_phones(
        self,
        names: list[str],
        *,
        mobile_type: str = DEFAULT_MOBILE_TYPE,
        proxy_serial_no: int | None = None,
        proxy_information: str | None = None,
        group_name: str | None = None,
        tags: list[str] | None = None,
        region: str | None = None,
        charge_mode: int = 0,
        **extra: object,
    ) -> dict:
        """Create one cloud phone per entry in `names`.

        Takes explicit names rather than a bare count, because a phone with no
        name is unfindable afterwards -- this fleet already has 46 phones called
        "Blank (NN)" and it is the reason the folder is the only usable grouping.

        **A proxy is required.** Creating with none fails per-row with
        `45006 proxy information error`, which the batch envelope still reports
        as `code: 0` -- so the caller must read the returned details, not the
        envelope. Pass either `proxy_serial_no` (the `serialNo` of a proxy
        already saved on the account) or a full `proxy_information` string of
        the form ``socks5://user:pass@host:port``.

        `charge_mode` 0 is pay-per-minute (which parallel slots can cover) and 1
        is a bound monthly-rental device; 1 fails unless a rental is available.

        Creating phones **costs money** against the plan's profile allowance.
        """
        if not names:
            raise ValueError("refusing to create phones with no names")
        if len(names) > 100:
            raise ValueError("Geelark accepts at most 100 phones per create call")
        if not proxy_serial_no and not proxy_information:
            raise ValueError(
                "a proxy is required: pass proxy_serial_no or proxy_information")

        rows: list[dict[str, object]] = []
        for name in names:
            row: dict[str, object] = {"profileName": name}
            if proxy_serial_no is not None:
                row["proxyNumber"] = int(proxy_serial_no)
            if proxy_information:
                row["proxyInformation"] = proxy_information
            if group_name:
                row["profileGroup"] = group_name
            if tags:
                row["profileTags"] = list(tags)
            if region:
                row["mobileRegion"] = region
            rows.append(row)

        payload: dict[str, object] = {
            "mobileType": mobile_type,
            "chargeMode": int(charge_mode),
            "amount": len(rows),
            "data": rows,
        }
        payload.update(extra)
        return self.transport.post(ADD_PATH, payload)

    def update_phone(self, profile_id: str, *, name: str | None = None,
                     remark: str | None = None, group_id: str | None = None,
                     tag_ids: list[str] | None = None,
                     proxy_id: str | None = None, **extra: object) -> dict:
        """Rename, re-tag, re-group or re-proxy a phone.

        This is the endpoint that makes Geelark able to carry the state
        MultiLogin's tag pair carries today -- names and tags are writable here,
        which `mlx-sync` never managed against MultiLogin.

        Geelark's docs warn explicitly: **do not call this while the phone is
        starting.**
        """
        payload: dict[str, object] = {"id": profile_id}
        if name is not None:
            payload["name"] = name
        if remark is not None:
            payload["remark"] = remark
        if group_id is not None:
            payload["groupID"] = group_id
        if tag_ids is not None:
            payload["tagIDs"] = list(tag_ids)
        if proxy_id is not None:
            payload["proxyId"] = proxy_id
        payload.update(extra)
        return self.transport.post(DETAIL_UPDATE_PATH, payload)

    def clone_phone(self, profile_id: str, amount: int = 1, **extra: object) -> dict:
        """Duplicate an existing phone `amount` times.

        Cloning the configured template is usually cheaper to reason about than
        `create_phones`, because proxy, device model and installed apps come
        along instead of having to be reassembled field by field.
        """
        if amount < 1:
            raise ValueError("amount must be at least 1")
        payload: dict[str, object] = {"envId": profile_id, "amount": int(amount)}
        payload.update(extra)
        return self.transport.post(CLONE_PATH, payload)

    def delete_phones(self, profile_ids: list[str]) -> BatchOutcome:
        """Delete phones. Irreversible -- the phone and its state are gone."""
        if not profile_ids:
            raise ValueError("refusing to call delete with an empty id list")
        try:
            data = self.transport.post(DELETE_PATH, {"ids": list(profile_ids)})
        except GeelarkError as error:
            return BatchOutcome({
                "totalAmount": len(profile_ids),
                "successAmount": 0,
                "failAmount": len(profile_ids),
                "failDetails": [
                    {"id": profile_id, "code": error.code, "msg": error.message}
                    for profile_id in profile_ids
                ],
            })
        return BatchOutcome(data)
