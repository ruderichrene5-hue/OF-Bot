import time
import threading
from concurrent.futures import ThreadPoolExecutor

from adb_bot.clients.adb import ADBClient
from adb_bot.clients.api import MultiloginApiClient
from adb_bot.automation import AutomationRunner
from adb_bot.config.config import get_bearer_token, get_profile_ids
from adb_bot.automation.flows.instagram import InstagramLikeFeedFlow, InstagramNotificationsFlow, InstagramScrollFlow, InstagramStoryUploadFlow, InstagramReelUploadFlow, InstagramUpdateBioFlow, InstagramUpdateBioU2Flow, InstagramUpdateProfilePictureU2Flow, InstagramWarmUpDay1Flow
from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
from adb_bot.core.logger import get_logger
from adb_bot.core.models import Profile
from adb_bot.clients.multilogin import (
    MultiloginAdbEnableClient,
    MultiloginLauncherClient,
    MultiloginShutdownClient,
)

DEFAULT_PROFILE_IDS = ["626005033091072287", "624310694145163612", "625149430776987991", "622381034276651060", "623102213643829576", "623102253758153032"]


FLOW_ACTION_COUNTS = {
    "instagram_scroll": 12,
    "instagram_like_feed": 14,
    "instagram_notifications": 5,
    "instagram_story_upload": 10,
    "update_bio": 10,
    "update_bio_u2": 10,
    "update_profile_picture": 10,
    "warm_up_process": 18,
}


class _ProgressTrackingADBClient:
    def __init__(self, client, on_action=None):
        self._client = client
        self._on_action = on_action

    def mark_progress_step(self) -> None:
        if callable(self._on_action):
            self._on_action()

    def run_command(self, command: str):
        return self._client.run_command(command)

    def __getattr__(self, name):
        return getattr(self._client, name)


def get_flow_progress(flow_name: str, completed_actions: int, total_actions: int | None = None) -> float:
    if total_actions is None:
        total_actions = FLOW_ACTION_COUNTS.get(flow_name, 10)
    if total_actions <= 0:
        return 0.0
    percent = (completed_actions / total_actions) * 100.0
    return round(min(100.0, max(0.0, percent)), 2)


def should_shutdown_profile_after_flow(flow_name: str) -> bool:
    """Backward-compatible placeholder for flow-level shutdown decisions.

    The UI now controls whether profiles close on successful completion.
    """

    return False


def get_progress_counts(completed_actions: int, total_actions: int | None = None) -> tuple[int, int]:
    completed_value = max(0, int(completed_actions))
    if total_actions is None:
        return completed_value, completed_value

    total_value = int(total_actions)
    if total_value <= 0:
        return completed_value, 0
    return max(0, min(total_value, completed_value)), total_value


def parse_profiles_from_response(api_client, api_response: dict):
    if hasattr(api_client, "parse_profiles") and callable(api_client.parse_profiles):
        try:
            parsed_profiles = api_client.parse_profiles(api_response)
        except TypeError:
            parsed_profiles = None
        if isinstance(parsed_profiles, list):
            return parsed_profiles
        if isinstance(parsed_profiles, tuple):
            return list(parsed_profiles)
        if parsed_profiles is not None and not isinstance(parsed_profiles, (str, bytes, dict)):
            try:
                return list(parsed_profiles)
            except TypeError:
                pass

    items = api_response.get("data", {}).get("items", []) or []
    return list(items)


def profile_matches_id(profile, profile_id: str) -> bool:
    profile_id_value = getattr(profile, "id", None)
    if profile_id_value is None and isinstance(profile, dict):
        profile_id_value = profile.get("id")
    return profile_id_value == profile_id


def profile_is_ready(profile) -> bool:
    explicit_ready = getattr(profile, "is_ready", None)
    if explicit_ready is not None:
        return bool(explicit_ready)

    if isinstance(profile, dict):
        status = profile.get("status")
        return str(status).lower() in {"active", "ready"}

    status = getattr(profile, "status", None)
    return str(status).lower() in {"active", "ready"}


