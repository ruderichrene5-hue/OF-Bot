"""Preflight check: verify everything the bot needs, before trusting a run.

On the server, most failures are environmental -- a token that expired, a missing
ffmpeg, a Drive folder nobody shared with the service account, a path that
doesn't exist. Those all surface as confusing mid-run errors. This turns them
into one readable report:

    python -m adb_bot.automation.run_loop doctor

Each check returns PASS / WARN / FAIL plus a fix hint. WARN means "this loop
can't run yet, but the rest can" (e.g. Drive not configured); FAIL means
something that is configured is broken.

Checks are small functions returning a CheckResult, so they're individually
testable and the report is just a loop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PASS


def _exe_version(exe: str, args=("--version",)) -> str | None:
    """First line of `exe --version`, or None if it can't run."""
    path = shutil.which(exe)
    if not path:
        return None
    try:
        proc = subprocess.run([path, *args], capture_output=True, text=True, timeout=20)
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        return out[0] if out else path
    except Exception:
        return path


# --- one live listing per doctor run -----------------------------------------

_probe_cache: dict = {}


def _probe(key, fetch):
    """Memoise a live listing for the length of one :func:`run_checks`.

    Two checks want the same MLX mobile-profile inventory (`check_multilogin`,
    `check_models`) and two want the same Drive subfolder listing (`check_drive`,
    `check_models`), so an otherwise idle doctor run made four API calls where
    two do -- every 30 minutes, on a MultiLogin token whose whole job is to still
    be valid when a loop needs it.

    Deliberately not a TTL cache: it is cleared at the top of `run_checks`, so
    "one run, one fetch" is the only guarantee it makes and a long-lived process
    can never be served a stale fleet. Failures are cached too, so a dead token
    is reported by both checks without being asked twice.
    """
    if key not in _probe_cache:
        try:
            _probe_cache[key] = (fetch(), None)
        except Exception as exc:
            _probe_cache[key] = (None, exc)
    value, error = _probe_cache[key]
    if error is not None:
        raise error
    return value


def _mlx_mobile_profiles(token: str) -> list:
    from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient

    return _probe(("mlx_mobile_profiles", token),
                  lambda: MultiloginMobileListClient(token).list_mobile_profiles())


class _CachedSubfolders:
    """A DriveClient stand-in holding one already-fetched subfolder listing.

    Lets `check_models` hand the real `DriveRawSource` the listing `check_drive`
    already paid for, instead of a second identical call. It answers only
    `list_subfolders`; anything that tried to download a file through it would
    (correctly) fail loudly, because nothing in a doctor run should.
    """

    def __init__(self, folders: list):
        self._folders = list(folders or [])

    def list_subfolders(self, _folder_id) -> list:
        return list(self._folders)


def _drive_subfolders(service_account_json: str, folder_id: str) -> list:
    from adb_bot.clients.gdrive import DriveClient

    return _probe(("drive_subfolders", service_account_json, folder_id),
                  lambda: DriveClient(service_account_json).list_subfolders(folder_id))


# --- individual checks -------------------------------------------------------

def check_adb() -> CheckResult:
    version = _exe_version("adb", ("version",))
    if not version:
        return CheckResult("ADB", FAIL, "adb not found on PATH",
                           "Install Android platform-tools and add it to PATH.")
    return CheckResult("ADB", PASS, version)


def check_ffmpeg() -> CheckResult:
    version = _exe_version("ffmpeg")
    if not version:
        return CheckResult("FFmpeg", WARN, "ffmpeg not found on PATH",
                           "Needed only by the spoofing pipeline. Install FFmpeg and add it to PATH.")
    return CheckResult("FFmpeg", PASS, version)


def check_uiautomator2() -> CheckResult:
    try:
        import uiautomator2  # noqa: F401
    except ImportError:
        return CheckResult("uiautomator2", FAIL, "not installed",
                           "pip install uiautomator2 -- the u2 flows (bio, reel) need it.")
    return CheckResult("uiautomator2", PASS, "importable")


