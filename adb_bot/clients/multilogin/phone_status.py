from __future__ import annotations
import requests

# Ground-truth phone lifecycle status, distinct from adb_status. Discovered via
# Multilogin's own reference implementation (github.com/multilogin/multilogin-
# cloud-phone-agent) -- undocumented on our side before 2026-09-17, which is
# why nothing here ever checked it: every prior readiness loop inferred boot
# state indirectly from adb/set's own "not running" (42002) responses instead
# of asking the phone's real status directly.
STATUS_READY = 0
STATUS_STARTING = 1
STATUS_SHUTDOWN = 2
STATUS_EXPIRED = 3


class MultiloginPhoneStatusClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.multilogin.com/mobile_profiles/phone/statuses") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    def statuses(self, profile_ids: list[str]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {"ids": profile_ids}
        print(f"[HTTP] POST {self.base_url}")
        print(f"[HTTP] Payload: {payload}")
        response = requests.post(self.base_url, json=payload, headers=headers, timeout=30)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        return response.json()

    def status_for(self, profile_id: str) -> int | None:
        """The single profile's status code, or None if it wasn't returned at all."""
        details = (self.statuses([profile_id]).get("data") or {}).get("success_details") or []
        item = next((d for d in details if str(d.get("id") or "") == str(profile_id)), None)
        return item.get("status") if item else None
