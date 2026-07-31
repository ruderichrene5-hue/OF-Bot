from __future__ import annotations
import requests


class MultiloginLauncherClient:
    def __init__(self, bearer_token: str, base_url: str = "https://launcher.mlx.yt:45001/api/v1/mobile_phone/launch") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    def start_profiles(self, profile_ids: list[str]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {"ids": profile_ids}
        print(f"[HTTP] POST {self.base_url}")
        print(f"[HTTP] Payload: {payload}")
        try:
            response = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
            print(f"[HTTP] Status: {response.status_code}")
            print(f"[HTTP] Response: {response.text}")
            response.raise_for_status()
        except requests.RequestException as exc:
            return {
                "status": "error",
                "error": str(exc),
                "response_text": getattr(exc.response, "text", None),
                "status_code": getattr(exc.response, "status_code", None),
            }
        return response.json()
