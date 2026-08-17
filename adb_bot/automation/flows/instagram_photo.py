"""Post a photo to the Instagram feed (uiautomator2).

This is the missing third leg of the "set an account up by itself" trio. The
bot could already change the bio (`update_bio_u2`) and the profile picture
(`update_profile_picture`), and it could post a *reel*
(`instagram_reel_upload_u2`) -- but a plain feed photo had no flow at all.
`lifecycle.UNSUPPORTED_PLAN_KEYS` said so out loud: the Warmup Plan table has
had a `Feed Posts` column since the schema was written, and every row that
asked for one was parsed, warned about, and dropped.

Rather than a second copy of the reel flow, this subclasses it. Everything that
is genuinely shared -- popup dismissal, the account switcher, opening the
composer, the caption editor, Share, the post ledger, and the whole
post-verification pass -- is inherited unchanged, so a fix to any of those
reaches both flows at once. Three things actually differ, and they are the only
things overridden here:

1. **Mode.** The composer's carousel has to land on POST, not REEL. The parent
   already selects-and-confirms a mode generically off two selector lists, so
   the child just supplies the other two lists. This matters: picking a
   thumbnail in the wrong mode is how you post a reel when you meant a photo.

2. **The cell to pick.** Reels want a `Video, ...` gallery cell; a feed post
   wants a `Photo, ...` one.

3. **The screens between the picker and the caption.** A reel goes
   gallery -> Next -> caption. A photo goes gallery -> (crop) -> Next ->
   (filter/edit) -> Next -> caption, and which of those screens Instagram
   shows varies by build. The parent's fixed "one Next, then maybe one more"
   sequence would type the caption into the filter screen. This flow advances
   in a loop until the caption field or Share is actually on screen, which
   covers every build without hard-coding a screen count.

Everything else -- including the rule that Share being tapped is recorded in
the ledger *before* anything that can crash, and that an unproven post is
reported `uncertain` rather than `failed` -- is the parent's behaviour, on
purpose. A photo post that cannot be confirmed is exactly as dangerous to
retry blindly as a reel that cannot be confirmed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from adb_bot.core.models import Profile
from adb_bot.automation import post_ledger

from . import instagram as instagram_module
from . import reel_verify
from . import waits
from .instagram_reel import InstagramReelUploadU2Flow

u2 = getattr(instagram_module, "u2", None)

_emit = instagram_module._emit
_u2_click = instagram_module._u2_click
_adb_push_media_to_device = instagram_module._adb_push_media_to_device
_adb_wait_for_media_store_index = instagram_module._adb_wait_for_media_store_index
_adb_resolve_story_media_path = instagram_module._adb_resolve_story_media_path
get_story_media_queue = instagram_module.get_story_media_queue

# What counts as a picture for this flow. The shared media queue also accepts
# video (it backs the reel and story flows too), so a folder handed to this
# flow is filtered down to stills -- otherwise "post a pic" quietly posts an
# mp4 as a feed video the first time somebody drops a clip in the wrong folder.
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class InstagramPhotoPostU2Flow(InstagramReelUploadU2Flow):
    """Post a single photo to the feed. See the module docstring for what this
    changes relative to the reel flow it inherits from."""

    name = "instagram_photo_post_u2"

    # Pushed to Pictures/ rather than the reel flow's Download/. Both end up in
    # the images MediaStore collection (the index check keys on _display_name,
    # not the directory), but a still in Pictures/ is where every gallery app
    # expects to find it, and the picker's "Recents" tab surfaces it first.
    REMOTE_MEDIA_DIR = "/sdcard/Pictures"

    # How many times to tap Next/Continue looking for the caption screen. Three
    # covers the longest chain seen (crop -> filter -> caption); the loop stops
    # the moment the caption field or Share appears, so this is only a ceiling.
    MAX_ADVANCE_STEPS = 3

    # Ceiling on how far into a mixed media folder this flow will walk looking
    # for a still before giving up. Stops a folder full of clips from draining
    # the whole queue on one run.
    MAX_QUEUE_SKIPS = 10

    # ---- the three real differences from the reel flow ----------------------

    def _reel_tab_selectors(self):
        """The composer mode this flow wants selected. Named for the parent's
        API, not for reels -- the parent calls this to mean "the mode I am
        trying to reach"."""
        return [
            {"text": "POST"}, {"text": "Post"},
            {"textMatches": "(?i)^post$"}, {"descriptionMatches": "(?i)^post$"},
        ]

    def _other_mode_selectors(self):
        """The modes that must NOT be selected when we pick a thumbnail."""
        return [
            {"text": "REEL"}, {"text": "Reel"}, {"text": "Reels"},
            {"text": "STORY"}, {"text": "Story"},
            {"text": "LIVE"}, {"text": "Live"},
        ]

    def _select_media_u2(self, d, target, emit, logger=None) -> bool:
        """Confirm POST mode, then pick the first *photo* cell.

        The mode check is the parent's -- reveal by swiping the carousel, tap,
        then confirm the tab reports selected. It refuses to pick anything if a
        different mode is still active, which is the guard that stops this flow
        posting a reel.
        """
        if not self._select_mode_u2(d, target, emit, logger=logger):
            _emit(logger, "warning",
                  "u2: could not confirm POST mode for %s; not selecting media to avoid "
                  "posting the wrong type", target)
            return False

        waits.settle(1.5, ready=waits.u2_ready(
            d,
            {"descriptionStartsWith": "Photo"},
            {"descriptionContains": "Photo"},
        ), logger=logger, what="photo gallery thumbnails")

        # Prefer an explicit "Photo, ..." cell. Deliberately no Video fallback:
        # on a phone whose gallery holds both, falling back to a video cell is
        # how a feed post silently becomes a video post. If no photo cell is
        # readable we take the first grid position, which is the file this run
        # just pushed.
        return _u2_click(
            d,
            [
                {"descriptionStartsWith": "Photo"},
                {"descriptionContains": "Photo"},
            ],
            logger=logger,
            purpose="first gallery photo thumbnail",
            fallback_ratio=(0.17, 0.30),
        )

    def _advance_to_caption_u2(self, d, target, emit, logger=None) -> bool:
        """Tap Next/Continue until the caption screen (or Share) is up.

        A photo post passes through a crop screen and a filter/edit screen on
        the way to the caption, and which of them appear depends on the build
        and on the image's aspect ratio. Counting screens would break on the
        next Instagram update; asking "am I there yet" after each tap does not.

        Returns True if the caption/share screen was reached.
        """
        for step in range(1, self.MAX_ADVANCE_STEPS + 1):
            if waits.any_exists(d, *self._CAPTION_SELECTORS, *self._SHARE_SELECTORS):
                _emit(logger, "info",
                      "u2: caption/share screen reached for %s after %s advance step(s)",
                      target, step - 1)
                return True
            if not self._tap_advance_u2(d, target, emit, logger=logger,
                                        purpose=f"Next (photo composer step {step})"):
                _emit(logger, "warning",
                      "u2: no Next/Continue control on photo composer step %s for %s",
                      step, target)
                break
            waits.settle(
                3,
                ready=waits.u2_ready(d, *self._CAPTION_SELECTORS, *self._SHARE_SELECTORS,
                                     *self._NEXT_SELECTORS),
                logger=logger, what="next photo composer screen",
            )

        reached = waits.any_exists(d, *self._CAPTION_SELECTORS, *self._SHARE_SELECTORS)
        if not reached:
            _emit(logger, "warning",
                  "u2: never reached the caption/share screen for %s after %s advance step(s)",
                  target, self.MAX_ADVANCE_STEPS)
        return reached

    # ---- helpers ------------------------------------------------------------

    def _select_mode_u2(self, d, target, emit, logger=None) -> bool:
        """Readable alias for the parent's mode selector, which is named for
        reels but is generic over `_reel_tab_selectors` / `_other_mode_selectors`."""
        return self._select_reel_mode_u2(d, target, emit, logger=logger)

    def _build_remote_media_path(self, local_media_path: str) -> str:
        return f"{self.REMOTE_MEDIA_DIR}/{Path(local_media_path).name.replace(' ', '_')}"

    def _resolve_photo(self, profile, emit, logger=None):
        """The picture this run posts, plus the queue it came from (if any).

        Resolution order, most specific first:
          1. `profile.media_path` -- the per-run choice, a file or a folder.
          2. `profile.picture`    -- the field the profile-picture flow uses, so
             a caller that already knows which still to use does not need to
             learn a second field name.
          3. the global story/reel media setting, as a last resort.

        Returns (path, selected, queue) or (None, None, None).
        """
        raw = (getattr(profile, "media_path", None)
               or getattr(profile, "picture", None)
               or _adb_resolve_story_media_path(logger=logger))
        if not raw:
            emit("warning", "No photo configured for profile %s (set media_path or picture)",
                 profile.id)
            return None, None, None

        source = Path(str(raw)).expanduser()
        if source.is_dir():
            # The queue is what makes two profiles running at once safe: it hands
            # each caller a different file. So it stays in charge of the pick,
            # and this loop only skips past anything that is not a still.
            #
            # A skipped clip is deliberately NOT marked used -- that would MOVE
            # it into `used/` and take it away from the reel flow, which is the
            # flow it belongs to. It does stay "assigned" for the life of this
            # process, so point this flow at a photos-only folder; a folder
            # shared with the reel queue is a misconfiguration, and the warning
            # below is there to name it.
            queue = get_story_media_queue(source, logger=logger)
            for _ in range(self.MAX_QUEUE_SKIPS):
                candidate = queue.get_next_media()
                if candidate is None:
                    break
                if Path(candidate).suffix.lower() in PHOTO_SUFFIXES:
                    emit("info", "Assigned photo %s to profile %s from folder %s",
                         candidate, profile.id, source)
                    return str(candidate), candidate, queue
                emit("warning",
                     "Skipping %s in %s: not a still, and this flow posts photos only. "
                     "That folder looks like it is shared with the reel queue -- give the "
                     "photo flow its own folder.", Path(candidate).name, source)
            emit("warning", "No pending photo in %s for profile %s", source, profile.id)
            return None, None, None

        if not source.exists() or not source.is_file():
            emit("warning", "Configured photo does not exist for %s: %s", profile.id, source)
            return None, None, None
        if source.suffix.lower() not in PHOTO_SUFFIXES:
            emit("warning", "%s is not a photo (%s); this flow posts stills only",
                 source.name, source.suffix or "no extension")
            return None, None, None
        return str(source), source, None

    # ---- the run ------------------------------------------------------------

    def get_progress_total_steps(self, target: str) -> int:
        return 7

    def run(
        self,
        profile: Profile,
        adb_client=None,
        logger=None,
        should_stop=None,
        status_callback=None,
        manual_continue_event=None,
        manual_continue_callback=None,
    ):
        if not adb_client:
            raise ValueError("adb_client is required")
        if not profile.target:
            raise ValueError("Profile target is missing")

        log = logger if logger is not None else None
        target = profile.target

        def emit(level: str, message: str, *args) -> None:
            target_logger = log if log is not None else print
            method = getattr(target_logger, level, None)
            if callable(method):
                method(message, *args)
            else:
                if args:
                    message = message % args
                print(message)

        def check_abort() -> bool:
            if callable(should_stop) and should_stop():
                emit("info", "Abort requested during Instagram photo post flow for profile %s",
                     profile.id)
                return True
            return False

        def mark_step() -> None:
            if hasattr(adb_client, "mark_progress_step"):
                adb_client.mark_progress_step()

        if u2 is None:
            emit("warning",
                 "uiautomator2 is not importable in the interpreter running this app "
                 "(python=%s, frozen_exe=%s). Install it into THAT environment or rebuild "
                 "the exe with it bundled. Skipping %s",
                 sys.executable, bool(getattr(sys, "frozen", False)), profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        emit("info", "Starting Instagram photo post flow for profile %s", profile.id)

        # --- Resolve the photo ------------------------------------------------
        media_path, selected_media, media_queue = self._resolve_photo(profile, emit, logger=log)
        if media_path is None:
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        # --- Double-post guard (identical to the reel flow's) -----------------
        # The ledger is written the instant Share is tapped, so it answers even
        # when a previous run died mid-verification. Without it, every "we could
        # not tell" eventually becomes a second post on the account.
        ledger = post_ledger.PostLedger()
        media_hash = post_ledger.media_fingerprint(media_path)
        prior = ledger.lookup(str(profile.id), media_hash)
        if prior is not None and prior.blocks_repost():
            emit("warning",
                 "Refusing to post %s to profile %s again -- Share was already tapped for this "
                 "photo %.0f min ago (%s). Not a failure: the earlier post is live or still "
                 "being verified.",
                 Path(media_path).name, profile.id, prior.age_seconds / 60.0, prior.status)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "uncertain": False, "already_shared": True,
                    "verify_method": "ledger", "verify_detail": f"already shared ({prior.status})"}

        # --- Push the photo ---------------------------------------------------
        remote_media_path = self._build_remote_media_path(media_path)
        emit("info", "Pushing photo for profile %s: %s -> %s",
             profile.id, media_path, remote_media_path)
        mark_step()
        if not _adb_push_media_to_device(target, media_path, remote_media_path, logger=log):
            emit("warning", "adb push failed for profile %s on target %s", profile.id, target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
        emit("info", "adb push succeeded for profile %s on target %s", profile.id, target)
        # Make the picker see it. Best-effort: some OEM builds restrict
        # `content query`, and the flow proceeds either way.
        _adb_wait_for_media_store_index(target, remote_media_path, logger=log)

        # The photo is NOT marked used here. Pushing a file to a phone is not
        # posting it -- if this run fails later the still must stay in the queue
        # for the next attempt. Committed only once the post is confirmed.
        def commit_media_used() -> None:
            if media_queue is not None and selected_media is not None:
                moved = media_queue.mark_used(selected_media)
                emit("info", "Marked photo %s as used for profile %s: %s",
                     selected_media, profile.id, moved)

        def keep_media_for_retry(reason: str) -> None:
            if media_queue is not None and selected_media is not None:
                emit("info", "Leaving photo %s in the queue for a retry (%s)",
                     selected_media, reason)

        mark_step()
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        waits.set_speed(None)
        emit("info", "Flow speed factor for %s: %.2fx", target, waits.speed_factor())

        # --- Launch Instagram + connect --------------------------------------
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            is_start = "monkey" in command or "am start" in command
            waits.settle(
                5 if is_start else 2,
                ready=(lambda: self._ig_is_foreground(target, adb_client)) if is_start else None,
                logger=log, what="Instagram in foreground",
            )
        mark_step()

        emit("info", "Connecting uiautomator2 to %s", target)
        try:
            d = u2.connect(target)
            d.implicitly_wait(self.SELECTOR_WAIT_SECONDS)
        except Exception as exc:
            emit("warning", "uiautomator2 could not connect to %s: %s", target, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        waits.settle(
            10,
            ready=waits.u2_ready(
                d,
                {"resourceId": "com.instagram.android:id/feed_tab"},
                {"resourceIdMatches": r"com\.instagram\.android:id/.*(tab_bar|profile_tab).*"},
            ),
            logger=log, what="Instagram UI loaded",
        )
        self._dismiss_popups_u2(
            d, logger=log,
            skip_if=lambda: waits.any_exists(
                d,
                {"resourceId": "com.instagram.android:id/feed_tab"},
                {"resourceId": "com.instagram.android:id/profile_tab"},
            ),
        )
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # --- Step 0: be on the account this post is for -----------------------
        # Same rule as the reel flow: before the baseline count (a count read on
        # the wrong account proves nothing) and before the composer (which posts
        # as whoever is in front). No handle = a single-account phone = no-op.
        want_handle = getattr(profile, "target_handle", None)
        account_ok, account_why = (True, self.ACCOUNT_OK)
        if want_handle:
            account_ok, account_why = self._ensure_account_state_u2(
                d, target, want_handle, emit, logger=log)
        posted_as = None
        if want_handle and not account_ok:
            flagged = self._account_flag_result_u2(d, profile, target, emit, "switching accounts")
            if flagged:
                return flagged
            if account_why == self.ACCOUNT_ABSENT:
                posted_as = self._phones_own_account_u2(d, target, emit, log)
            if posted_as:
                emit("warning", "The phone does not have @%s, so this photo goes out on @%s -- "
                                "the account this phone is actually signed in as.",
                     want_handle, posted_as)
            else:
                emit("warning", "Not posting on %s: could not prove it is signed in as @%s. "
                                "The photo stays queued -- posting it on the wrong account is "
                                "the one outcome that cannot be undone.", target, want_handle)
                result = {"profile_id": profile.id, "target": target, "aborted": False,
                          "success": False, "failed": True}
                if account_why == self.ACCOUNT_ABSENT:
                    result["wrong_account"] = True
                    result["wanted_handle"] = str(want_handle)
                return result

        # --- Baseline post count ---------------------------------------------
        baseline_count = None
        if self._open_profile_tab_u2(d, target, logger=log):
            waits.settle(2, ready=waits.u2_ready(d, *self._POST_COUNT_SELECTORS),
                         logger=log, what="profile header")
            baseline_count = self._read_post_count_u2(d, target, logger=log)
        emit("info", "Baseline post count for %s: %s", target,
             baseline_count.value if baseline_count else "unavailable")

        instagram_module._ensure_instagram_home_feed_u2(d, target, logger=log)

        # --- Step 1: open the composer ---------------------------------------
        emit("info", "Opening the post composer for %s", target)
        if not self._open_reel_composer_u2(d, target, emit, log):
            flagged = self._account_flag_result_u2(d, profile, target, emit, "opening the composer")
            if flagged:
                return flagged
            emit("warning", "Unable to open the Instagram composer for %s (leaving Instagram open)",
                 target)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "failed": True}
        mark_step()
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # --- Step 2: POST mode + the photo -----------------------------------
        emit("info", "Selecting POST mode and the pushed photo for %s", target)
        if not self._select_media_u2(d, target, emit, log):
            flagged = self._account_flag_result_u2(d, profile, target, emit, "selecting media")
            if flagged:
                return flagged
            emit("warning", "Unable to select a photo for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
        mark_step()
        waits.settle(3, ready=waits.u2_ready(d, *self._NEXT_SELECTORS),
                     logger=log, what="photo editor")
        # Instagram's "Edits" app promo can land on top of the composer here and
        # cover Next. The parent knows how to spot and clear it.
        self._dismiss_edit_app_popup_u2(d, target, emit, log)

        # --- Step 3: advance to the caption screen ----------------------------
        if not self._advance_to_caption_u2(d, target, emit, log):
            emit("warning", "Photo composer never reached the caption/share screen for %s; "
                            "nothing was posted", target)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "failed": True}

        if profile.caption:
            if self._enter_caption_u2(d, target, adb_client, profile.caption, emit, log):
                emit("info", "Entered photo caption for %s", target)
                waits.settle(1)
                try:
                    d.press("back")  # hide the keyboard so it can't cover Share
                except Exception:
                    adb_client.run_command(f"adb -s {target} shell input keyevent 111")
                waits.settle(1, ready=waits.u2_ready(d, *self._SHARE_SELECTORS, *self._NEXT_SELECTORS),
                             logger=log, what="keyboard dismissed")
            # A caption that would not go in is not a reason to abandon the
            # post: an uncaptioned photo is still the photo. Logged by the
            # caption helper either way.
        mark_step()

        # --- Step 4: Share ----------------------------------------------------
        photo_posted = False
        if self._tap_share_u2(d, target, emit, log):
            emit("info", "Tapped Share for %s", target)
            # Ledger FIRST -- before the settle, before verification, before
            # anything that can crash. From this instant a post may exist on the
            # account, and that fact has to outlive this process.
            ledger.record_share(str(profile.id), media_path,
                                caption=getattr(profile, "caption", "") or "",
                                queue_id=getattr(profile, "queue_id", "") or "",
                                media_hash=media_hash,
                                baseline_count=baseline_count,
                                target_handle=want_handle or "")
            waits.settle(8, ready=waits.u2_ready(d, {"resourceId": "com.instagram.android:id/feed_tab"}),
                         logger=log, what="composer closed")
            photo_posted = True
        else:
            emit("warning", "Share button was not detected after composing the photo for %s", target)

        if not photo_posted:
            flagged = self._account_flag_result_u2(d, profile, target, emit, "the Share step")
            if flagged:
                return flagged
            emit("warning", "Instagram photo post did not complete for %s (the Share button was "
                            "never tapped -- nothing was posted)", target)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "uncertain": False}

        # --- Step 5: confirm the post ----------------------------------------
        # Read the screen before touching it: Share has just landed and the
        # composer has closed, which is both the moment Instagram shows its
        # confirmation and the last moment we can be sure of seeing it -- popup
        # dismissal can tap it away and the home tap re-renders over it.
        verdict = None
        try:
            early_text = self._screen_text_probe_u2(d, target, logger=log)()
            if reel_verify.classify_post_screen(early_text) == reel_verify.STATE_CONFIRMED:
                verdict = reel_verify.VerifyResult(
                    True, reel_verify.VIA_BANNER,
                    "confirmation seen on the feed immediately after Share",
                )
        except Exception as exc:
            _emit(log, "info", "u2: early confirmation read failed for %s (%s); "
                               "falling through to full verification", target, exc)

        if verdict is None:
            self._dismiss_popups_u2(d, logger=log, max_rounds=2)
            self._tap_ig_home_icon_u2(d, target, emit, log)
            # The post-count delta is what proves a photo landed. The banner
            # check inside this pass is perishable, so it runs first each round.
            verdict = reel_verify.verify_reel_posted(
                baseline_count=baseline_count,
                get_post_count=self._profile_post_count_probe_u2(d, target, emit, log),
                get_screen_text=self._screen_text_probe_u2(d, target, logger=log),
                get_notification_state=self._notification_probe(target, adb_client, logger=log),
                min_wait=reel_verify.FAST_MIN_WAIT_SECONDS,
                timeout=reel_verify.FAST_TIMEOUT_SECONDS,
                should_stop=should_stop,
                emit=emit,
            )
        post_confirmed = verdict.confirmed
        mark_step()

        # Only a conclusive negative clears the photo for another send; a
        # timeout leaves it blocked, which is the safe direction to be wrong in.
        if post_confirmed:
            ledger.resolve(str(profile.id), media_hash,
                           post_ledger.STATUS_CONFIRMED, verdict.method)
        elif not verdict.uncertain:
            ledger.resolve(str(profile.id), media_hash,
                           post_ledger.STATUS_DISPROVED, verdict.method)

        if callable(should_stop) and should_stop():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        uncertain = (not post_confirmed) and verdict.uncertain

        if post_confirmed:
            commit_media_used()
            emit("info", "Instagram photo post for %s: %s", target, verdict.summary())
        elif uncertain:
            keep_media_for_retry("outcome uncertain -- queued for recheck")
            emit("warning", "Instagram photo post for %s: %s -- Share was tapped, so the photo "
                            "may well be live. Handing off to the deferred recheck; the ledger "
                            "blocks a re-post until it resolves. Leaving Instagram open.",
                 target, verdict.summary())
        else:
            keep_media_for_retry("post not confirmed")
            emit("warning", "Instagram photo post for %s: %s -- leaving Instagram open in case "
                            "the upload is still finishing", target, verdict.summary())

        return {
            "profile_id": profile.id,
            "target": target,
            "aborted": False,
            "success": post_confirmed,
            "uncertain": uncertain,
            "post_confirmed": post_confirmed,
            "verify_method": verdict.method,
            "verify_strength": verdict.strength,
            "verify_detail": verdict.detail,
            "media_hash": media_hash,
            "media_path": media_path,
            "posted_as": posted_as,
        }
