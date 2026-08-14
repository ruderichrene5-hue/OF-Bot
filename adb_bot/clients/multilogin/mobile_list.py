from __future__ import annotations

import requests


class MultiloginMobileListClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.multilogin.com/mobile_profiles/phone/list") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url

    PAGE_SIZE = 100

    def list_mobile_profiles(self) -> list[dict]:
        """Every mobile profile in the workspace, across all pages.

        The endpoint pages at 100 and reports the true count in `data.total`.
        Reading only page 1 (sorted newest-first) used to be enough, but the
        workspace has since grown past 100 with staging "Blank" profiles, which
        are the newest rows -- so the oldest *real* profiles silently fell off
        the page and stopped being synced. Anything that diffs MLX against
        Airtable has to see the whole list or it reports phantom deletions.
        """
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        items: list[dict] = []
        page = 1
        while True:
            params = {
                "page": page,
                "page_size": self.PAGE_SIZE,
                "sort": "desc",
                "order_by": "created_at",
            }
            print(f"[HTTP] GET {self.base_url}?page={page}&page_size={self.PAGE_SIZE}"
                  f"&sort=desc&order_by=created_at")
            response = requests.get(self.base_url, headers=headers, params=params, timeout=30)
            print(f"[HTTP] Status: {response.status_code}")
            response.raise_for_status()
            data = response.json().get("data", {}) or {}
            batch = data.get("items") or []
            items.extend(batch)
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            # Stop on a short/empty page as well as on the count, so a missing or
            # wrong `total` cannot spin this into an endless loop.
            if not batch or len(items) >= total:
                break
            page += 1
        print(f"[HTTP] {len(items)} profile(s) over {page} page(s)")
        return items