def check_imaging() -> CheckResult:
    """OpenCV + Tesseract, the pair the screen verification depends on.

    Both imports are wrapped in try/except in the flows, so when they are
    missing nothing raises -- the blue-button classification and the OCR
    confirmations quietly turn into no-ops and flows report success without
    having verified anything. On a headless Linux server this is the most
    likely thing to be silently wrong: plain `opencv-python` links libGL.so.1,
    which a server with no desktop does not have.
    """
    missing = []
    try:
        import cv2  # noqa: F401
    except Exception as exc:                       # ImportError, or libGL OSError
        detail = str(exc)[:70]
        hint = ("Install opencv-python-headless (requirements.txt already selects it on "
                "Linux) -- the GUI wheel needs libGL.so.1."
                if "libGL" in detail or "cv2" in detail else "pip install -r requirements.txt")
        return CheckResult("Imaging (OpenCV/OCR)", FAIL, f"cv2 unusable: {detail}", hint)

    try:
        import pytesseract  # noqa: F401
    except ImportError:
        missing.append("pytesseract")
    from adb_bot.automation.flows.instagram import _resolve_tesseract_executable
    if _resolve_tesseract_executable() is None:
        missing.append("tesseract binary")
    if missing:
        return CheckResult("Imaging (OpenCV/OCR)", FAIL, f"cv2 ok, missing: {', '.join(missing)}",
                           "Linux: apt install tesseract-ocr. Otherwise OCR verification silently no-ops.")
    return CheckResult("Imaging (OpenCV/OCR)", PASS, "cv2 + tesseract available")


def check_desktop_ui() -> CheckResult:
    """tkinter, which the app's UI needs.

    Only relevant if you drive the server's UI over X11/VNC -- the loops
    themselves never import tkinter. It earns a check because on Linux tkinter
    is a *separate distro package* (`python3-tk`) that pip cannot install, so a
    working venv can still fail to open the app with a bare ImportError.
    """
    import sys
    try:
        import tkinter  # noqa: F401
    except ImportError:
        hint = ("apt install python3-tk (pip cannot install it)."
                if sys.platform.startswith("linux") else "Reinstall Python with tcl/tk support.")
        return CheckResult("Desktop UI (tkinter)", WARN, "tkinter not available", hint)
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                 or os.environ.get("WAYLAND_DISPLAY")):
        return CheckResult("Desktop UI (tkinter)", WARN, "tkinter present, no display attached",
                           "Headless is fine for the loops. For the UI use 'ssh -X' or VNC.")
    return CheckResult("Desktop UI (tkinter)", PASS, "available")


def check_mlx_agent() -> CheckResult:
    """The local MultiLogin agent that actually launches profiles.

    `check_multilogin` proves the cloud API and token work; this proves the
    thing on *this machine* that opens the phones is running. On a fresh server
    it is the most common missing piece, and its absence looks like a launch
    failure rather than a setup problem.
    """
    import socket
    from urllib.parse import urlparse
    from adb_bot.clients.multilogin.launcher import MultiloginLauncherClient

    url = urlparse(MultiloginLauncherClient("").base_url)
    host, port = url.hostname, url.port or 443
    try:
        with socket.create_connection((host, port), timeout=5):
            return CheckResult("MultiLogin agent", PASS, f"listening on {host}:{port}")
    except OSError as exc:
        return CheckResult("MultiLogin agent", FAIL, f"{host}:{port} unreachable ({exc.__class__.__name__})",
                           "Start the MultiLogin X desktop agent on this machine -- profiles "
                           "cannot launch without it.")


def check_airtable(token: str, base_id: str) -> CheckResult:
    """Token + base reachable, and the tables the loops read/write all exist."""
    if not token:
        return CheckResult("Airtable", FAIL, "no token configured",
                           "Set AIRTABLE_TOKEN (or save it in Dev controls).")
    from adb_bot.clients import airtable as at
    from adb_bot.clients.airtable import AirtableClient

    client = AirtableClient(token, base_id, at.TABLE_PROFILES)
    required = [at.TABLE_ACCOUNTS, at.TABLE_PROFILES, at.TABLE_RUN_LOG,
                at.TABLE_POSTING_QUEUE, at.TABLE_SPOOF_VARIANTS, at.TABLE_BAN_HISTORY]
    missing = []
    for table in required:
        try:
            client._list_table(table, page_size=1, max_records=1)
        except Exception as exc:
            text = str(exc)
            if "401" in text or "403" in text:
                return CheckResult("Airtable", FAIL, f"auth rejected ({text[:60]})",
                                   "Check the PAT and that it has access to this base.")
            missing.append(table)
    if missing:
        return CheckResult("Airtable", FAIL, f"base {base_id}: missing/unreadable tables: {', '.join(missing)}",
                           "Check AIRTABLE_BASE_ID and the PAT's table scopes.")
    return CheckResult("Airtable", PASS, f"base {base_id}, {len(required)} tables readable")


