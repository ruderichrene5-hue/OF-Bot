import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Scheduled loops run unattended and often (the posting loop fires every ~10
# minutes), so log files must not grow without bound on the server.
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
LOG_BACKUP_COUNT = 5              # keep 5 rotations -> ~30 MB per loop, worst case


def get_logger(name: str = "adb_bot", log_file: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        base_format = "%(asctime)s | %(levelname)s | %(message)s"
        formatter = logging.Formatter(base_format)

        # Stream handler with ANSI colorized level names for terminals
        class _AnsiLevelFormatter(logging.Formatter):
            COLORS = {
                "INFO": "\u001b[32m",    # green
                "WARNING": "\u001b[33m", # yellow
                "ERROR": "\u001b[31m",   # red
            }
            RESET = "\u001b[0m"

            def format(self, record: logging.LogRecord) -> str:
                base = super().format(record)
                level = record.levelname
                color = self.COLORS.get(level)
                if color:
                    # Replace the level token only for terminal output
                    return base.replace(f" | {level} |", f" | {color}{level}{self.RESET} |")
                return base

        # Instagram text (usernames, captions, the post-confirmation banners
        # like "High five! 🙌") contains emoji/non-latin characters. On Windows
        # the console/file default to cp1252, which raises UnicodeEncodeError on
        # those, breaking log lines. Make the console tolerant (escape instead
        # of raise) and write the file as UTF-8 so nothing is lost.
        stream = sys.stderr
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setFormatter(_AnsiLevelFormatter(base_format))
        logger.addHandler(stream_handler)

        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
            )
            # Keep file logs uncolored
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

    return logger
