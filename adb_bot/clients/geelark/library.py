"""Geelark's Library (material) API -- profile pictures live here already.

Each model has her own material tag matching her name (e.g. tag "Nikki"),
so a profile picture is found by resolving that tag to an id, then
searching materials carrying it -- not by any naming convention on the
files themselves.
"""

from __future__ import annotations

from .transport import GeelarkTransport

MATERIAL_SEARCH_PATH = "/material/search"
MATERIAL_TAG_SEARCH_PATH = "/material/tag/search"

# From Geelark's material-search docs.
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2


def search_tags(name: str, transport: GeelarkTransport | None = None) -> list[dict]:
    """Material tags whose name matches `name` (Geelark does the matching)."""
    transport = transport or GeelarkTransport()
    data = transport.post(MATERIAL_TAG_SEARCH_PATH, {"name": name})
    return list(data.get("list") or [])


def tag_id_for_name(name: str, transport: GeelarkTransport | None = None) -> str:
    """The id of the material tag named exactly `name`, or "" if there is
    none -- an exact match only, since "Nikki" must not silently pick up
    "Nikki 2" or some other model's near-miss tag."""
    for tag in search_tags(name, transport=transport):
        if str(tag.get("name") or "") == name:
            return str(tag.get("id") or "")
    return ""


def search_materials(tag_ids: list[str], *, file_type: int = FILE_TYPE_IMAGE,
                     page_size: int = 50,
                     transport: GeelarkTransport | None = None) -> list[dict]:
    transport = transport or GeelarkTransport()
    body = {"tagIds": list(tag_ids), "fileType": [file_type],
           "pageSize": page_size}
    data = transport.post(MATERIAL_SEARCH_PATH, body)
    return list(data.get("list") or [])


def picture_url_for_tag(model_tag: str,
                        transport: GeelarkTransport | None = None) -> str:
    """The most recently uploaded image material tagged `model_tag`.

    Returns "" if the tag does not exist, or exists but has no image
    material on it yet -- never a guess.
    """
    transport = transport or GeelarkTransport()
    tag_id = tag_id_for_name(model_tag, transport=transport)
    if not tag_id:
        return ""
    materials = search_materials([tag_id], file_type=FILE_TYPE_IMAGE,
                                 transport=transport)
    if not materials:
        return ""
    newest = max(materials, key=lambda m: m.get("createdTime") or 0)
    return str(newest.get("fileUrl") or "")