def check_multilogin(token: str) -> CheckResult:
    if not token:
        return CheckResult("MultiLogin", FAIL, "no token configured",
                           "Set MULTILOGIN_TOKEN (use the workspace Automation Token for unattended runs).")
    try:
        items = _mlx_mobile_profiles(token)
    except Exception as exc:
        text = str(exc)
        hint = ("Token expired? A regular MLX token lasts ~1h -- use the workspace Automation Token."
                if "401" in text else "Check network access to api.multilogin.com.")
        return CheckResult("MultiLogin", FAIL, text[:80], hint)
    return CheckResult("MultiLogin", PASS, f"{len(items)} mobile profile(s) visible")


def check_paths(raw_root: str, out_root: str, drive_folder: str) -> list:
    """Pipeline inputs/outputs. Raw may come from Drive instead of a local dir."""
    results = []
    if drive_folder:
        results.append(CheckResult("Pipeline raw source", PASS, f"Google Drive folder {drive_folder}"))
    elif raw_root:
        exists = Path(raw_root).is_dir()
        results.append(CheckResult(
            "Pipeline raw source", PASS if exists else FAIL,
            f"local {raw_root}" + ("" if exists else " (missing)"),
            "" if exists else "Create the folder or fix RAW_VIDEOS_DIR.",
        ))
    else:
        results.append(CheckResult("Pipeline raw source", WARN, "not configured",
                                   "Set DRIVE_RAW_FOLDER_ID (+ key) or RAW_VIDEOS_DIR."))

    if out_root:
        path = Path(out_root)
        if path.is_dir():
            results.append(CheckResult("Pipeline output dir", PASS, out_root))
        else:
            results.append(CheckResult("Pipeline output dir", WARN, f"{out_root} (will be created)",
                                       "The pipeline creates it on first run; check the drive has space."))
    else:
        results.append(CheckResult("Pipeline output dir", WARN, "not configured",
                                   "Set SPOOFED_VIDEOS_DIR."))
    return results


def check_drive(service_account_json: str, folder_id: str) -> CheckResult:
    if not folder_id and not service_account_json:
        return CheckResult("Google Drive", WARN, "not configured (using local raw folder)",
                           "Set DRIVE_RAW_FOLDER_ID + GOOGLE_SERVICE_ACCOUNT_JSON to read raw videos from Drive.")
    if not service_account_json or not folder_id:
        return CheckResult("Google Drive", FAIL, "half-configured",
                           "Drive needs BOTH DRIVE_RAW_FOLDER_ID and GOOGLE_SERVICE_ACCOUNT_JSON.")
    from adb_bot.clients.gdrive import DriveUnavailable
    try:
        folders = _drive_subfolders(service_account_json, folder_id)
    except DriveUnavailable as exc:
        return CheckResult("Google Drive", FAIL, str(exc)[:90],
                           "pip install google-api-python-client google-auth, and check the key path.")
    except Exception as exc:
        return CheckResult("Google Drive", FAIL, str(exc)[:90],
                           "Share the folder with the service account's email, and verify the folder id.")
    return CheckResult("Google Drive", PASS, f"{len(folders)} model folder(s) visible")


