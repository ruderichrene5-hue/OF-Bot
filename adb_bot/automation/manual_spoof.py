"""Spoof the raw clips Rene drops into Drive 'Multilogin IG Posting/Manuell Spoofer/<Model>'
and file them where the account managers post from:

    <model reels folder>/Scheduled (Account Manager)/<date>/[DE|EN/]<15|17|19|21|23 UHR>/

Clip n of a model (number taken from the name, "kathi 3 I okt 4 I DE.mp4" -> 3; otherwise
name order) goes to the n-th time slot, as VARIANTS[model, lang] spoofed variants -- one per
account posting that slot. Models in RAW_ONLY are not spoofed here: their clips are copied
as they are to the Multilogin raw folder for <date>.
Nikki EN was discontinued 2026-10-14 (Rene: "Englisch gibt es nicht mehr") -- she is single-stream
like Kathi/Mandy now, no more DE/EN split.

    .venv/bin/python3 -m adb_bot.automation.manual_spoof 2026-10-04 [Nikki,Lea]   (needs /etc/adbbot/env;
    the optional list limits the run to those models)

Drive access is the rclone remote `veralta` (/root/.config/rclone/rclone.conf, OAuth as the
100 GB account that owns MODELS/ -- authorised by Rene 2026-10-04). The Google service
account can't be used: it may create folders but gets 403 "no storage quota" on uploads.
Re-running the same date skips variants already made (work dir /opt/adbbot/manual_spoof/<date>)
and never overwrites what is already in a slot folder. At the end every slot is listed back
from Drive and compared with what should be there ("manchmal ist es nicht drin").
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adb_bot.automation.spoof_pipeline import build_cli_spoofer, normalize_for_upload

log = logging.getLogger("manual_spoof")

SRC = "veralta:Multilogin IG Posting/Manuell Spoofer"
REELS = {"Kathi": "MODELS/Kathi/Reels Instagram - Kathi",
         "Nikki": "MODELS/Corina OF/Reels Instagram - Nikki",
         "Mandy": "MODELS/Mandy OF/Reels Instagram - Mandy",
         "Lea": "MODELS/Lea OF/Reels Instagram - Lea"}
SLOTS = ["15 UHR", "17 UHR", "19 UHR", "21 UHR", "23 UHR"]
# variants per time slot = accounts posting that slot (Rene 2026-10-04: "in jeder Uhrzeit
# 5 deutsch bei Nikki und englische 2, bei Kathi auch zwei, Mandy 3, Lea nur Multilogin raw";
# 2026-10-05: "bei Kati sind es vier Varianten")
# 2026-10-06: "Mickey 5 und Englisch auch 3" -> Nikki EN 3
# 2026-10-10, explicit instruction (chat): every model, every slot, always 5 -- raised Nikki
# EN/Kathi/Mandy/Lea to 5 (were 3/4/3/1).
VARIANTS = {("Nikki", None): 5, ("Kathi", None): 5, ("Mandy", None): 5,
            ("Lea", None): 5}
# models whose clips go unspoofed into the MLX pipeline's raw folder instead (Lea until 2026-10-08)
RAW_ONLY: dict[str, str] = {}
# One extra spoof per clip ("<clip>_mlx.mp4") for the Multilogin pipeline's raw folder, next to the
# account-manager slots (2026-10-08: "es sind keine Nikki Reels im ML-Ordner ... muss rein, auch Lea";
# Nikki got these by hand on 10-06, nobody did on 10-07).
_MLX_RAW = "veralta:Multilogin IG Posting/01_Raw_Videos"
MLX_SPOOF = {("Nikki", None): f"{_MLX_RAW}/Nikki I DE", ("Lea", None): f"{_MLX_RAW}/Lea"}
# 14.10, Rene: "Englisch gibt es nicht mehr" -- Nikki EN discontinued, she is single-stream like
# Kathi/Mandy now. Empty on purpose: nothing splits by language any more. The on-disk "DE" folder
# name is kept (MLX_SPOOF above, and the "extra" variants below) only because the iPhone-farm
# posting tool's own path is still hardcoded to .../extra/Nikki/DE/ -- not because German still
# means anything here.
LANG_SPLIT: set[str] = set()
WORKERS = 4
VIDEO_EXT = (".mp4", ".mov", ".m4v")
# 11.10: the iPhone farm's own posting tool (iphone_xx_post_batch.py) reads
# /opt/adbbot/manual_spoof/<date>/extra/Nikki/DE/<clip>/<clip>_xNN.mp4 -- local only, no Drive
# upload, one variant per posting account (not per Scheduled-slot account). This generation step
# existed through 10-09, then silently stopped (nothing in this file wrote "extra" anymore) and
# nobody noticed until the farm had grown to 40 accounts and a posting run needed it again.
# Rene: size it to the account count, not a fixed number like the Scheduled pipeline's VARIANTS.
EXTRA_VARIANTS = 50   # 17.10: 46 Healthy Nikki accounts, 40 variants were not enough
# Lea posts from the farm too (13 containers, E13-E25): one local variant per account, same layout as Nikki.
EXTRA_PER_MODEL = {"Nikki": EXTRA_VARIANTS, "Lea": 20, "Kathi": 5, "Luisa": 10, "Mandy": 15}   # 23.10: Nikki retired; Kathi (G/H), Luisa (I) start


def _lang(name: str) -> str | None:
    m = re.search(r"\bI\s+(DE|EN)\b", name, re.I)
    return m.group(1).upper() if m else None


def _lang_of(raw: Path) -> str | None:
    """Language from the clip's name ("… I DE") or, since 2026-10-06, from the DE/ or EN/
    subfolder Rene drops Nikki's clips into (downloaded clips keep their IG names)."""
    return _lang(raw.name) or (raw.parent.name.upper() if raw.parent.name.upper() in ("DE", "EN") else None)


