from __future__ import annotations
import requests

from adb_bot.config.config import API_URL
from adb_bot.core.models import Profile


class MultiloginApiClient:
    def __init__(self, bearer_token: str, api_url: str = API_URL) -> None:
        self.bearer_token = bearer_token
        self.api_url = api_url

    def fetch_adb_credentials(self, profile_ids: list[str]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {"ids": profile_ids}
        print(f"[HTTP] POST {self.api_url}")
        print(f"[HTTP] Payload: {payload}")
        response = requests.post(self.api_url, json=payload, headers=headers, timeout=30)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def parse_profiles(api_response: dict) -> list[Profile]:
        items = api_response.get("data", {}).get("items", []) or []
        profiles: list[Profile] = []

        for item in items:
            profile_id = item.get("id")
            if not profile_id:
                continue

            profiles.append(
                Profile(
                    id=profile_id,
                    status=item.get("status", ""),
                    ip=item.get("ip"),
                    port=item.get("port"),
                    pwd=item.get("pwd"),
                )
            )

        return profiles
