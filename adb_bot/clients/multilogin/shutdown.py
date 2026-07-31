from __future__ import annotations
import requests


class MultiloginShutdownClient:
    def __init__(self, bearer_token: str, base_url: str = "https://launcher.mlx.yt:45001/api/v1/mobile_phone/shutdown") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    def shutdown_profiles(self, profile_ids: list[str]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {"ids": profile_ids}
        print(f"[HTTP] POST {self.base_url}")
        print(f"[HTTP] Payload: {payload}")
        response = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        return response.json()
