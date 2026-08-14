"""Liveness check for the MLX profile a flow is currently driving.

Why this exists
---------------
A flow talks to a cloud phone over an ADB tunnel. If that phone goes away --
it shut down on its own, or somebody opened the same profile on another
machine -- nothing in the ADB layer notices: `adb connect` will happily
re-attach to whatever now answers at that address. That is how a server-side
run ended up driving a phone a human had opened on their own desktop.

`ProfileHeartbeat` is the guard. It is a `should_stop` callable, so it plugs
into the abort plumbing the flows already honour (they poll `should_stop()`
before and after every action). The flows therefore stop within one action of
the heartbeat tripping -- no changes needed inside the flows themselves.

Fail-closed: when the check fails, the profile is shut down. That deliberately
also closes a phone somebody has opened elsewhere, which is the point -- a
profile this run can no longer vouch for must not be left being driven.
"""
from __future__ import annotations

import time


# Consecutive MultiLogin API errors tolerated before the heartbeat gives up.
# A single blip shouldn't kill a good 10-minute run, but a sustained inability
# to confirm ownership must not read as "everything is fine".
MAX_CONSECUTIVE_API_ERRORS = 3

DEFAULT_INTERVAL_SECONDS = 60


class ProfileHeartbeat:
    """Callable returning True once the profile can no longer be vouched for.

    Cheap to call: the flows poll it many times a second's worth of actions, so
    real work happens at most once per `interval_seconds`. Once it trips it
    latches -- a run never un-aborts.
    """

    def __init__(
        self,
        profile_id: str,
        target: str,
        api_client,
        adb_client,
        shutdown_client,
        logger,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        inner_should_stop=None,
        shutdown_on_failure: bool = True,
    ) -> None:
        self.profile_id = profile_id
        self.target = target
        self.interval_seconds = max(1, int(interval_seconds))
        self._api = api_client
        self._adb = adb_client
        self._shutdown = shutdown_client
        self._logger = logger
        self._inner = inner_should_stop
        self._shutdown_on_failure = shutdown_on_failure

        self._stopped = False
        self._reason: str | None = None
        self._api_errors = 0
        # Start the clock at construction so the first check happens one full
        # interval into the flow, not immediately after connecting.
        self._last_check = time.monotonic()

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def reason(self) -> str | None:
        return self._reason

    def __call__(self) -> bool:
        # The caller's own stop signal (UI stop button, scheduler shutdown)
        # always wins and is checked every time -- it costs nothing.
        if callable(self._inner):
            try:
                if self._inner():
                    return True
            except Exception:  # a broken callback must not wedge the run
                pass

        if self._stopped:
            return True

        now = time.monotonic()
        if now - self._last_check < self.interval_seconds:
            return False
        self._last_check = now

        failure = self._check()
        if failure:
            self._trip(failure)
            return True
        return False

    def check_now(self) -> bool:
        """Force a check regardless of the interval. Returns True if tripped."""
        if self._stopped:
            return True
        self._last_check = time.monotonic()
        failure = self._check()
        if failure:
            self._trip(failure)
            return True
        return False

    # --- internals ----------------------------------------------------------

    def _check(self) -> str | None:
        """Return a human-readable failure reason, or None when still ours."""
        adb_failure = self._check_adb()
        if adb_failure:
            return adb_failure
        return self._check_multilogin()

    def _check_adb(self) -> str | None:
        try:
            state = (self._adb.get_state(self.target) or "").strip().lower()
        except Exception as exc:
            return f"could not read the adb state of {self.target}: {exc}"
        if state != "device":
            return f"adb reports {self.target} as '{state or 'gone'}' instead of 'device'"
        return None

    def _check_multilogin(self) -> str | None:
        # Imported here rather than at module scope: workflow.py imports this
        # module, so a top-level import would be circular.
        from adb_bot.automation.workflow import (
            coerce_profile,
            parse_profiles_from_response,
            profile_is_ready,
            profile_matches_id,
        )

        try:
            response = self._api.fetch_adb_credentials([self.profile_id])
            profiles = parse_profiles_from_response(self._api, response)
        except Exception as exc:
            self._api_errors += 1
            if self._api_errors >= MAX_CONSECUTIVE_API_ERRORS:
                return (
                    f"MultiLogin could not be reached {self._api_errors} times in a row, "
                    f"so the profile can no longer be confirmed as ours: {exc}"
                )
            self._log(
                "warning",
                "Heartbeat: MultiLogin check failed for profile %s (%s/%s consecutive): %s",
                self.profile_id, self._api_errors, MAX_CONSECUTIVE_API_ERRORS, exc,
            )
            return None
        self._api_errors = 0

        match = next((p for p in profiles if profile_matches_id(p, self.profile_id)), None)
        if match is None:
            return "MultiLogin no longer lists this profile as running"
        if not profile_is_ready(match):
            return "MultiLogin reports the profile is no longer running"

        current_target = coerce_profile(match, self.profile_id).target
        if current_target and current_target != self.target:
            # The phone was restarted, most likely by somebody opening the same
            # profile elsewhere. Reconnecting would mean driving their session.
            return (
                f"the profile moved to a different adb endpoint "
                f"({self.target} -> {current_target}); another session has taken it over"
            )
        return None

    def _trip(self, reason: str) -> None:
        self._stopped = True
        self._reason = reason
        self._log(
            "error",
            "Heartbeat FAILED for profile %s: %s -- aborting the flow",
            self.profile_id, reason,
        )
        if not self._shutdown_on_failure or self._shutdown is None:
            return
        try:
            self._log(
                "warning",
                "Heartbeat: shutting profile %s down. This also closes it if it was "
                "opened on another machine -- intended, so no run keeps driving it.",
                self.profile_id,
            )
            self._shutdown.shutdown_profiles([self.profile_id])
        except Exception as exc:
            self._log("warning", "Heartbeat: shutdown of profile %s failed: %s", self.profile_id, exc)

    def _log(self, level: str, message: str, *args) -> None:
        if self._logger is None:
            return
        try:
            getattr(self._logger, level)(message, *args)
        except Exception:
            pass
