"""Per-model profile-picture URLs for Geelark's `instagramEdit` task.

Kept in a small JSON file by hand rather than Airtable's `Models` table,
which has no image field. A model not yet in the file gets `""` back --
never a guessed or fabricated URL -- so a caller can tell "not configured
yet" apart from "here is the picture", which matters because Geelark's
task treats an included `profilePicture` as something to actually set.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".adb_bot" / "model_profile_pictures.json"


def load_pictures(path: Path = CONFIG_PATH) -> dict[str, str]:
    """`{model name: picture URL}`, or `{}` if the file is missing/unreadable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def picture_url_for(model: str, pictures: dict[str, str] | None = None,
                    path: Path = CONFIG_PATH) -> str:
    """The configured URL for `model`, or `""` if none is set yet.

    `pictures` lets a caller pass an already-loaded dict once for a whole
    batch instead of re-reading the file per phone; omit it to read fresh.
    """
    pictures = load_pictures(path) if pictures is None else pictures
    return pictures.get(model, "")