def get_profile_id(profile) -> str | None:
    if isinstance(profile, dict):
        return profile.get("id")
    return getattr(profile, "id", None)


def coerce_profile(profile, profile_id: str, caption: str | None = None, bio: str | None = None, picture: str | None = None, media_path: str | None = None) -> Profile:
    if isinstance(profile, Profile):
        if caption is not None:
            profile.caption = caption
        if bio is not None:
            profile.bio = bio
        if picture is not None:
            profile.picture = picture
        if media_path is not None:
            profile.media_path = media_path
        return profile

    if isinstance(profile, dict):
        return Profile(
            id=profile.get("id", profile_id),
            status=profile.get("status", ""),
            ip=profile.get("ip"),
            port=profile.get("port"),
            pwd=profile.get("pwd"),
            caption=caption,
            bio=bio,
            picture=picture,
            media_path=media_path,
        )

    return Profile(
        id=get_profile_id(profile) or profile_id,
        status=getattr(profile, "status", ""),
        ip=getattr(profile, "ip", None),
        port=getattr(profile, "port", None),
        pwd=getattr(profile, "pwd", None),
        caption=caption,
        bio=bio,
        picture=picture,
        media_path=media_path,
    )


def normalize_profile_ids(profile_ids: list[str] | None = None, fallback_ids: list[str] | None = None) -> list[str]:
    selected_ids = [profile_id for profile_id in (profile_ids or []) if profile_id]
    fallback_ids = [profile_id for profile_id in (fallback_ids or []) if profile_id]
    combined_ids = [*fallback_ids, *selected_ids]
    return list(dict.fromkeys(combined_ids))


# Multilogin's code for "profile is not running; ADB toggle skipped". It is the
# normal answer while a profile is still booting -- every profile in a 56-profile
# run hit it at least once -- so it is not on its own a failure.
MLX_PROFILE_NOT_RUNNING_CODE = 42002

# How many *consecutive* not-running answers mean the launch genuinely did not
# take, rather than the profile still coming up. Measured from a real 56-profile
# run: healthy profiles needed at most 11 attempts, while every profile that
# reached the 15-attempt cap had failed to start at all and never recovered.
# 12 therefore separates the two populations without disturbing a slow boot.
DEFAULT_RELAUNCH_AFTER_ATTEMPTS = 12


def _enable_reported_not_running(response, profile_id: str) -> bool:
    """Whether an enable_adb response said this profile isn't running.

    Worth reading rather than ignoring: enabling ADB on a stopped profile can
    never succeed, so repeating it is guaranteed waste. The response shape
    varies, so this stays tolerant and treats anything unparseable as "no
    opinion" rather than as a failure.
    """
    try:
        details = ((response or {}).get("data") or {}).get("fail_details") or []
    except AttributeError:
        return False
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("id") or "") not in ("", str(profile_id)):
            continue
        if detail.get("code") == MLX_PROFILE_NOT_RUNNING_CODE:
            return True
        if "not running" in str(detail.get("msg") or "").lower():
            return True
    return False


