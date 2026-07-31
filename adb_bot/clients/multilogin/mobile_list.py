from __future__ import annotations

import requests


class MultiloginMobileListClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.multilogin.com/mobile_profiles/phone/list") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    def list_mobile_profiles(self) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        params = {
            "page": 1,
            "page_size": 100,
            "sort": "desc",
            "order_by": "created_at",
        }
        print(f"[HTTP] GET {self.base_url}?page=1&page_size=100&sort=desc&order_by=created_at")
        response = requests.get(self.base_url, headers=headers, params=params, timeout=30)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", {}).get("items", []) or []
