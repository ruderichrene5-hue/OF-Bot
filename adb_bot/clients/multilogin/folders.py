from __future__ import annotations

import requests


class MultiloginFolderClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.multilogin.com/workspace/folders") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    def list_mobile_folders(self, folder_type: str = "mobile") -> list[dict]:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        params = {"folder_type": folder_type}
        print(f"[HTTP] GET {self.base_url}?folder_type={folder_type}")
        response = requests.get(self.base_url, headers=headers, params=params, timeout=30)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", {}).get("folders", []) or []
