"""Read and write MultiLogin tags.

Tags are the client's own vocabulary for where a profile stands -- `Created`,
`Active / Posting`, `Warmup Day 2 Done`. Until now the bot only ever *read*
them (`warmup_targets` selects on `Created`), so the warm-up tags in the
workspace sat at `in_use_count: 0`: somebody made them expecting the state to
show up there, and nothing ever wrote one.

Three endpoints, from the Multilogin API guide's Profile Management and Mobile
Profile Management sections:

* ``POST /tag/search``  -- the workspace's tags, with their ids.
* ``POST /tag/create``  -- a tag has to exist before it can be assigned.
* ``POST /mobile_profiles/tag/{assign,unassign}`` -- by tag **id**, never by
  name, and at most 10 per call.

The mobile endpoints take `profile_id` = the 18-digit MLX API ID, the same key
the launcher uses -- not the human serial.
"""

from __future__ import annotations

import requests

# `tag/search` answers 400 "limit invalid" above 100, so this is the page size
# rather than a self-imposed cap.
SEARCH_PAGE_SIZE = 100
# Documented ceiling on both mobile tag endpoints.
MAX_TAGS_PER_CALL = 10

# The palette `tag/create` accepts. Anything else is rejected outright, so a
# caller's colour is checked here rather than at the far end of an HTTP call.
COLORS = ("blue", "green", "red", "orange", "purple", "teal", "yellow", "gray")


class MultiloginTagClient:
    def __init__(self, bearer_token: str, base_url: str = "https://api.multilogin.com") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url.rstrip("/")
        self._by_name: dict | None = None

    # ------------------------------------------------------------------ http

    def _post(self, path: str, payload: dict) -> dict:
        response = requests.post(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.bearer_token}",
                     "Content-Type": "application/json"},
            json=payload, timeout=30)
        if response.status_code >= 400:
            print(f"[-] MLX {path} -> {response.status_code} {response.text[:200]}")
            response.raise_for_status()
        return response.json() or {}

    # ------------------------------------------------------------------ read

    def list_tags(self) -> list:
        """Every tag in the workspace: ``{id, name, color, in_use_count, ...}``."""
        tags: list = []
        offset = 0
        while True:
            data = self._post("/tag/search", {
                "search_text": "",   # required, and documented as allowed empty
                "limit": SEARCH_PAGE_SIZE,
                "offset": offset,
            }).get("data") or {}
            batch = data.get("tags") or []
            tags.extend(batch)
            if len(batch) < SEARCH_PAGE_SIZE:
                return tags
            offset += SEARCH_PAGE_SIZE

    def tag_ids_by_name(self, refresh: bool = False) -> dict:
        """``{lowercased name: id}``.

        Case-insensitive because these names are typed by a person in the MLX
        UI, which is the same reason `warmup_targets.has_tag` matches that way.
        Memoised: the caller resolves the same handful of names once per
        profile across a fleet-wide sweep.
        """
        if self._by_name is None or refresh:
            self._by_name = {}
            for tag in self.list_tags():
                name = str(tag.get("name") or "").strip()
                if name and tag.get("id"):
                    # First wins. Two tags can share a name in this workspace;
                    # picking the earlier one keeps the choice stable across
                    # runs instead of alternating with the search order.
                    self._by_name.setdefault(name.lower(), tag["id"])
        return self._by_name

    # ----------------------------------------------------------------- write

    def create_tag(self, name: str, color: str = "gray") -> str | None:
        """Create one tag, returning its id (None if the API returned none)."""
        if color not in COLORS:
            raise ValueError(f"{color!r} is not an MLX tag colour ({', '.join(COLORS)})")
        data = self._post("/tag/create", {"tags": [{"name": name, "color": color}]}).get("data") or {}
        ids = data.get("ids") or []
        tag_id = ids[0] if ids else None
        if tag_id and self._by_name is not None:
            self._by_name[name.strip().lower()] = tag_id
        return tag_id

    def ensure_tag(self, name: str, color: str = "gray") -> str | None:
        """The id of `name`, creating the tag if the workspace hasn't got it."""
        existing = self.tag_ids_by_name().get(str(name).strip().lower())
        return existing or self.create_tag(name, color)

    def _tag_call(self, path: str, profile_id: str, tag_ids) -> bool:
        ids = [t for t in dict.fromkeys(tag_ids) if t]   # de-duped, order kept
        if not ids:
            return True
        for start in range(0, len(ids), MAX_TAGS_PER_CALL):
            self._post(path, {"profile_id": str(profile_id),
                              "tags": ids[start:start + MAX_TAGS_PER_CALL]})
        return True

    def assign(self, profile_id: str, tag_ids) -> bool:
        return self._tag_call("/mobile_profiles/tag/assign", profile_id, tag_ids)

    def unassign(self, profile_id: str, tag_ids) -> bool:
        return self._tag_call("/mobile_profiles/tag/unassign", profile_id, tag_ids)

    def retag(self, profile_id: str, *, add=(), remove=()) -> bool:
        """Drop `remove`, then add `add`.

        In that order deliberately: a profile moving from `Warmup Day 1 Done` to
        `Warmup Day 2 Done` should never be seen carrying both, because a person
        filtering on the old tag mid-sweep would read it as not having advanced.
        """
        self.unassign(profile_id, remove)
        return self.assign(profile_id, add)
