"""Download Airtable attachments (e.g. an account's Profile Picture) to a local
temp file so a device flow can push it. Airtable serves attachments from
temporary URLs, so we fetch on demand right before a run rather than storing paths.
"""

from __future__ import annotations

import os
import tempfile
from urllib.parse import urlparse

import requests


def first_attachment_url(attachments) -> str | None:
    """The first URL from an Airtable multipleAttachments field value."""
    if not attachments:
        return None
    first = attachments[0]
    if isinstance(first, dict):
        return first.get("url")
    return None


def is_url(value) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def download_to_temp(url: str, logger=None, suffix: str | None = None) -> str | None:
    """Fetch `url` into a temp file and return its path (caller cleans up), or
    None on failure. Suffix is inferred from the URL path when not given."""
    if not is_url(url):
        return None
    if suffix is None:
        ext = os.path.splitext(urlparse(url).path)[1]
        suffix = ext or ".jpg"
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="adbbot_pic_")
        with os.fdopen(fd, "wb") as handle:
            handle.write(response.content)
        if logger:
            logger.info("Downloaded attachment to %s (%s bytes)", path, len(response.content))
        return path
    except Exception as exc:  # network/diagnostic path
        if logger:
            logger.warning("Failed to download attachment %s: %s", url, exc)
        return None