def _number(name: str) -> int | None:
    m = re.match(r"\D*?(\d+)", os.path.splitext(name)[0])   # first number = clip number ("kathi 1 okt 7 I DE" -> 1)
    return int(m.group(1)) if m else None


def _fingerprint(raw: Path) -> tuple[str, str]:
    """Duration + a 16x16 frame at 1 s: a re-upload ("mandy 3 I 7. okt(1).mp4" was mandy 4,
    2026-10-07) differs by a few bytes, so file size/hash can't tell."""
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(raw)],
                         capture_output=True, text=True).stdout.strip()
    frame = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(raw), "-frames:v", "1",
                            "-vf", "scale=16:16", "-f", "rawvideo", "-"], capture_output=True).stdout
    return dur, str(zlib.crc32(frame))


def _drop_duplicates(model: str, raws: list[Path]) -> list[Path]:
    """Same clip uploaded twice under two names would take two time slots -- keep the one whose
    name has no "(1)" copy suffix (else the first by name)."""
    seen, keep = {}, []
    for raw in sorted(raws, key=lambda r: (bool(re.search(r"\(\d+\)$", r.stem)), r.name)):
        fp = _fingerprint(raw)
        if fp[0] and fp in seen:
            log.warning("%s: %s is the same clip as %s -- skipped", model, raw.name, seen[fp].name)
            continue
        seen[fp] = raw
        keep.append(raw)
    return keep


SEEN_PATH = Path("/opt/adbbot/manual_spoof/seen_clips.json")