def prepare_profile_for_adb(
    profile_id: str,
    api_client: MultiloginApiClient,
    adb_enable_client: MultiloginAdbEnableClient,
    logger,
    max_attempts: int = 2,
    wait_seconds: int = 10,
    caption: str | None = None,
    bio: str | None = None,
    picture: str | None = None,
    media_path: str | None = None,
    launcher_client=None,
    relaunch_after_attempts: int = DEFAULT_RELAUNCH_AFTER_ATTEMPTS,
):
    """Wait for a launched profile to become ADB-ready.

    The loop used to do one thing on every attempt -- enable ADB, check
    credentials -- regardless of what Multilogin said. When the answer was
    "profile is not running", that repeated a call which *cannot* succeed:
    nothing here restarts the profile, so the outcome was fixed from the first
    attempt and the remaining ~4 minutes of retries were spent confirming it,
    holding a concurrency slot the whole time.

    Now a persistent not-running answer triggers the thing that can actually
    fix it -- a relaunch -- and if that still doesn't take, we stop early
    instead of running out the budget.
    """
    profile = None
    consecutive_not_running = 0
    relaunched = False
    attempt = 0
    budget = max_attempts

    while attempt < budget:
        attempt += 1
        logger.info("Waiting for profile %s readiness, attempt %s/%s", profile_id, attempt, budget)
        logger.info("Sleeping %s seconds before checking again", wait_seconds)
        time.sleep(wait_seconds)

        logger.info("Enabling ADB for profile %s before checking credentials", profile_id)
        adb_enable_response = adb_enable_client.enable_adb([profile_id], enabled=True)
        logger.info("ADB enable response for %s: %s", profile_id, adb_enable_response)
        if _enable_reported_not_running(adb_enable_response, profile_id):
            consecutive_not_running += 1
        else:
            consecutive_not_running = 0

        logger.info("Checking ADB credentials for %s", profile_id)
        api_response = api_client.fetch_adb_credentials([profile_id])
        profiles = parse_profiles_from_response(api_client, api_response)
        profile = next((item for item in profiles if profile_matches_id(item, profile_id) and profile_is_ready(item)), None)
        if profile:
            profile = coerce_profile(profile, profile_id, caption=caption, bio=bio, picture=picture, media_path=media_path)
            logger.info("Profile %s is ready after ADB enable attempt %s", profile_id, attempt)
            return profile

        if consecutive_not_running >= relaunch_after_attempts:
            if launcher_client is not None and not relaunched:
                logger.warning(
                    "Profile %s has reported 'not running' %s times in a row -- the launch did not "
                    "take. Relaunching instead of enabling ADB again (which cannot work on a "
                    "stopped profile).", profile_id, consecutive_not_running)
                try:
                    response = launcher_client.start_profiles([profile_id])
                    logger.info("Relaunch response for %s: %s", profile_id, response)
                except Exception as exc:
                    logger.warning("Relaunch failed for profile %s: %s", profile_id, exc)
                relaunched = True
                consecutive_not_running = 0
                # A relaunched profile needs the same boot time as a fresh one,
                # so give it a full budget rather than whatever was left over.
                budget = attempt + max_attempts
                continue

            logger.warning(
                "Profile %s is still not running after %s attempts%s -- stopping early rather than "
                "spending the rest of the budget on a call that cannot succeed.",
                profile_id, attempt, " and a relaunch" if relaunched else "")
            break

        if attempt < budget:
            logger.info("Profile %s still not ready after attempt %s; retrying enable and check", profile_id, attempt)

    logger.warning("Profile %s is not ready for ADB automation", profile_id)
    return None


def connect_with_retries(
    adb_client: ADBClient,
    profile: Profile,
    logger,
    profile_id: str,
    max_attempts: int = 3,
    retry_delay_seconds: int = 5,
) -> str | None:
    for attempt in range(1, max_attempts + 1):
        logger.info("ADB connection attempt %s/%s for profile %s", attempt, max_attempts, profile_id)
        target = adb_client.connect_and_auth(profile, logger=logger)
        if target:
            logger.info("ADB connection established for profile %s on attempt %s/%s", profile_id, attempt, max_attempts)
            return target

        if attempt < max_attempts:
            # Progressive backoff: the profile's adb tunnel sometimes needs a
            # few more seconds after Multilogin reports the profile "ready".
            delay = retry_delay_seconds * attempt
            logger.warning(
                "ADB connection attempt %s/%s failed for profile %s; retrying in %s seconds",
                attempt,
                max_attempts,
                profile_id,
                delay,
            )
            time.sleep(delay)

    logger.warning("ADB connection failed for profile %s after %s attempts", profile_id, max_attempts)
    return None


