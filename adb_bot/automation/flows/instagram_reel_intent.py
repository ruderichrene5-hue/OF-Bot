"""Probe: can an Android share Intent drop Instagram straight into the Reel
composer, skipping the picker navigation?

The proposal this tests is:

    adb push reel.mp4 /sdcard/Download/reel.mp4
    adb shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://...
    adb shell am start -a android.intent.action.SEND -t video/mp4 \\
        --eu android.intent.extra.STREAM file:///sdcard/Download/reel.mp4 -p com.instagram.android

Steps 1-2 already exist in `_adb_push_media_to_device` (with a MediaStore index
wait the proposal omits), so this flow reuses them and concentrates on step 3.

Two things make the outcome genuinely uncertain, which is why this measures
rather than assumes:

- **`file://` should fail.** Since Android 7 a `file://` URI in a cross-app
  Intent raises FileUriExposedException, and since Android 10's scoped storage
  Instagram cannot read a raw `/sdcard` path anyway. The correct form is a
  `content://media/external/video/media/<id>` URI plus a read grant. But getting
  that id needs `content query`, which `_adb_wait_for_media_store_index` already
  documents as returning nothing on the MLX cloud phones -- so the fix may be
  blocked on this hardware specifically. Variant B measures exactly that.
- **ACTION_SEND is not documented to reach the Reel composer.** Instagram routes
  external shares through its own handler, which historically offers a
  destination sheet or leans toward Story for video. Landing on Story would be
  worse than today, not better.

So this flow **does not post**. It pushes one video, fires each Intent variant,
records where Instagram actually lands, and returns a verdict. Pass
`post_after_probe=True` to let a landing that reaches a real composer continue
into the normal caption/share path.

Even in the best case this replaces only the first three of the reel flow's
seven stages -- caption entry, Share and post verification are unavoidable UI
work. Read the verdict with that ceiling in mind.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from adb_bot.core.models import Profile
from adb_bot.core.proc import adb as _adb_run
from adb_bot.automation.flows.instagram import (
    _OFFLINE_MARKERS,
    _adb_get_foreground_activity,
    _adb_push_media_to_device,
    _adb_reconnect_device,
    _adb_resolve_story_media_path,
    _adb_verify_remote_media_exists,
)
from adb_bot.automation.flows.story_media import discover_story_media_files

IG_PACKAGE = "com.instagram.android"

# `discover_story_media_files` accepts images too -- it serves Story uploads,
# which take photos. A reel probe must push a real video: sending a .jpg with
# `-t video/mp4` is a mimetype lie that Instagram will reject, and the run tells
# you nothing about whether the Intent works.
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# Screens worth telling apart in the verdict.
#
# A bare "share" marker is deliberately NOT here. It matches
# DirectExternalMediaShareActivity and StoryShareHandlerActivity, both of which
# are the *wrong* surface -- treating them as a composer would report success for
# an Intent that actually shares to DMs. Only reel/creation-specific names count.
_COMPOSER_MARKERS = ("clipsshare", "clips", "reels", "creation", "mediacapture", "gallery")
# Instagram's external-share router. Landing here means the Intent was accepted
# but the destination is still undecided -- neither success nor failure.
_ROUTER_MARKERS = ("sharehandler", "handleractivity", "externalshare")
_DIRECT_MARKERS = ("direct",)
_STORY_MARKERS = ("story",)
_CHOOSER_MARKERS = ("resolver", "chooser", "sharesheet")
# Landing on the plain feed *after* aiming at a share handler is its own result:
# the Intent was delivered (am confirms `cmp=`), the handler looked at the media,
# could not use it, and redirected. That is a media problem, not a routing one.
_MAIN_MARKERS = ("mainactivity",)


class InstagramReelIntentProbeFlow:
    """Measure whether a share Intent can replace the reel composer navigation.

    Diagnostic by default: pushes media, tries each Intent variant, reports where
    Instagram landed, and leaves the account untouched.
    """

    name = "instagram_reel_intent_probe"
    # Every working flow pushes here, and for good reason: a push to /sdcard/DCIM
    # reported success ("11964305 bytes in 20.419s") and the file was then simply
    # absent -- DCIM is a MediaStore-managed collection, and under scoped storage
    # the media provider can reject or remove what adb drops in. /sdcard/Download
    # is not managed that way and the push sticks.
    remote_directory = "/sdcard/Download"

    def __init__(self, post_after_probe: bool = False, watch_seconds: int = 45,
                 hold_seconds: int = 45):
        # Off by default on purpose: an untested share Intent that lands on the
        # Story composer would publish to the wrong surface, and that cannot be
        # undone.
        self.post_after_probe = post_after_probe
        # How long to follow the foreground activity after each Intent. The
        # routing activity can sit for several seconds before either opening the
        # composer or giving up, so a short sample reads as success either way.
        self.watch_seconds = watch_seconds
        # After the last variant, re-fire the best one and leave it on screen so
        # a human can actually look at the phone. The probe otherwise finishes,
        # reports success, and the runner shuts the profile down immediately --
        # which is exactly what stopped you seeing the result.
        self.hold_seconds = hold_seconds

    def get_progress_total_steps(self, target: str) -> int:
        return 5

    # --- helpers -------------------------------------------------------------

    def _build_remote_media_path(self, local_media_path: str) -> str:
        safe_name = Path(local_media_path).name.replace(" ", "_")
        return f"{self.remote_directory}/{safe_name}"

    @staticmethod
    def _shell(target: str, command: str, timeout: int = 25):
        """One device command as a single argv element -- see core.proc.adb."""
        return _adb_run("-s", target, "shell", command,
                        check=False, capture_output=True, text=True, timeout=timeout)

    def _resolve_content_uri(self, target: str, remote_path: str, emit) -> str | None:
        """MediaStore `content://` URI for a pushed file, or None.

        Several strategies, because the obvious one is wrong on modern Android.
        `_data` (the raw filesystem path) is deprecated under scoped storage and
        commonly returns nothing on Android 10+ -- which is very likely what the
        "content query returns nothing at all on the MLX cloud phones" note in
        `_adb_wait_for_media_store_index` actually ran into. `_display_name` is
        the supported column and is tried first.

        This matters more than it looks: Instagram's reel share handler accepts a
        `file://` URI and then silently bounces to the main feed, because scoped
        storage stops it reading the raw path. A content URI is the only form it
        can actually open.
        """
        basename = remote_path.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0]
        strategies = [
            ("_display_name", f"_display_name='{basename}'"),
            ("_data", f"_data='{remote_path}'"),
            ("title", f"title='{stem}'"),
        ]
        for table in ("video", "file"):
            uri_base = f"content://media/external/{table}/media"
            for column, where in strategies:
                command = (f"content query --uri {uri_base} "
                           f"--projection _id --where \"{where}\"")
                try:
                    result = self._shell(target, command)
                except Exception as exc:
                    emit("info", "content query failed (%s/%s): %s", table, column, exc)
                    continue
                output = (result.stdout or "").strip()
                match = re.search(r"_id=(\d+)", output)
                if match:
                    uri = f"{uri_base}/{match.group(1)}"
                    emit("info", "Resolved MediaStore URI via %s on '%s': %s", column, table, uri)
                    return uri
                emit("info", "  no id from %s/%s (%r)", table, column, output[:80])

            # Last resort: list the newest rows and match the name in the dump.
            # Works even when every --where form is restricted.
            try:
                listing = self._shell(
                    target, f"content query --uri {uri_base} "
                            f"--projection _id:_display_name --sort \"date_added DESC\"",
                    timeout=40)
            except Exception:
                continue
            for line in (listing.stdout or "").splitlines():
                if basename in line:
                    match = re.search(r"_id=(\d+)", line)
                    if match:
                        uri = f"{uri_base}/{match.group(1)}"
                        emit("info", "Resolved MediaStore URI by listing '%s': %s", table, uri)
                        return uri
        return None

    def _classify(self, activity: str | None) -> str:
        """Bucket an activity into something decision-useful.

        Accepts either form: `com.instagram.android/com.instagram.foo.BarActivity`
        from dumpsys, or a bare `com.instagram.foo.BarActivity` from
        query-activities -- the latter has no package prefix, so matching on the
        full package name would wrongly call every one of them "left-instagram".

        Order matters. Direct and Story are checked before the composer markers
        because those names also contain "Share", and reporting a DM share as a
        reel composer would be the one failure that wastes the most time.
        """
        if not activity:
            return "unknown"
        low = activity.lower()
        if any(marker in low for marker in _CHOOSER_MARKERS):
            return "chooser"
        if "instagram" not in low:
            return "left-instagram"
        if any(marker in low for marker in _DIRECT_MARKERS):
            return "direct"
        if any(marker in low for marker in _STORY_MARKERS):
            return "story"
        # Router BEFORE composer. `ReelShareHandlerActivity` contains "reel", but
        # it is the routing activity, not the editor -- it decides where the
        # media goes and then hands off (or gives up and returns to the feed).
        # Calling it a composer on a single early sample is how a run that later
        # bounced would be scored as a success.
        if any(marker in low for marker in _ROUTER_MARKERS):
            return "share-router"
        if any(marker in low for marker in _COMPOSER_MARKERS):
            return "composer"
        if any(marker in low for marker in _MAIN_MARKERS):
            return "bounced-to-feed"
        return "instagram-other"

    def _watch_landing(self, target: str, emit, log, seconds: int = 20):
        """Follow the foreground activity for a while and report the whole path.

        One sample a few seconds in cannot distinguish "arrived at the composer"
        from "still on the routing activity, about to bounce back to the feed" --
        and we have already seen a run do exactly that. What decides the result
        is where it *settles*, so this records the transitions and returns the
        final activity plus the sequence.
        """
        sequence = []
        deadline = time.time() + seconds
        while True:                      # always sample at least once
            current = _adb_get_foreground_activity(target, logger=log) or self._foreground_any(target)
            if current and (not sequence or sequence[-1] != current):
                sequence.append(current)
                emit("info", "    t+%2ds  %s  -> %s",
                     max(0, int(seconds - (deadline - time.time()))), current,
                     self._classify(current).upper())
            if time.time() >= deadline:
                break
            time.sleep(2.0)
        return (sequence[-1] if sequence else None), sequence

    def _is_offline(self, target: str) -> bool:
        """True when the adb tunnel is down rather than the device saying no.

        These MLX tunnels drop -- a 12 MB push at 0.6 MB/s takes 20 s and the
        connection does not always survive it. Without this check every later
        command fails and the probe blames the wrong thing: a dropped tunnel
        reported "Pushed file not found", which sends you looking at storage
        paths instead of the connection.
        """
        result = _adb_run("-s", target, "get-state", check=False,
                          capture_output=True, text=True, timeout=15)
        combined = f"{result.stdout or ''} {result.stderr or ''}".strip().lower()
        if not combined:
            return True
        return combined != "device" and any(m in combined for m in _OFFLINE_MARKERS)

    def _ensure_online(self, target: str, emit, log) -> bool:
        """Reconnect a dropped tunnel, using the same recovery the flows use."""
        if not self._is_offline(target):
            return True
        emit("warning", "adb tunnel to %s is offline; reconnecting", target)
        if _adb_reconnect_device(target, logger=log):
            emit("info", "Reconnected to %s", target)
            return True
        emit("warning", "Could not bring %s back online", target)
        return False

    def _enumerate_share_targets(self, target: str, emit) -> list:
        """Exact `package/activity` names that accept a video SEND.

        `--brief` prints one component per line instead of the full ActivityInfo
        dump, whose sourceDir/splitSourceDirs noise buried the aliases last time.
        These aliases are what the system chooser shows as "Instagram Feed",
        "Instagram Reels", "Instagram Stories" -- so naming one with `-n` is what
        skips the chooser.
        """
        commands = (
            f"cmd package query-activities --brief -a android.intent.action.SEND "
            f"-t video/mp4 -p {IG_PACKAGE}",
            f"cmd package query-activities --brief -a android.intent.action.SEND -t video/mp4",
            f"cmd package query-activities -a android.intent.action.SEND -t video/mp4",
        )
        for command in commands:
            try:
                result = self._shell(target, command, timeout=30)
            except Exception:
                continue
            out = result.stdout or ""
            found = []
            for match in re.finditer(rf"({re.escape(IG_PACKAGE)}/[\w.$]+)", out):
                component = match.group(1)
                if component not in found:
                    found.append(component)
            # The verbose form reports the alias target on its own; keep those too.
            for match in re.finditer(r"targetActivity=([\w.$]*instagram[\w.$]*)", out):
                component = f"{IG_PACKAGE}/{match.group(1)}"
                if component not in found:
                    found.append(component)
            if found:
                return found
        emit("info", "Could not enumerate share targets on this build.")
        return []

    def _foreground_any(self, target: str) -> str | None:
        """Whatever is in front, Instagram or not.

        `_adb_get_foreground_activity` returns None unless Instagram is the
        foreground app, so on its own it cannot distinguish "the Intent was
        rejected and we're still on the launcher" from "dumpsys failed". That
        difference decides whether a run is a real negative or just broken.
        """
        for command in ("dumpsys activity activities", "dumpsys window"):
            try:
                result = self._shell(target, command, timeout=15)
            except Exception:
                continue
            out = result.stdout or ""
            for pattern in (r"topResumedActivity[^\n]*?([\w.]+/[\w./]+)",
                            r"mResumedActivity[^\n]*?([\w.]+/[\w./]+)",
                            r"mCurrentFocus[^\n]*?([\w.]+/[\w./]+)",
                            r"mFocusedApp[^\n]*?([\w.]+/[\w./]+)"):
                match = re.search(pattern, out)
                if match:
                    return match.group(1)
        return None

    def _reset_instagram(self, target: str) -> None:
        """Force-stop between variants so each one starts from the same place."""
        self._shell(target, f"am force-stop {IG_PACKAGE}")
        time.sleep(1.5)

    @staticmethod
    def _looks_like_reels(component: str) -> bool:
        low = component.lower()
        return "clips" in low or "reels" in low

    def _intent_variants(self, remote_path: str, content_uri: str | None,
                         share_targets: list | None = None) -> list:
        """The Intent forms worth measuring, weakest-to-strongest.

        `-p <package>` only narrows to the app. Instagram registers several video
        SEND aliases (Feed, Messages, Reels, Stories), so the system still has to
        ask which -- that is the chooser the first real run stopped on. Naming the
        component with `-n` is what removes that step, which makes the Reels alias
        the whole point of this probe.

        `--grant-read-uri-permission` lets the receiving app open a content URI;
        without it even a correct URI is unusable.
        """
        file_uri = f"file://{remote_path}"
        base = f"am start -a android.intent.action.SEND -t video/mp4"
        variants = [
            ("A: file:// + -p (the original proposal)",
             f"{base} --eu android.intent.extra.STREAM {file_uri} -p {IG_PACKAGE}"),
        ]

        # The interesting ones: aim straight at each Reels-looking alias, so no
        # chooser can appear. Both URI forms, since which one Instagram can
        # actually read is the remaining unknown.
        for component in (share_targets or []):
            if not self._looks_like_reels(component):
                continue
            variants.append(
                (f"B: file:// -> {component.split('/')[-1]}",
                 f"{base} --eu android.intent.extra.STREAM {file_uri} -n {component}"))
            if content_uri:
                variants.append(
                    (f"C: content:// -> {component.split('/')[-1]}",
                     f"{base} --eu android.intent.extra.STREAM {content_uri} "
                     f"--grant-read-uri-permission -n {component}"))

        if content_uri:
            variants.append(
                ("D: content:// + -p",
                 f"{base} --eu android.intent.extra.STREAM {content_uri} "
                 f"--grant-read-uri-permission -p {IG_PACKAGE}"))
        return variants

    # --- flow ----------------------------------------------------------------

    def run(
        self,
        profile: Profile,
        adb_client=None,
        logger=None,
        should_stop=None,
        status_callback=None,
        manual_continue_event=None,
        manual_continue_callback=None,
    ) -> dict[str, Any]:
        if not adb_client:
            raise ValueError("adb_client is required")
        if not profile.target:
            raise ValueError("Profile target is missing")

        log = logger
        target = profile.target

        def emit(level: str, message: str, *args) -> None:
            target_logger = log if log is not None else print
            method = getattr(target_logger, level, None)
            if callable(method):
                method(message, *args)
            else:
                print(message % args if args else message)

        def mark_step() -> None:
            if hasattr(adb_client, "mark_progress_step"):
                adb_client.mark_progress_step()

        def aborted() -> bool:
            return callable(should_stop) and should_stop()

        emit("info", "=== Reel share-Intent probe for %s (posting %s) ===",
             target, "ENABLED" if self.post_after_probe else "disabled -- diagnostic only")

        # --- 1. Which activities even claim to handle a video SEND? -----------
        # Answers "is there a Reels share target at all" before any Intent flies.
        share_targets = self._enumerate_share_targets(target, emit)
        if share_targets:
            emit("info", "Instagram components accepting SEND video/mp4:")
            for component in share_targets:
                tag = "  <-- REELS" if self._looks_like_reels(component) else ""
                emit("info", "    %-72s %s%s", component,
                     self._classify(component).upper(), tag)
            if not any(self._looks_like_reels(c) for c in share_targets):
                emit("info", "No reels/clips alias found by name. The chooser may still "
                             "offer one -- compare this list against what it shows.")

        handler_names = list(share_targets)
        try:
            result = self._shell(
                target, f"cmd package query-activities -a android.intent.action.SEND -t video/mp4")
            # Pull out just the activity class names. `name=` is the activity,
            # `targetActivity=` the real target behind an <activity-alias>.
            # Everything else in this dump (sourceDir, splitSourceDirs, dataDir...)
            # is noise that buries the one line that matters.
            for match in re.finditer(r"(?:^|\s)(?:name|targetActivity)=([\w.]*instagram[\w.]*)",
                                     result.stdout or ""):
                candidate = match.group(1)
                if "." in candidate and candidate not in handler_names:
                    handler_names.append(candidate)
            if handler_names:
                emit("info", "Instagram activities registered for SEND video/mp4:")
                for handler in handler_names:
                    emit("info", "    %-70s -> %s", handler, self._classify(handler).upper())
                # Ask whether a *reels* target exists, not whether one of these is
                # a composer -- they are all handlers by nature, so the composer
                # test always failed and warned even when a Reels alias was right
                # there in the list.
                if not any(self._looks_like_reels(h) for h in handler_names):
                    emit("warning", "No Reels alias among these. If the only handlers are "
                                    "Direct/Story, Instagram does not expose a Reels share target "
                                    "and the Intent shortcut cannot work by design.")
            else:
                emit("info", "No activity names reported (some builds restrict this query).")
        except Exception as exc:
            emit("info", "query-activities unavailable: %s", exc)
        mark_step()

        # --- 2. Media: resolve + push (reuses the production path) ------------
        media_setting = getattr(profile, "media_path", None) or _adb_resolve_story_media_path(logger=log)
        if media_setting is None:
            emit("warning", "No media available to probe with for profile %s", profile.id)
            return {"profile_id": profile.id, "target": target, "success": False,
                    "reason": "no media configured"}

        # The setting is usually a *folder* (the per-folder reel mapping), so a
        # real video has to be picked out of it -- pushing the folder itself just
        # fails. Unlike the posting flows this uses `discover_story_media_files`
        # rather than the media queue on purpose: the queue marks a clip as
        # assigned, which would quietly pull it out of rotation for the next real
        # post. A diagnostic must not consume production media.
        found = discover_story_media_files(media_setting, logger=log)
        candidates = [p for p in found if p.suffix.lower() in VIDEO_EXTENSIONS]
        if not candidates:
            skipped = ", ".join(sorted({p.suffix.lower() for p in found})) or "nothing"
            emit("warning", "No video at %s -- found %s. A reel probe needs a real video "
                            "(%s); point the flow's media folder at one.",
                 media_setting, skipped, "/".join(sorted(VIDEO_EXTENSIONS)))
            return {"profile_id": profile.id, "target": target, "success": False,
                    "reason": f"no video file at {media_setting} (found: {skipped})"}
        media_path = str(candidates[0])
        if len(candidates) > 1:
            emit("info", "Probing with the first of %s clips at %s", len(candidates), media_setting)

        remote_path = self._build_remote_media_path(media_path)
        emit("info", "Pushing probe media %s -> %s", media_path, remote_path)
        # Also runs the MEDIA_SCANNER broadcast and waits for the index.
        if not _adb_push_media_to_device(target, media_path, remote_path, logger=log):
            emit("warning", "adb push failed for %s", target)
            return {"profile_id": profile.id, "target": target, "success": False,
                    "reason": "push failed"}
        if not _adb_verify_remote_media_exists(target, remote_path, logger=log):
            # Distinguish a dropped tunnel from a genuinely missing file: those
            # need completely different fixes, and the verification helper cannot
            # tell them apart on its own.
            if self._is_offline(target):
                emit("warning", "Verification failed because the adb tunnel dropped, not "
                                "because the file is missing. Reconnecting and re-checking.")
                if self._ensure_online(target, emit, log) and \
                        _adb_verify_remote_media_exists(target, remote_path, logger=log):
                    emit("info", "File confirmed on device after reconnect: %s", remote_path)
                else:
                    return {"profile_id": profile.id, "target": target, "success": False,
                            "reason": "adb tunnel offline -- could not verify the push. "
                                      "Re-run; the media location is not the problem."}
            else:
                emit("warning", "Pushed file not found on device at %s", remote_path)
                return {"profile_id": profile.id, "target": target, "success": False,
                        "reason": "pushed file missing"}
        mark_step()

        # --- 3. Can we build the correct (content://) Intent on this device? --
        content_uri = self._resolve_content_uri(target, remote_path, emit)
        if content_uri is None:
            emit("warning", "MediaStore gave no content:// id on this device. The correct "
                            "Intent form cannot be built here -- only the file:// variant "
                            "can be tested, and it is the one expected to fail.")
        mark_step()

        # --- 4. Fire each variant and record where Instagram lands ------------
        results = []
        for label, command in self._intent_variants(remote_path, content_uri, share_targets):
            if aborted():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            # A drop between variants would make every remaining one look like a
            # failure, so recover first rather than recording noise.
            if not self._ensure_online(target, emit, log):
                results.append({"variant": label, "command": command, "activity": None,
                                "verdict": "unknown", "error": "adb tunnel offline"})
                continue
            self._reset_instagram(target)
            emit("info", "--- %s ---", label)
            emit("info", "    %s", command)

            proc = self._shell(target, command)
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            error = None
            for text in (stdout, stderr):
                if text and ("Exception" in text or "Error" in text or "denied" in text.lower()):
                    error = text.splitlines()[0][:160]
                    break
            # Always report what `am` said. Silence is itself a data point, and
            # hiding it makes a failed Intent look identical to a successful one.
            emit("info", "    am said: %s",
                 (stdout or stderr or "<no output>").replace("\n", " | ")[:220])
            if error:
                emit("warning", "    error: %s", error)

            # Watch where it settles rather than sampling once.
            emit("info", "    watching for %ss...", self.watch_seconds)
            activity, sequence = self._watch_landing(target, emit, log, self.watch_seconds)
            if activity is None:
                emit("info", "    nothing readable in the foreground")
            verdict = self._classify(activity)
            if len(sequence) > 1:
                emit("info", "    path: %s", " -> ".join(
                    self._classify(step).upper() for step in sequence))
            emit("info", "    SETTLED ON: %s  -> %s", activity or "<unknown>", verdict.upper())

            results.append({"variant": label, "command": command, "activity": activity,
                            "verdict": verdict, "error": error,
                            "sequence": sequence})
        mark_step()

        # --- 5. Verdict -------------------------------------------------------
        usable = [r for r in results if r["verdict"] == "composer"]
        self._reset_instagram(target)

        emit("info", "=== PROBE SUMMARY for %s ===", target)
        for entry in results:
            emit("info", "  %-42s %-16s %s", entry["variant"], entry["verdict"],
                 entry["error"] or entry["activity"] or "")

        # A run can fail to reach a composer for two very different reasons, and
        # calling them both "it does not work" would retire the idea on bad
        # evidence. Only a run that actually observed where Instagram went is a
        # real negative.
        chooser_hits = [r for r in results if r["verdict"] == "chooser"]
        bounced = [r for r in results if r["verdict"] == "bounced-to-feed"]
        unreadable = [r for r in results if r["verdict"] == "unknown"]
        caveats = []
        if content_uri is None:
            caveats.append("the content:// variants could not be built on this device, "
                           "so only the file:// form (the one expected to fail) was tested")
        if unreadable:
            caveats.append(f"{len(unreadable)} variant(s) never brought Instagram to the "
                           "foreground and the front app could not be read")

        settled_on_router = [r for r in results if r["verdict"] == "share-router"]

        if usable:
            emit("info", "RESULT: %s reached a composer. Worth building as an optional "
                         "fast path -- it would still only replace the first 3 of 7 stages, "
                         "and caption/Share/verification remain UI work.",
                 usable[0]["variant"])
        elif settled_on_router:
            # Sitting on the handler is NOT success. A 20s watch once scored this
            # as "media accepted", and the hold immediately after showed the same
            # variant dropping to the feed at ~25s -- the handler just gives up
            # more slowly than it does for other URI forms.
            emit("warning",
                 "RESULT: %s was still on the reel share handler after %ss -- it never "
                 "reached a composer. The handler is known to sit for a while and then "
                 "drop to the feed, so treat this as a slow failure unless the hold below "
                 "shows a real composer with the video loaded.",
                 settled_on_router[0]["variant"], self.watch_seconds)
        elif bounced and content_uri is None:
            # The single most informative outcome so far: routing is solved, the
            # media handoff is not.
            emit("warning",
                 "RESULT: the reel share handler ACCEPTED the Intent but bounced to the main "
                 "feed. Routing works -- the media is the problem. Only file:// could be tried "
                 "here, and Instagram cannot read a raw /sdcard path under scoped storage. "
                 "Getting a content:// URI is the remaining blocker; if the MediaStore lookup "
                 "above still fails on every strategy, this device does not expose one and the "
                 "shortcut cannot be completed on it.")
        elif bounced:
            emit("warning",
                 "RESULT: the reel share handler bounced to the main feed even with a "
                 "content:// URI (%s). It received a URI it still would not open -- check "
                 "whether the grant flag survived, or whether Instagram requires the file "
                 "under its own media collection.", content_uri)
        elif chooser_hits and not any(self._looks_like_reels(c) for c in share_targets):
            # The chooser proves the Intent was accepted and that Instagram offers
            # several destinations; we just could not name the Reels one.
            emit("warning", "RESULT: the Intent reached the system chooser, so it IS accepted "
                            "-- Instagram just exposes several destinations and none could be "
                            "identified as Reels by name. Read the component list above against "
                            "what the chooser shows on screen and pass the Reels one to -n.")
        elif caveats:
            emit("warning", "RESULT: INCONCLUSIVE -- no composer reached, but %s. "
                            "Do not treat this as proof the Intent shortcut fails.",
                 "; and ".join(caveats))
        else:
            landed = ", ".join(sorted({r["verdict"] for r in results}))
            emit("info", "RESULT: no variant reached a reel composer (landed on: %s). "
                         "On this device/build the Intent shortcut does not reach Reels; "
                         "the existing UI flow stays the only reliable path.", landed)
        mark_step()

        # Leave the most promising variant on screen so the result can be seen
        # with your own eyes. Everything above is inference from activity names;
        # whether the video is actually loaded in the composer is something only
        # looking at the phone can settle.
        best = (usable or [r for r in results if r["verdict"] == "share-router"])
        if best and self.hold_seconds > 0:
            emit("info", "=== HOLDING for %ss so you can look at the phone ===", self.hold_seconds)
            emit("info", "Re-firing %s. Check whether the Reel composer is open with the "
                         "video loaded, or whether it fell back to the feed.", best[0]["variant"])
            hold_final = None
            if self._ensure_online(target, emit, log):
                self._reset_instagram(target)
                self._shell(target, best[0]["command"])
                deadline = time.time() + self.hold_seconds
                while time.time() < deadline:
                    remaining = int(deadline - time.time())
                    current = _adb_get_foreground_activity(target, logger=log) \
                        or self._foreground_any(target)
                    emit("info", "    %2ds left | on screen: %s", remaining, current or "<unknown>")
                    hold_final = current or hold_final
                    time.sleep(5.0)
            emit("info", "=== hold finished ===")

            # The hold watches for longer than the per-variant window, so it sees
            # slow bounces the earlier verdict missed. It is the better evidence
            # and supersedes what was printed above.
            hold_verdict = self._classify(hold_final)
            if hold_final and hold_verdict != best[0]["verdict"]:
                emit("warning",
                     "CORRECTION: over the longer hold, %s ended on %s -> %s, not %s. "
                     "The shorter per-variant watch stopped before this happened; the hold "
                     "is the reliable reading.",
                     best[0]["variant"], hold_final, hold_verdict.upper(),
                     best[0]["verdict"].upper())
                if hold_verdict == "bounced-to-feed":
                    emit("warning",
                         "FINAL: the reel share handler accepts the Intent and then returns to "
                         "the feed without opening the composer. The Intent shortcut does not "
                         "work for Reels on this Instagram build -- the existing UI flow stays "
                         "the only way to post.")
                    settled_on_router = []
                    usable = []

        if self.post_after_probe and usable:
            emit("warning", "post_after_probe is set and %s landed on a composer, but this "
                            "probe deliberately stops before publishing. Re-run the normal "
                            "instagram_reel_upload_u2 flow to post, or wire the fast path in "
                            "once you have decided from these results.", usable[0]["variant"])

        return {
            "profile_id": profile.id,
            "target": target,
            "aborted": False,
            "success": bool(usable or settled_on_router),
            "posted": False,
            "content_uri_available": content_uri is not None,
            "handlers": handler_names,
            "results": results,
        }