def check_models(airtable_token: str, base_id: str, mlx_token: str,
                 raw_root: str, drive_folder: str, service_account_json: str) -> CheckResult:
    """Do Drive, MultiLogin and Airtable agree on which models exist?

    The gap this closes: a model can be created in MultiLogin, cloned onto real
    phones and warmed up for a week while every posting code path is blind to it,
    because the join key is the *Airtable* profile name and `mlx_sync` writes
    that field only on create. The failure mode is silence -- zero variants, no
    error -- so the only way to catch it is to compare the systems on purpose.

    WARN, never FAIL: nothing here is broken, something is un-onboarded, and a
    FAIL would block `--apply` runs that are otherwise fine.

    A source that cannot be read makes the rules that depend on it *drop*, never
    run against an empty list. Without that, a Drive outage (or simply no
    DRIVE_RAW_FOLDER_ID) turned "I could not look" into "no model has a raw
    folder" and WARNed for every model on the fleet, hinting at Drive folders
    that already exist. What was skipped leads the detail line.
    """
    from adb_bot.automation import model_inventory, spoof_pipeline

    if not airtable_token:
        return CheckResult("Model inventory", WARN, "no Airtable token; cannot compare models",
                           "Set AIRTABLE_TOKEN.")

    mlx_profiles = mlx_folders = None
    partial = []
    try:
        from adb_bot.clients import airtable as at
        from adb_bot.clients.airtable import AirtableClient

        client = AirtableClient(airtable_token, base_id, at.TABLE_PROFILES)
    except Exception as exc:
        return CheckResult("Model inventory", WARN, f"Airtable unreadable: {str(exc)[:70]}")

    if mlx_token:
        try:
            from adb_bot.clients.multilogin.folders import MultiloginFolderClient

            mlx_profiles = _mlx_mobile_profiles(mlx_token)
            mlx_folders = MultiloginFolderClient(mlx_token).list_mobile_folders()
        except Exception as exc:
            mlx_profiles = mlx_folders = None
            partial.append(f"MultiLogin unreadable ({str(exc)[:40]})")
    else:
        partial.append("no MultiLogin token")

    source = None
    build_error = ""
    try:
        if drive_folder and service_account_json:
            # Reuse the listing check_drive just fetched rather than asking Drive
            # for the same folder twice. The real DriveRawSource still does the
            # name derivation, so the two paths cannot drift.
            source = spoof_pipeline.DriveRawSource(
                _CachedSubfolders(_drive_subfolders(service_account_json, drive_folder)),
                drive_folder)
        else:
            source = spoof_pipeline.build_source(raw_root, drive_folder, service_account_json)
    except Exception as exc:
        build_error = str(exc)[:40]

    try:
        inventory = model_inventory.collect(airtable=client, mlx_profiles=mlx_profiles,
                                            mlx_folders=mlx_folders, raw_source=source)
        findings = model_inventory.diff_models(inventory)
    except Exception as exc:
        return CheckResult("Model inventory", WARN, f"could not compare models: {str(exc)[:70]}")

    # The raw half of the comparison is either done or not done; there is no
    # half. When it is not done, `diff_models` has already dropped the two rules
    # that read raw folders, and the reader has to be told that BEFORE the
    # summary -- a trailing "[partial: ...]" reads as a footnote on a sentence
    # that has already claimed everything lines up.
    if not model_inventory.raw_source_known(inventory):
        why = build_error or inventory.raw_source_error or "no folders listed"
        partial.insert(0, f"raw source unreadable ({why}); "
                          f"raw-folder rules skipped")
    detail = model_inventory.summarise(findings)
    if partial:
        detail = f"[partial: {'; '.join(partial)}] {detail}"
    gaps = [f for f in findings if f.severity == model_inventory.WARN]
    if not gaps:
        return CheckResult("Model inventory", PASS, detail)
    return CheckResult("Model inventory", WARN, detail, gaps[0].hint)


