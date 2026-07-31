from __future__ import annotations
import requests


class MultiloginAdbEnableClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.multilogin.com/mobile_profiles/phone/adb/set") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    def enable_adb(self, profile_ids: list[str], enabled: bool = True) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {"ids": profile_ids, "enabled": enabled}
        print(f"[HTTP] POST {self.base_url}")
        print(f"[HTTP] Payload: {payload}")
        response = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        return response.json()
