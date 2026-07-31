import shutil
from pathlib import Path
from threading import Lock
from typing import Optional

_SHARED_QUEUES: dict[tuple[str, str], "StoryMediaQueueManager"] = {}
_SHARED_QUEUE_LOCK = Lock()

SUPPORTED_MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
}


def discover_story_media_files(source: str | Path, logger=None) -> list[Path]:
    source_path = Path(source)
    if not source_path.exists():
        return []

    if source_path.is_file():
        if source_path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS:
            return [source_path]
        return []

    if not source_path.is_dir():
        return []

    pending_files = [
        path
        for path in source_path.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
    ]
    pending_files.sort(key=lambda item: item.name.lower())
    return pending_files


class StoryMediaQueueManager:
    def __init__(self, source: str | Path, used_folder_name: str = "used", logger=None) -> None:
        self.source = Path(source)
        self.used_folder_name = used_folder_name
        self.used_folder = self.source / self.used_folder_name
        self.logger = logger
        self._lock = Lock()
        self._pending_files = discover_story_media_files(self.source, logger=logger)
        self._assigned_files: set[Path] = set()
        self._used_files: set[Path] = set()
        self.used_folder.mkdir(parents=True, exist_ok=True)

    def _take_pending(self) -> Optional[Path]:
        while self._pending_files:
            candidate = self._pending_files.pop(0)
            if candidate in self._used_files or candidate in self._assigned_files:
                continue
            if not candidate.exists():
                continue
            self._assigned_files.add(candidate)
            return candidate
        return None

    def _refresh_pending(self) -> None:
        """Re-scan for clips dropped into the folder after this queue was built.

        A queue is cached for the life of the process, so without this a folder
        mapped once and topped up later would keep reporting "no pending media".
        Files already handed out stay excluded, so a re-scan can never give two
        profiles the same clip in one session.
        """
        if not self.source.is_dir():
            return
        known = set(self._pending_files) | self._assigned_files | self._used_files
        self._pending_files.extend(
            path
            for path in discover_story_media_files(self.source, logger=self.logger)
            if path not in known
        )

    def get_next_media(self) -> Optional[Path]:
        with self._lock:
            candidate = self._take_pending()
            if candidate is None:
                self._refresh_pending()
                candidate = self._take_pending()
            return candidate

    def mark_used(self, media_path: str | Path) -> bool:
        media = Path(media_path)
        if not media.exists():
            return False

        with self._lock:
            if media in self._used_files:
                return True

            destination = self.used_folder / media.name
            try:
                if destination.exists():
                    destination.unlink()
                shutil.move(str(media), str(destination))
            except OSError as exc:
                if self.logger is not None:
                    self.logger.warning("Unable to move used story media %s to %s: %s", media, destination, exc)
                return False

            self._used_files.add(media)
            if self.logger is not None:
                self.logger.info("Moved used story media %s to %s", media, destination)
            return True


def get_story_media_queue(source: str | Path, used_folder_name: str = "used", logger=None) -> StoryMediaQueueManager:
    source_path = Path(source).resolve()
    key = (str(source_path), used_folder_name)
    with _SHARED_QUEUE_LOCK:
        queue = _SHARED_QUEUES.get(key)
        if queue is None:
            queue = StoryMediaQueueManager(source_path, used_folder_name=used_folder_name, logger=logger)
            _SHARED_QUEUES[key] = queue
        return queue
