"""Continuous Drive uploader for manual_spoof scheduled slots.

17.10 Rene (livemessage, 15-UHR-Deadline verpasst): "Scheduled muss sofort hochgeladen werden,
nicht warten bis die Pipeline durch ist. Immer direkt mit hochladen bitte." Problem: manual_spoof.py
sammelt alle Jobs (scheduled + extras) in einen Pool, uploadet erst am Ende -- wenn Extras lange
dauern (Nikki hat 46×5 = 230 Extras), kriegen Account-Manager die Scheduled erst spät/nie.

This watcher runs as a side-car. Polls /opt/adbbot/manual_spoof/<today>/<model>/out/<slot>/ every
POLL seconds. If a slot has >= EXPECTED .mp4 files AND some are missing in Drive, uploads them
immediately. Idempotent (rclone copy skips existing), so running alongside manual_spoof's own
end-of-run upload is safe (whichever gets there first wins, the other no-ops).

Run as: nohup .venv/bin/python3 -m adb_bot.automation.manual_spoof_upload_watcher &
or install as systemd unit (deploy/systemd/adbbot-upload-watcher.service).
"""
from __future__ import annotations

import logging
import subprocess
import time
from datetime import date
from pathlib import Path

# Mirror of manual_spoof.REELS so this stays decoupled (importing manual_spoof pulls in
# ffmpeg/video_testing_framework which we do not need here).
REELS = {
    "Kathi": "MODELS/Kathi/Reels Instagram - Kathi",
    "Nikki": "MODELS/Corina OF/Reels Instagram - Nikki",
    "Mandy": "MODELS/Mandy OF/Reels Instagram - Mandy",
    "Lea":   "MODELS/Lea OF/Reels Instagram - Lea",
}
# 17.10 Rene: Lea bekommt KEIN Drive-upload (sie postet nur über die iPhone-Farm). Skip her
# so the watcher never pushes Lea slots even if someone drops files in her out/ folder.
SKIP_MODELS: set[str] = {"Lea"}

LOCAL_ROOT = Path("/opt/adbbot/manual_spoof")
RCLONE_CONFIG = "/root/.config/rclone/rclone.conf"
EXPECTED_PER_SLOT = 5                                # 10.10 Rene: "every model, every slot, always 5"
POLL_SECONDS = 30
LSF_TIMEOUT = 60
UPLOAD_TIMEOUT = 600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("upload_watcher")


def _rclone_lsf(target: str) -> set[str] | None:
    try:
        r = subprocess.run(
            ["rclone", "--config", RCLONE_CONFIG, "lsf", "--files-only", target],
            capture_output=True, text=True, timeout=LSF_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("lsf timeout: %s", target)
        return None
    if r.returncode != 0:
        # directory-not-found is normal when the slot has never been uploaded yet
        if "directory not found" in r.stderr:
            return set()
        log.warning("lsf rc %d on %s: %s", r.returncode, target, r.stderr[:200])
        return None
    return {ln for ln in r.stdout.splitlines() if ln}


def _rclone_upload(src_dir: Path, target: str, names: list[str]) -> bool:
    args = ["rclone", "--config", RCLONE_CONFIG, "copy",
            str(src_dir) + "/", target + "/"]
    for n in names:
        args += ["--include", n]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=UPLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.error("upload timeout for %s", target)
        return False
    if r.returncode != 0:
        log.error("upload rc %d for %s: %s", r.returncode, target, r.stderr[:300])
        return False
    return True


def sweep(day: str) -> int:
    """One pass over today's local output. Returns number of uploaded files."""
    uploaded = 0
    for model, reels_path in REELS.items():
        if model in SKIP_MODELS:
            continue
        base = LOCAL_ROOT / day / model / "out"
        if not base.exists():
            continue
        for slot_dir in sorted(base.iterdir()):
            if not slot_dir.is_dir():
                continue
            slot = slot_dir.name
            mp4s = sorted(p for p in slot_dir.iterdir() if p.suffix.lower() == ".mp4")
            if len(mp4s) < EXPECTED_PER_SLOT:
                continue                                                # slot not finished locally yet
            target = f"veralta:{reels_path}/Scheduled (Account Manager)/{day}/{slot}"
            have = _rclone_lsf(target)
            if have is None:
                continue
            missing = [p for p in mp4s if p.name not in have]
            if not missing:
                continue
            log.info("%s/%s: uploading %d missing files -> %s", model, slot, len(missing), target)
            if _rclone_upload(slot_dir, target, [p.name for p in missing]):
                uploaded += len(missing)
    return uploaded


def main() -> int:
    log.info("upload_watcher started, poll every %ds, skipping models %s",
             POLL_SECONDS, sorted(SKIP_MODELS) or "none")
    while True:
        try:
            n = sweep(date.today().isoformat())
            if n:
                log.info("sweep uploaded %d files", n)
        except Exception as e:                                          # noqa: BLE001
            log.error("sweep failed: %s", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
