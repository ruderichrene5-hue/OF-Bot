"""N extra spoofed copies of every clip in the Manuell Spoofer drop folder, into a separate Drive
folder (Rene 09.10: "alle Reels nochmal zwölfmal spoofen, in einen extra Ordner bei Multilogin").

    .venv/bin/python3 -m adb_bot.automation.extra_spoofs 2026-10-09 [--n 12] [--models Nikki,Kathi]

Drive: veralta:Multilogin IG Posting/Extra Spoofs/<date>/<Model>/<clip>/<clip>_x01.mp4 ...
Reuses the raws manual_spoof already pulled to /opt/adbbot/manual_spoof/<date>/<Model>/raw (pulls them
if missing). Seeds are derived from "<clip>/extra/<i>", so no copy equals a slot variant.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adb_bot.automation.manual_spoof import REELS, SRC, VIDEO_EXT, WORKERS
from adb_bot.automation.spoof_pipeline import build_cli_spoofer, normalize_for_upload

log = logging.getLogger("extra_spoofs")
DEST = "veralta:Multilogin IG Posting/Extra Spoofs"


def _raws(work: Path, model: str) -> list[Path]:
    raw = work / model / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rclone", "copy", f"{SRC}/{model}", str(raw), "-q"], check=False)   # incl. DE/ EN/ subfolders
    return sorted(p for p in raw.rglob("*") if p.suffix.lower() in VIDEO_EXT and "/out/" not in str(p))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--models", default=",".join(REELS))
    a = ap.parse_args(argv)
    work = Path("/opt/adbbot/manual_spoof") / a.date
    spoof = build_cli_spoofer(os.environ["SPOOFER_PYTHON"], os.environ["SPOOFER_ROOT"], preset="normal")
    jobs = []
    for model in a.models.split(","):
        for raw in _raws(work, model):
            stem = re.sub(r"[^A-Za-z0-9]+", "_", raw.stem).strip("_")
            sub = raw.parent.name if raw.parent.name in ("DE", "EN") else ""
            out_dir = work / "extra" / model / sub / stem
            out_dir.mkdir(parents=True, exist_ok=True)
            for i in range(1, a.n + 1):
                jobs.append((model, sub, raw, out_dir / f"{stem}_x{i:02d}.mp4", zlib.crc32(f"{raw.name}/extra/{i}".encode()) % 100000))
    log.info("%d copies to make", len(jobs))

    def make(job):
        model, sub, raw, final, seed = job
        if final.exists():
            return True
        tmp = final.parent / f".tmp_{final.stem}"
        shutil.rmtree(tmp, ignore_errors=True)
        produced = spoof(str(raw), str(tmp), seed, log)
        if not produced:
            log.error("spoof failed: %s seed %d", raw.name, seed)
            return False
        normalize_for_upload(str(produced), log)
        shutil.move(str(produced), final)
        shutil.rmtree(tmp, ignore_errors=True)
        return True

    with ThreadPoolExecutor(WORKERS) as pool:
        ok = sum(pool.map(make, jobs))
    log.info("spoofed %d/%d", ok, len(jobs))
    for model in a.models.split(","):
        src = work / "extra" / model
        if src.exists():
            subprocess.run(["rclone", "copy", str(src), f"{DEST}/{a.date}/{model}", "-q", "--exclude", ".tmp_*/**"], check=False)
            n = len(subprocess.run(["rclone", "lsf", "-R", "--files-only", f"{DEST}/{a.date}/{model}"],
                                   capture_output=True, text=True).stdout.split())
            log.info("%s: %d files in Drive", model, n)
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
