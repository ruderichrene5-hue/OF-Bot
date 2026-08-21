"""Geelark's ADB-credentials endpoint, shaped like MultiLogin's.

`GeelarkApiClient` deliberately mirrors `MultiloginApiClient`'s public surface
(`fetch_adb_credentials` + `parse_profiles`). `prepare_profile_for_adb` in
`automation/workflow.py` is duck-typed against that pair, so a Geelark phone can
be driven through the *existing* connect/authenticate path without forking it.

That works because MultiLogin's mobile profiles are Geelark cloud phones
underneath -- the launcher opens `phone.geelark.com` consoles -- so the on-device
auth shim (`adb shell glogin <pwd>`) is the same one `ADBClient.authenticate`
already speaks.
"""

from __future__ import annotations

from adb_bot.core.models import Profile

from .transport import CODE_ADB_NOT_OPEN, CODE_PHONE_NOT_RUNNING, GeelarkTransport

ADB_GET_DATA_PATH = "/adb/getData"

# Per-item reasons, mapped to statuses a reader can act on. These two are the
# normal answers, not faults: a stopped phone and a phone whose ADB has simply
# never been switched on. Reporting them as "error-42002" makes an idle account
# look broken.
ADB_ITEM_STATUS = {
    CODE_ADB_NOT_OPEN: "adb-not-enabled",
    CODE_PHONE_NOT_RUNNING: "phone-not-running",
}


class GeelarkApiClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def fetch_adb_credentials(self, profile_ids: list[str]) -> dict:
        """Return the raw `/adb/getData` body for these phone ids.

        Unlike MultiLogin this never fails the whole call when one phone has ADB
        switched off -- that phone comes back as an item carrying
        `code: 49001, "ADB did not opened"`, which `parse_profiles` drops.
        """
        return self.transport.post(ADB_GET_DATA_PATH, {"ids": list(profile_ids)})

    @staticmethod
    def parse_profiles(api_response: dict) -> list[Profile]:
        """Map an `/adb/getData` payload to `Profile`s ready for `ADBClient`.

        Only phones that returned a full address *and* password become
        `status="active"`, because `Profile.is_ready` gates on that string. A
        phone whose ADB is still off, or which is mid-enable and has no port
        yet, is reported with its Geelark reason rather than silently dropped --
        the caller needs to tell "not enabled yet" apart from "does not exist".
        """
        # `/adb/getData` nests under `data.items`; `transport.post` has already
        # unwrapped `data`, but accept the full envelope too so this can be fed
        # a captured response.
        payload = api_response.get("data", api_response) or {}
        items = payload.get("items", []) or []
        profiles: list[Profile] = []

        for item in items:
            profile_id = item.get("id")
            if not profile_id:
                continue

            code = item.get("code")
            ip = item.get("ip")
            port = item.get("port")
            pwd = item.get("pwd")

            if code not in (None, 0):
                status = ADB_ITEM_STATUS.get(code, f"error-{code}")
            elif ip and port and pwd:
                status = "active"
            else:
                status = "incomplete"

            profiles.append(
                Profile(
                    id=str(profile_id),
                    status=status,
                    ip=str(ip) if ip else None,
                    port=str(port) if port else None,
                    pwd=str(pwd) if pwd else None,
                )
            )

        return profiles
