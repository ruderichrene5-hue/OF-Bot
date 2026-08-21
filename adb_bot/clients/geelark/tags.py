"""Geelark tags and groups.

These matter more than they look. On MultiLogin the tag pair is the entire
posting-permission and flag interface, so any eventual move here depends on
Geelark being able to carry the same state. It can: `/tag/*` is full CRUD.

The catch is that Geelark's tags live at the **top level** (`/tag/list`), not
under `/phone/...`, and a phone's own record carries only tag *names*, with no
ids -- so name is the practical key, exactly as on MultiLogin.
"""

from __future__ import annotations

from .transport import GeelarkTransport

TAG_LIST_PATH = "/tag/list"
TAG_ADD_PATH = "/tag/add"
TAG_UPDATE_PATH = "/tag/update"
TAG_DELETE_PATH = "/tag/delete"

GROUP_LIST_PATH = "/group/list"
GROUP_ADD_PATH = "/group/add"
GROUP_UPDATE_PATH = "/group/update"
GROUP_DELETE_PATH = "/group/delete"

DEFAULT_TAG_COLOR = "blue"


class GeelarkTagClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()
        self._cached_tags: list[dict] | None = None

    def list_tags(self, refresh: bool = False) -> list[dict]:
        if self._cached_tags is None or refresh:
            self._cached_tags = self.transport.paged(TAG_LIST_PATH)
        return self._cached_tags

    def tag_ids_by_name(self, refresh: bool = False) -> dict[str, str]:
        return {
            str(tag.get("name")): str(tag.get("id"))
            for tag in self.list_tags(refresh=refresh)
            if tag.get("name") and tag.get("id")
        }

    def create_tag(self, name: str, color: str = DEFAULT_TAG_COLOR) -> dict:
        data = self.transport.post(TAG_ADD_PATH,
                                   {"list": [{"name": name, "color": color}]})
        self._cached_tags = None
        return data

    def ensure_tag(self, name: str, color: str = DEFAULT_TAG_COLOR) -> str | None:
        """Return the id for `name`, creating the tag if it does not exist."""
        existing = self.tag_ids_by_name()
        if name in existing:
            return existing[name]
        self.create_tag(name, color=color)
        return self.tag_ids_by_name(refresh=True).get(name)

    def delete_tags(self, tag_ids: list[str]) -> dict:
        if not tag_ids:
            raise ValueError("refusing to call tag delete with an empty id list")
        data = self.transport.post(TAG_DELETE_PATH, {"ids": list(tag_ids)})
        self._cached_tags = None
        return data


class GeelarkGroupClient:
    """Groups are Geelark's equivalent of MultiLogin's folders."""

    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def list_groups(self) -> list[dict]:
        return self.transport.paged(GROUP_LIST_PATH)

    def create_group(self, name: str, remark: str = "") -> dict:
        return self.transport.post(GROUP_ADD_PATH,
                                   {"list": [{"name": name, "remark": remark}]})

    def delete_groups(self, group_ids: list[str]) -> dict:
        if not group_ids:
            raise ValueError("refusing to call group delete with an empty id list")
        return self.transport.post(GROUP_DELETE_PATH, {"ids": list(group_ids)})