def run_profile_workflow(
    profile_id: str,
    bearer_token: str,
    api_client: MultiloginApiClient,
    adb_enable_client: MultiloginAdbEnableClient,
    shutdown_client: MultiloginShutdownClient,
    automation: AutomationRunner,
    logger,
    readiness_wait_seconds: int = 10,
    readiness_max_attempts: int = 2,
    flow_name: str = "instagram_scroll",
    should_stop=None,
    shutdown_on_abort: bool = False,
    shutdown_on_success: bool = False,
    status_callback=None,
    manual_continue_event=None,
    manual_continue_callback=None,
    connect_max_attempts: int = 5,
    connect_retry_delay_seconds: int = 5,
    progress_callback=None,
    caption: str | None = None,
    bio: str | None = None,
    picture: str | None = None,
    media_path: str | None = None,
    result_callback=None,
    launcher_client=None,
) -> None:
    adb_client = ADBClient()

    def emit_status(pid, status, result=None) -> None:
        """Report a status, carrying *how* the flow reached it when it knows.

        The reel flow works out which signal proved (or failed to prove) the
        post -- post count, banner, notification, upload lifecycle -- and that
        was being discarded here, because the callback only ever took
        (profile_id, status). Without it there is no way to see which signal is
        firing, and so no way to tell a detection problem from a posting one.
        """
        if not callable(status_callback):
            return
        detail = ""
        if isinstance(result, dict):
            method = result.get("verify_method") or ""
            strength = result.get("verify_strength") or ""
            extra = result.get("verify_detail") or ""
            if method:
                detail = f"via {method}" + (f" [{strength}]" if strength else "")
                if extra:
                    detail += f": {extra}"
        try:
            status_callback(pid, status, detail)
        except TypeError:
            # Callbacks that only accept (profile_id, status) still work.
            status_callback(pid, status)

    emit_status(profile_id, "starting")

    profile = prepare_profile_for_adb(
        profile_id,
        api_client,
        adb_enable_client,
        logger,
        max_attempts=readiness_max_attempts,
        wait_seconds=readiness_wait_seconds,
        caption=caption,
        bio=bio,
        picture=picture,
        media_path=media_path,
        launcher_client=launcher_client,
    )
    if not profile:
        if callable(status_callback):
            status_callback(profile_id, "failed")
        return

    profile_id_value = get_profile_id(profile)
    if callable(status_callback):
        status_callback(profile_id_value, "connecting")
    logger.info("Connecting to profile %s", profile_id_value)
    target = connect_with_retries(
        adb_client,
        profile,
        logger,
        profile_id_value,
        max_attempts=connect_max_attempts,
        retry_delay_seconds=connect_retry_delay_seconds,
    )
    if not target:
        # Left open on purpose: only a successful run closes its profile, so a
        # failure can be looked at (here, why ADB wouldn't connect).
        logger.warning(
            "ADB connection failed for profile %s after %s attempts; leaving the profile open for inspection",
            profile_id_value, connect_max_attempts,
        )
        if callable(status_callback):
            status_callback(profile_id_value, "adb_connect_failed")
        return

    if callable(status_callback):
        status_callback(profile_id_value, "running")
    logger.info("Starting Instagram-style automation for profile %s using flow '%s'", profile_id_value, flow_name)

    done_event = threading.Event()
    completed_actions = 0
    action_counter_lock = threading.Lock()
    flow_result = None

    def _progress_updater():
        total_actions = None
        try:
            flow = automation.flows.get(flow_name)
            if flow is not None and hasattr(flow, "get_progress_total_steps"):
                total_actions = flow.get_progress_total_steps(target)
        except Exception:
            total_actions = None

        while not done_event.is_set():
            with action_counter_lock:
                completed_value, total_value = get_progress_counts(completed_actions, total_actions)
                if total_value <= 0:
                    total_value = FLOW_ACTION_COUNTS.get(flow_name, 10)
            if callable(progress_callback):
                try:
                    progress_callback(profile_id_value, completed_value, total_value)
                except Exception:
                    pass
            time.sleep(0.5)

    def _advance_progress(step: int = 1) -> None:
        nonlocal completed_actions
        with action_counter_lock:
            total_actions_for_flow = None
            try:
                flow = automation.flows.get(flow_name)
                if flow is not None and hasattr(flow, "get_progress_total_steps"):
                    total_actions_for_flow = flow.get_progress_total_steps(target)
            except Exception:
                total_actions_for_flow = None
            completed_actions, _ = get_progress_counts(completed_actions + step, total_actions_for_flow)

    def _wrap_flow_run() -> None:
        nonlocal completed_actions
        flow = automation.flows.get(flow_name)
        if not flow:
            raise ValueError(f"Unknown automation flow: {flow_name}")

        tracked_adb_client = _ProgressTrackingADBClient(
            adb_client,
            on_action=lambda: _advance_progress(1),
        )
        flow_result = flow.run(
            profile,
            adb_client=tracked_adb_client,
            logger=logger,
            should_stop=should_stop,
            status_callback=status_callback,
            manual_continue_event=manual_continue_event,
            manual_continue_callback=manual_continue_callback,
        )
        return flow_result

    updater_thread = None
    try:
        if callable(progress_callback):
            updater_thread = threading.Thread(target=_progress_updater, daemon=True)
            updater_thread.start()

        try:
            flow_result = _wrap_flow_run()
            # Hand the raw flow result to anyone who asked for it. The status
            # callback only ever carries a rendered detail *string*, which is
            # right for logs and useless to a caller that needs a value back --
            # the deferred recheck needs the actual post count, not a sentence
            # about it.
            if callable(result_callback) and isinstance(flow_result, dict):
                try:
                    result_callback(flow_result)
                except Exception:
                    pass
        except Exception as exc:
            logger.exception("Workflow failed for profile %s: %s", profile_id_value, exc)
            if callable(status_callback):
                status_callback(profile_id_value, "failed")
            return
    finally:
        done_event.set()
        if callable(progress_callback):
            try:
                total_actions_for_flow = None
                try:
                    flow = automation.flows.get(flow_name)
                    if flow is not None and hasattr(flow, "get_progress_total_steps"):
                        total_actions_for_flow = flow.get_progress_total_steps(target)
                except Exception:
                    total_actions_for_flow = None
                completed_value, total_value = get_progress_counts(completed_actions, total_actions_for_flow)
                if total_value <= 0:
                    total_value = FLOW_ACTION_COUNTS.get(flow_name, 10)
                progress_callback(profile_id_value, completed_value, total_value)
            except Exception:
                pass

    if callable(should_stop) and should_stop():
        if shutdown_on_abort:
            logger.info("Shutting down profile %s because the workflow was aborted", profile_id_value)
            shutdown_client.shutdown_profiles([profile_id_value])
        else:
            logger.info("Workflow aborted for profile %s without shutting it down", profile_id_value)
        if callable(status_callback):
            status_callback(profile_id_value, "failed")
        return

    if isinstance(flow_result, dict):
        # IG flagged the account (ban / human-verification / action-block). The
        # flow reports the specific kind in "account_flag"; the older bare
        # "human_verification": True is still honored for back-compat.
        account_flag = flow_result.get("account_flag")
        if not account_flag and flow_result.get("human_verification"):
            account_flag = "human_verification"
        if account_flag:
            # Deliberately left open: this is not a success, and for a
            # human-verification prompt the profile needs to stay up so the
            # challenge can be solved by hand.
            logger.warning(
                "Instagram flagged profile %s (%s); leaving the profile open for inspection",
                profile_id_value, account_flag,
            )
            if callable(status_callback):
                status_callback(profile_id_value, account_flag)
            return
        if flow_result.get("already_has_bio"):
            # Nothing went wrong -- the work was simply already done -- so this
            # follows the same "close when it succeeds" setting as a normal pass.
            logger.info("Profile %s already has a bio; skipping", profile_id_value)
            if shutdown_on_success:
                try:
                    shutdown_client.shutdown_profiles([profile_id_value])
                    logger.info("Closed profile %s because it already had a bio", profile_id_value)
                except Exception as exc:
                    logger.warning("Failed to close profile %s after already-has-bio detection: %s", profile_id_value, exc)
            if callable(status_callback):
                status_callback(profile_id_value, "already_had_bio")
            return
        # "Uncertain" is checked first: the flow reached the Share tap but could
        # not prove the outcome, so the reel may be live. Reporting that as
        # "failed" is what gets it posted a second time.
        if flow_result.get("uncertain", False) and not flow_result.get("aborted", False):
            logger.warning("Workflow outcome UNCERTAIN for profile %s -- Share was tapped but "
                           "the post could not be confirmed; check before re-posting",
                           profile_id_value)
            emit_status(profile_id_value, "uncertain", flow_result)
            return
        if flow_result.get("aborted", False) or flow_result.get("failed", False) or flow_result.get("success") is False:
            logger.info("Workflow failed for profile %s", profile_id_value)
            emit_status(profile_id_value, "failed", flow_result)
            return

    logger.info("Workflow completed for profile %s", profile_id_value)
    emit_status(profile_id_value, "done", flow_result if isinstance(flow_result, dict) else None)

    if shutdown_on_success:
        logger.info("Shutting down Multilogin profile %s after successful flow '%s' completion", profile_id_value, flow_name)
        try:
            shutdown_client.shutdown_profiles([profile_id_value])
        except Exception:
            logger.exception("Failed to shutdown Multilogin profile %s", profile_id_value)
    else:
        logger.info("Keeping Multilogin profile %s open after successful flow '%s' completion", profile_id_value, flow_name)