def _seen() -> dict:
    try:
        import json
        return json.loads(SEEN_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _seen_save(d: dict) -> None:
    import json
    SEEN_PATH.write_text(json.dumps(d, indent=1))


def plan_all(model: str, raws: list[Path]) -> list[tuple[Path, str | None]]:
    """(raw, lang) for every raw, same grouping/order as plan() but with no 5-slot cap -- for
    outputs that don't live in a fixed time slot (11.10, Rene: clips 6-9 of a 9-clip day were
    silently dropped with just a warning; the MLX-raw extra spoof below now covers all of them,
    split 5/4 -- the first 5 keep their normal slot, the rest still get spoofed and uploaded)."""
    out = []
    lang_of = _lang_of if model in LANG_SPLIT else (lambda r: None)
    for lang in sorted({lang_of(r) for r in raws}, key=lambda x: x or ""):
        group = [r for r in raws if lang_of(r) == lang]
        group.sort(key=lambda r: (_number(r.name) is None, _number(r.name) or 0, r.name))
        out += [(raw, lang) for raw in group]
    return out


def plan(model: str, raws: list[Path]) -> list[tuple[Path, int, str]]:
    """(raw, day offset, slot path below the date folder) for every raw. 11.10, Rene: clips
    beyond 5 a day don't get dropped or just MLX-extra'd -- they roll onto the next day's slots
    (clip 6 -> tomorrow's slot 1, clip 7 -> tomorrow's slot 2, ... clip 11 would be the day after,
    and so on), restarting at slot 1 each day. day offset 0 = today's date folder."""
    out = []
    lang_of = _lang_of if model in LANG_SPLIT else (lambda r: None)
    for lang in sorted({lang_of(r) for r in raws}, key=lambda x: x or ""):
        group = [r for r in raws if lang_of(r) == lang]
        group.sort(key=lambda r: (_number(r.name) is None, _number(r.name) or 0, r.name))
        for i, raw in enumerate(group):
            day, slot_i = divmod(i, len(SLOTS))
            out.append((raw, day, f"{lang}/{SLOTS[slot_i]}" if lang else SLOTS[slot_i]))
    return out


def run(date: str, only: set[str] | None = None) -> bool:
    work = Path("/opt/adbbot/manual_spoof") / date
    spoof = build_cli_spoofer(os.environ["SPOOFER_PYTHON"], os.environ["SPOOFER_ROOT"], preset="normal")
    jobs, expected, out_dirs = [], {}, {}
    for model, reels in REELS.items():
        if only and model not in only:                        # e.g. yesterday's Kathi/Mandy clips still in the drop folder
            continue
        raw_dir = work / model / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rclone", "copy", f"{SRC}/{model}", str(raw_dir), "--max-depth", "1", "-q"], check=True)
        raws = [p for p in raw_dir.iterdir() if p.suffix.lower() in VIDEO_EXT]
        if model in LANG_SPLIT:                               # Nikki: DE/ and EN/ subfolders too
            for lang in ("DE", "EN"):
                sub = raw_dir / lang
                sub.mkdir(exist_ok=True)
                subprocess.run(["rclone", "copy", f"{SRC}/{model}/{lang}", str(sub), "--max-depth", "1", "-q"])
                raws += [p for p in sub.iterdir() if p.suffix.lower() in VIDEO_EXT]
        raws = _drop_duplicates(model, raws)
        # 25.10: clips stay in the Drive drop folder after their day; the 08:40 timer took 24.10's clips again and
        # filed them into 25.10's slots, pushing the new clips to the 26th. A clip (by fingerprint) belongs to the
        # first date it was spoofed for -- later dates skip it and drop it from their work folder.
        seen = _seen()
        fresh = []
        for raw in raws:
            fp = f"{model}|" + "|".join(_fingerprint(raw))      # per model: the same clip may go to two models
            first = seen.get(fp)
            if first and first < date and os.environ.get("MANUAL_SPOOF_ALLOW_REPEAT") != "1":   # Rene re-uploads on purpose
                log.info("%s: %s already spoofed for %s -- skipped", model, raw.name, first)
                raw.unlink(missing_ok=True)
                continue
            seen.setdefault(fp, date)
            fresh.append(raw)
        _seen_save(seen)
        raws = fresh
        log.info("%s: %d raws", model, len(raws))
        if model in RAW_ONLY:
            if raws:
                target = f"{RAW_ONLY[model]}/{date}"
                subprocess.run(["rclone", "copy", str(raw_dir), target, "-q"], check=True)
                expected[target] = sorted(p.name for p in raws)
            continue
        lang_of = _lang_of if model in LANG_SPLIT else (lambda r: None)
        for raw, _lang in plan_all(model, raws):
            key = (model, lang_of(raw))
            if key in MLX_SPOOF:
                target = f"{MLX_SPOOF[key]}/{date}"
                out_dir = work / model / "mlx" / (key[1] or "all")
                out_dir.mkdir(parents=True, exist_ok=True)
                name = f"{re.sub(r'[^A-Za-z0-9]+', '_', raw.stem).strip('_')}_mlx.mp4"
                expected.setdefault(target, []).append(name)
                out_dirs[target] = out_dir
                jobs.append((model, raw, out_dir / name, zlib.crc32(f"{raw.name}/mlx".encode()) % 100000))
        for raw, day, slot in plan(model, raws):
            target_date = (dt.date.fromisoformat(date) + dt.timedelta(days=day)).isoformat()
            target = f"veralta:{reels}/Scheduled (Account Manager)/{target_date}/{slot}"
            out_dir = work / model / "out" / (slot if not day else f"+{day}d_{slot}")
            out_dir.mkdir(parents=True, exist_ok=True)
            n = VARIANTS[model, slot.split("/")[0] if "/" in slot else None]
            names = [f"{re.sub(r'[^A-Za-z0-9]+', '_', raw.stem).strip('_')}_v{v}.mp4" for v in range(1, n + 1)]
            expected[target] = names
            out_dirs[target] = out_dir
            for v, name in enumerate(names, 1):
                jobs.append((model, raw, out_dir / name, zlib.crc32(f"{raw.name}/{v}".encode()) % 100000))
        if model in EXTRA_PER_MODEL and os.environ.get("MANUAL_SPOOF_NO_EXTRA") != "1":   # 16.10, Rene: model slots only, farm variants later
            for raw, _lang in plan_all(model, raws):
                clipname = re.sub(r"[^A-Za-z0-9]+", "_", raw.stem).strip("_")
                out_dir = work / "extra" / model / "DE" / clipname
                out_dir.mkdir(parents=True, exist_ok=True)
                for v in range(1, EXTRA_PER_MODEL[model] + 1):
                    name = f"{clipname}_x{v:02d}.mp4"
                    jobs.append((model, raw, out_dir / name, zlib.crc32(f"{raw.name}/extra{v}".encode()) % 100000))

    def make(job):
        model, raw, final, seed = job
        if final.exists():
            return True
        tmp = final.parent / f".tmp_{final.stem}"
        shutil.rmtree(tmp, ignore_errors=True)
        produced = spoof(str(raw), str(tmp), seed, log)
        if not produced:
            log.error("%s: spoof failed for %s (seed %d)", model, raw.name, seed)
            return False
        normalize_for_upload(str(produced), log)
        shutil.move(str(produced), final)
        shutil.rmtree(tmp, ignore_errors=True)
        return True

    with ThreadPoolExecutor(WORKERS) as pool:
        ok = sum(pool.map(make, jobs))
    log.info("spoofed %d/%d variants", ok, len(jobs))

    complete = True
    for target, names in expected.items():
        if any(target.startswith(r) for r in RAW_ONLY.values()):
            have = set(subprocess.run(["rclone", "lsf", "--files-only", target], capture_output=True, text=True).stdout.split("\n"))
            missing = [n for n in names if n not in have]
            log.info("%s (raw): %d/%d in Drive%s", target, len(names) - len(missing), len(names),
                     f" -- MISSING {missing}" if missing else "")
            complete &= not missing
            continue
        out_dir = out_dirs[target]
        label = target.removeprefix("veralta:").replace("Multilogin IG Posting/01_Raw_Videos/", "MLX raw/")
        have = set(subprocess.run(["rclone", "lsf", "--files-only", target], capture_output=True, text=True).stdout.split("\n"))
        todo = [n for n in names if n not in have and (out_dir / n).exists()]
        if todo:
            subprocess.run(["rclone", "copy", str(out_dir), target, "-q"]
                           + [a for n in todo for a in ("--include", n)], check=True)
        have = set(subprocess.run(["rclone", "lsf", "--files-only", target], capture_output=True, text=True).stdout.split("\n"))
        missing = [n for n in names if n not in have]
        log.info("%s: %d/%d in Drive%s", label, len(names) - len(missing), len(names),
                 f" -- MISSING {missing}" if missing else "")
        complete &= not missing
    return complete


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sys.exit(0 if run(sys.argv[1], set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None) else 1)