def check_spoofer(spoofer_python: str, spoofer_root: str) -> CheckResult:
    if not spoofer_python or not spoofer_root:
        return CheckResult("Video spoofer", WARN, "not configured",
                           "Set SPOOFER_PYTHON + SPOOFER_ROOT so the pipeline can encode variants.")
    if not Path(spoofer_python).is_file():
        return CheckResult("Video spoofer", FAIL, f"interpreter missing: {spoofer_python}",
                           "Point SPOOFER_PYTHON at the video_spoofer venv's interpreter "
                           "(bin/python on Linux, Scripts\\python.exe on Windows).")
    if not Path(spoofer_root).is_dir():
        return CheckResult("Video spoofer", FAIL, f"project root missing: {spoofer_root}",
                           "Point SPOOFER_ROOT at the video_spoofer project folder.")
    try:
        proc = subprocess.run([spoofer_python, "-m", "video_testing_framework.cli", "--version"],
                              cwd=spoofer_root, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return CheckResult("Video spoofer", FAIL, str(exc)[:80], "Check the interpreter and project root.")
    if proc.returncode != 0:
        return CheckResult("Video spoofer", FAIL, (proc.stderr or "").strip()[-90:],
                           "Install the spoofer's deps in its venv (pip install -e .).")
    return CheckResult("Video spoofer", PASS, (proc.stdout or "").strip() or "CLI runs")


def check_scheduler() -> CheckResult:
    """Name the loops that are not scheduled, not just "none are".

    A loop with no timer runs only when a human types the command, so a partly
    installed set looks healthy until something quietly never happens (a
    `Verifying` row waiting on a recheck that is never triggered).
    """
    from adb_bot.automation import scheduling
    report = scheduling.timer_report()
    if not report.supported:
        return CheckResult("Scheduler", WARN, "no scheduler backend available", report.hint())
    return CheckResult("Scheduler", PASS if report.ok else WARN, report.summary(), report.hint())


def check_locks() -> CheckResult:
    """Surface stuck profile locks -- a crashed run leaves one behind, and a
    profile that looks 'busy' every cycle is usually this."""
    from adb_bot.core import locks
    try:
        held = [p.stem for p in locks.lock_dir().iterdir() if p.suffix == ".lock"]
    except OSError:
        return CheckResult("Profile locks", PASS, "none held")
    active = [name for name in held if locks.is_locked(name)]
    if not active:
        return CheckResult("Profile locks", PASS, "none held")
    return CheckResult("Profile locks", WARN, f"{len(active)} held: {', '.join(active[:5])}",
                       "Normal while a loop is running. If it persists, the holder died; "
                       "locks self-expire, or delete them in " + str(locks.lock_dir()))


def check_loop_production() -> CheckResult:
    """Report any loop the watchdog currently has flagged as stalled.

    The alert already went to `logs/alerts.log` when it tripped; this is so
    somebody who runs `doctor` an hour later still finds out, instead of having
    to know which log to read.
    """
    from adb_bot.automation import loop_watchdog

    try:
        # No sinks: this only reads the state files, it must never alert.
        states = loop_watchdog.LoopWatchdog(sinks=[]).snapshot()
    except Exception as exc:
        return CheckResult("Loop production", WARN, f"could not read the watchdog state: {exc}")
    # Drop doctor's own health entry, and only that one. It is written by
    # `observe_doctor` from the result of this very check, so reading it back
    # here would make a single failing check latch: doctor fails -> entry goes
    # unhealthy -> this check fails because that entry is unhealthy, and it
    # never recovers. Another loop's unhealthy entry has no such feedback path
    # and must be surfaced -- `issue-tags` reports itself this way, and dropping
    # every unhealthy entry would have hidden it completely.
    states = {name: s for name, s in states.items() if name != DOCTOR_WATCHDOG_LOOP}
    if not states:
        return CheckResult("Loop production", WARN, "no loop has reported yet",
                           "Expected until the scheduled loops have each run once.")
    stalled = [s for s in states.values() if s.state == loop_watchdog.STATE_STALLED]
    unhealthy = [s for s in states.values() if s.state == loop_watchdog.STATE_UNHEALTHY]
    if stalled or unhealthy:
        parts = []
        if stalled:
            parts.append(f"{len(stalled)} loop(s) producing nothing while work is due: "
                         + ", ".join(sorted(s.loop for s in stalled)))
        if unhealthy:
            parts.append(f"{len(unhealthy)} loop(s) failing: "
                         + ", ".join(sorted(s.loop for s in unhealthy)))
        return CheckResult("Loop production", FAIL, "; ".join(parts),
                           "See logs/alerts.log for when it started and what to check.")
    return CheckResult("Loop production", PASS,
                       f"{len(states)} loop(s) watched, none stalled")


# --- alerting ----------------------------------------------------------------

# The watchdog key doctor's own health is filed under. Not a loop in the
# production sense, but it shares the state dir, the sinks and the status line.
DOCTOR_WATCHDOG_LOOP = "doctor"

# Checks whose failure does not mean the setup is broken for the loops. The
# desktop UI is the standing example: a server has no display, that WARNs on
# every run, and an alert nobody can act on trains people to ignore alerts.
HEALTH_IGNORED_CHECKS = ("Desktop UI (tkinter)",)


def failing_checks(results, include_warnings: bool = False) -> list:
    """The check names a human should act on. FAILs always; WARNs on request."""
    bad = (FAIL, WARN) if include_warnings else (FAIL,)
    return [r.name for r in results
            if r.status in bad and r.name not in HEALTH_IGNORED_CHECKS]


def observe_doctor(watchdog, results, include_warnings: bool = False, now=None):
    """Report this doctor run to the watchdog, alerting on failing checks.

    Scheduled preflight is the point: the connectivity checks (MLX agent
    listening, Airtable readable, Drive reachable) previously only ran when a
    person asked, so a dead agent surfaced as failing launches an hour later
    rather than as an alert. Routed through the watchdog so it reuses one alert
    path, one storm guard and one status line.
    """
    failures = failing_checks(results, include_warnings=include_warnings)
    detail = ""
    if failures:
        by_name = {r.name: r for r in results}
        detail = "; ".join(f"{n}: {by_name[n].detail}" for n in failures if n in by_name)
    return watchdog.observe_health(DOCTOR_WATCHDOG_LOOP, failures,
                                   checked=len(results), detail=detail, now=now)


# --- report ------------------------------------------------------------------

def run_checks(settings_mod=None) -> list:
    """Run every check using the app's saved settings/env. Returns CheckResults."""
    if settings_mod is None:
        from adb_bot.config import settings as settings_mod

    # Every run reads the fleet fresh; only checks *within* one run share.
    _probe_cache.clear()

    airtable_token = settings_mod.get_saved_airtable_token()
    base_id = settings_mod.get_saved_airtable_base_id()
    mlx_token = settings_mod.get_saved_bearer_token()
    raw_root = settings_mod.get_saved_raw_videos_dir()
    out_root = settings_mod.get_saved_spoofed_videos_dir()
    drive_folder = settings_mod.get_saved_drive_folder_id()
    sa_json = settings_mod.get_saved_google_service_account_json()
    spoofer_python = settings_mod.get_saved_spoofer_python()
    spoofer_root = settings_mod.get_saved_spoofer_root()

    results = [
        check_airtable(airtable_token, base_id),
        check_multilogin(mlx_token),
        check_mlx_agent(),
        check_adb(),
        check_uiautomator2(),
        check_imaging(),
        check_ffmpeg(),
        check_desktop_ui(),
    ]
    results.extend(check_paths(raw_root, out_root, drive_folder))
    results.extend([
        check_drive(sa_json, drive_folder),
        check_models(airtable_token, base_id, mlx_token, raw_root, drive_folder, sa_json),
        check_spoofer(spoofer_python, spoofer_root),
        check_scheduler(),
        check_locks(),
        check_loop_production(),
    ])
    return results


def format_report(results: list) -> str:
    icons = {PASS: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    width = max((len(r.name) for r in results), default=10)
    lines = []
    for r in results:
        lines.append(f"{icons.get(r.status, '[????]')} {r.name.ljust(width)}  {r.detail}")
        if r.hint and r.status != PASS:
            lines.append(f"{' ' * (width + 9)}-> {r.hint}")
    fails = sum(1 for r in results if r.status == FAIL)
    warns = sum(1 for r in results if r.status == WARN)
    lines.append("")
    lines.append(f"{len(results)} checks: {len(results) - fails - warns} passed, {warns} warning(s), {fails} failure(s)")
    if fails:
        lines.append("Fix the [FAIL] items before running the loops with --apply.")
    elif warns:
        lines.append("Ready. The [WARN] items only limit the loops they belong to.")
    else:
        lines.append("All good.")
    return "\n".join(lines)


def exit_code(results: list) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0