def main() -> None:
    logger = get_logger("test_main", log_file="logs/test_main.log")
    bearer_token = get_bearer_token()
    profile_ids = normalize_profile_ids(get_profile_ids(), DEFAULT_PROFILE_IDS)

    logger.info("Starting full workflow test for profiles %s", profile_ids)

    launcher_client = MultiloginLauncherClient(bearer_token)
    shutdown_client = MultiloginShutdownClient(bearer_token)
    adb_enable_client = MultiloginAdbEnableClient(bearer_token)
    api_client = MultiloginApiClient(bearer_token)
    automation = AutomationRunner()
    automation.register_flow(InstagramScrollFlow())
    automation.register_flow(InstagramLikeFeedFlow())
    automation.register_flow(InstagramNotificationsFlow())
    automation.register_flow(InstagramStoryUploadFlow())
    automation.register_flow(InstagramReelUploadFlow())
    automation.register_flow(InstagramReelUploadU2Flow())
    automation.register_flow(InstagramUpdateProfilePictureU2Flow())

    logger.info("Launching profiles %s on Multilogin", profile_ids)
    for profile_id in profile_ids:
        launch_response = launcher_client.start_profiles([profile_id])
        if launch_response.get("status") == "error":
            logger.error(
                "Failed to launch profile %s on Multilogin. Response: %s",
                profile_id,
                launch_response,
            )
            continue
        logger.info("Launched profile %s successfully", profile_id)
        time.sleep(1)

    logger.info("Waiting 40 seconds before checking profile readiness")
    time.sleep(40)

    with ThreadPoolExecutor(max_workers=len(profile_ids)) as executor:
        futures = [
            executor.submit(
                run_profile_workflow,
                profile_id,
                bearer_token,
                api_client,
                adb_enable_client,
                shutdown_client,
                automation,
                logger,
            )
            for profile_id in profile_ids
        ]
        for future in futures:
            future.result()

    logger.info("Workflow completed for profiles %s", profile_ids)


if __name__ == "__main__":
    main()
