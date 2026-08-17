"""Build the automation runner and MultiLogin device clients from a bearer token.

The UI wires these up inline; the headless loops (run_loop.py, Task Scheduler)
need the same wiring without a window, so it lives here in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from adb_bot.automation import AutomationRunner
from adb_bot.clients.api import MultiloginApiClient
from adb_bot.clients.multilogin import (
    MultiloginAdbEnableClient,
    MultiloginLauncherClient,
    MultiloginShutdownClient,
)
from adb_bot.automation.flows import (
    InstagramLikeFeedFlow,
    InstagramNotificationsFlow,
    InstagramPhotoPostU2Flow,
    InstagramReelIntentProbeFlow,
    InstagramReelUploadFlow,
    InstagramReelUploadU2Flow,
    InstagramScrollFlow,
    InstagramStoryUploadFlow,
    InstagramUpdateBioFlow,
    InstagramUpdateBioU2Flow,
    InstagramUpdateProfilePictureU2Flow,
    InstagramWarmUpDay1Flow,
    PushMediaTestFlow,
    ReelPostCountProbeFlow,
)


def build_automation() -> AutomationRunner:
    """An AutomationRunner with every Instagram flow registered (same set the UI
    registers). Keep this list in sync with the UI's registration block."""
    automation = AutomationRunner()
    for flow in (
        InstagramScrollFlow(),
        InstagramLikeFeedFlow(),
        InstagramNotificationsFlow(),
        InstagramStoryUploadFlow(),
        InstagramReelUploadFlow(),
        InstagramReelUploadU2Flow(),
        InstagramPhotoPostU2Flow(),
        InstagramUpdateBioFlow(),
        InstagramUpdateBioU2Flow(),
        InstagramUpdateProfilePictureU2Flow(),
        InstagramWarmUpDay1Flow(),
        PushMediaTestFlow(),
        # Read-only. Used by the deferred recheck loop to resolve posts that
        # could not be confirmed in-run; posts nothing itself.
        ReelPostCountProbeFlow(),
        # Diagnostic only -- never posts. Not referenced by lifecycle or the
        # posting loop, so scheduled runs can't pick it up.
        InstagramReelIntentProbeFlow(),
    ):
        automation.register_flow(flow)
    return automation


@dataclass
class MlxClients:
    api: MultiloginApiClient
    launcher: MultiloginLauncherClient
    shutdown: MultiloginShutdownClient
    adb_enable: MultiloginAdbEnableClient


def build_mlx_clients(bearer_token: str) -> MlxClients:
    return MlxClients(
        api=MultiloginApiClient(bearer_token),
        launcher=MultiloginLauncherClient(bearer_token),
        shutdown=MultiloginShutdownClient(bearer_token),
        adb_enable=MultiloginAdbEnableClient(bearer_token),
    )
