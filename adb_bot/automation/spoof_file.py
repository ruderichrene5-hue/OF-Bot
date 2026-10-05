"""Spoof ONE local video into N upload-ready variants. Command-line entry for
reel_pipeline's spoof_runner.py (the "10 Spoof" tick in Airtable Generate Reels,
Rene 2026-10-05: "Spoofer muss mit in die Tabelle ... manuell durchballern").

    /root/adb_bot/.venv/bin/python3 -m adb_bot.automation.spoof_file in.mp4 out_dir --variants 3 [--stem reel]

Prints a JSON list of the produced files. Same spoofer + normalize_for_upload as
manual_spoof.py (preset "normal", full size, 1080x1920/30fps re-encode), variants
run 4 at a time. Needs SPOOFER_PYTHON and SPOOFER_ROOT in the environment
(/etc/adbbot/env). Exit 1 when fewer variants than asked came out.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adb_bot.automation.spoof_pipeline import build_cli_spoofer, normalize_for_upload

log = logging.getLogger("spoof_file")
WORKERS = 4


def spoof_file(src: Path, out_dir: Path, variants: int, stem: str | None = None) -> list[Path]:
    spoof = build_cli_spoofer(os.environ["SPOOFER_PYTHON"], os.environ["SPOOFER_ROOT"], preset="normal")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", stem or src.stem).strip("_") or "reel"

    def make(v: int) -> Path | None:
        final = out_dir / f"{stem}_v{v}.mp4"
        if final.exists():
            return final
        tmp = out_dir / f".tmp_{stem}_v{v}"
        shutil.rmtree(tmp, ignore_errors=True)
        seed = zlib.crc32(f"{src.name}/{v}".encode()) % 100000
        produced = spoof(str(src), str(tmp), seed, log)
        if not produced:
            log.error("variant %d failed (seed %d)", v, seed)
            return None
        normalize_for_upload(str(produced), log)
        shutil.move(str(produced), final)
        shutil.rmtree(tmp, ignore_errors=True)
        return final

    with ThreadPoolExecutor(min(WORKERS, max(1, variants))) as pool:
        results = list(pool.map(make, range(1, variants + 1)))
    return [p for p in results if p]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src")
    ap.add_argument("out_dir")
    ap.add_argument("--variants", type=int, default=3)
    ap.add_argument("--stem")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    files = spoof_file(Path(a.src), Path(a.out_dir), a.variants, a.stem)
    print(json.dumps([str(p) for p in files]))
    return 0 if len(files) == a.variants else 1


if __name__ == "__main__":
    sys.exit(main())
