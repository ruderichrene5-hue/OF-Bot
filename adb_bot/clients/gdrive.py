"""Minimal Google Drive access for the spoofing pipeline's raw videos.

The pipeline needs to list the per-model subfolders of `01_Raw_Videos`, list
the video files in each, download one file, and (since 2026-09-11, explicit
operator instruction -- restructuring MLX's raw-video layout to match
GeeLark's `<Model>/<YYYY-MM-DD>/` date-folder pattern) create folders and move
files. That narrow surface is what `DriveClient` exposes, so the rest of the
pipeline never imports a Google library and stays testable with a fake.

Auth is a **service account** JSON key (no interactive OAuth -- this runs
unattended on the server). Share the Drive folder with the service account's
email, then point `GOOGLE_SERVICE_ACCOUNT_JSON` (or the Dev-controls setting) at
the key file.

Requires: pip install google-api-python-client google-auth
"""

from __future__ import annotations

import io
from pathlib import Path

# Read+write (widened 2026-09-11 from drive.readonly, explicit operator
# instruction -- "du kannst umtauschen von mir aus" -- so this client can
# create the date subfolders and move the existing raw videos into them.
# Was deliberately readonly before that; keep new write calls scoped to what
# the raw-videos restructuring actually needs (create_folder, move_file),
# not a general excuse to start writing elsewhere in Drive.
SCOPES = ("https://www.googleapis.com/auth/drive",)

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveUnavailable(RuntimeError):
    """Raised when the Google libraries or credentials aren't usable."""


def _build_service(service_account_json: str):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise DriveUnavailable(
            "Google Drive support needs: pip install google-api-python-client google-auth"
        ) from exc

    key_path = Path(service_account_json)
    if not key_path.is_file():
        raise DriveUnavailable(f"Service-account key not found: {service_account_json}")
    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=list(SCOPES))
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class DriveClient:
    """Thin wrapper over the Drive v3 API (list folders/files, download a file).

    `service` is injectable so tests can pass a fake; in production it's built
    lazily from the service-account key.
    """

    def __init__(self, service_account_json: str | None = None, service=None):
        self._service_account_json = service_account_json
        self._service = service

    @property
    def service(self):
        if self._service is None:
            if not self._service_account_json:
                raise DriveUnavailable("No Google service-account key configured.")
            self._service = _build_service(self._service_account_json)
        return self._service

    def _list(self, query: str) -> list:
        """All files matching `query`, following pagination. Shared-drive safe."""
        items: list = []
        page_token = None
        while True:
            response = self.service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token,
            ).execute()
            items.extend(response.get("files", []) or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items

    def list_subfolders(self, parent_id: str) -> list:
        """[{'id','name'}] of the folders directly under `parent_id`."""
        query = f"'{parent_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        return [{"id": f["id"], "name": f["name"]} for f in self._list(query)]

    def list_files(self, parent_id: str) -> list:
        """[{'id','name','link'}] of the non-folder files under `parent_id`."""
        query = f"'{parent_id}' in parents and mimeType != '{FOLDER_MIME}' and trashed = false"
        return [
            {"id": f["id"], "name": f["name"], "link": f.get("webViewLink")}
            for f in self._list(query)
        ]

    def create_folder(self, parent_id: str, name: str) -> str:
        """Create a subfolder named `name` under `parent_id`; returns its id."""
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self.service.files().create(
            body=body, fields="id", supportsAllDrives=True).execute()
        return created["id"]

    def get_or_create_folder(self, parent_id: str, name: str) -> str:
        """`create_folder`, but reuses an existing same-named subfolder instead
        of making a duplicate -- safe to call every run without piling up
        `<Model>/2026-09-11 (1)/`-style copies."""
        for f in self.list_subfolders(parent_id):
            if f["name"] == name:
                return f["id"]
        return self.create_folder(parent_id, name)

    def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str) -> None:
        """Move `file_id` from `old_parent_id` to `new_parent_id` (Drive files
        can have multiple parents; this adds the new one and removes the old,
        which is how Drive v3 represents a "move")."""
        self.service.files().update(
            fileId=file_id, addParents=new_parent_id, removeParents=old_parent_id,
            fields="id, parents", supportsAllDrives=True).execute()

    def download(self, file_id: str, dest_path: str) -> str:
        """Download `file_id` to `dest_path`; returns the path."""
        from googleapiclient.http import MediaIoBaseDownload  # local: optional dep

        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        with io.FileIO(dest_path, "wb") as handle:
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
        return dest_path
