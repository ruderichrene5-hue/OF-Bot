"""Count what MultiLogin's launcher does to a run, so a bad night is visible.

The failure this exists for: MultiLogin's cloud answers a launch with

    HTTP 500 {"status":{"error_code":"INTERNAL_SERVER_ERROR","http_code":500,
              "message":"failed to get profiles starting urls"}}

(their own launcher log spells the same failure "failed to get profiles start
urls: internal server error" -- both spellings are matched below). It is their
side, it self-heals, and no single occurrence is worth waking anyone. What it
does do is burn a queue row's retry budget silently: the launch fails, the post
fails, `apply_post_result` bumps Retry Count, and after enough of them the row
lands on "Retries Exhausted" with nothing in the summary to say the bot was
never the problem.

Measured on 2026-08-04/05 (logs/loop_posting.log): 48 of 289 launch attempts
(16.6%) came back with this 500, in hourly bands from 2.9% to 50% -- while a
*separate* 30 attempts failed with "Connection refused" to the local launcher,
which is our side (the MLX agent was down). Those two must never land in the
same bucket: one means "wait it out", the other means "go fix the box". That
split is the whole point of the counters here.

Nothing here makes a call of its own. `CountingLauncherClient` wraps whatever
launcher client a run already uses and tallies the answers it was already
getting, so both the first launch and readiness' relaunch are counted with no
extra traffic and no new service.
"""

from __future__ import annotations

import threading

# Matches both spellings: the API body says "failed to get profiles starting
# urls", MultiLogin's own launcher log says "failed to get profiles start urls".
# The common prefix is deliberately short so a third wording of the same failure
# still lands in the right bucket.
MLX_START_URLS_MARKER = "failed to get profiles start"

# Outcome buckets. `OTHER` is "not their 500" -- a refused connection to the
# local launcher, a timeout, a 4xx, an unparseable answer: things that are ours
# to fix.
OK = "ok"
MLX_500 = "mlx_500"
OTHER = "other"


def _text(response) -> str:
    """A lowercase blob of everything the answer carries, for substring checks.

    The message can arrive in `response_text` (raw body kept by the launcher
    client's error envelope), in `error` (requests' exception string), or nested
    in a parsed body -- so stringify the whole thing rather than guessing which
    field holds it this time.
    """
    try:
        return str(response).lower()
    except Exception:  # pragma: no cover - str() on anything sane cannot fail
        return ""


def is_mlx_start_urls_failure(response) -> bool:
    """True if this answer is MultiLogin's 'failed to get profiles start urls'."""
    return MLX_START_URLS_MARKER in _text(response)


def _status_code(response) -> int:
    """The HTTP code of a launch answer, or 0 when there isn't one.

    Two shapes carry it: the launcher client's error envelope (`status_code`,
    None when the request never reached a server) and MultiLogin's body
    (`status.http_code`).
    """
    if not isinstance(response, dict):
        return 0
    for value in (response.get("status_code"),
                  ((response.get("status") or {}).get("http_code")
                   if isinstance(response.get("status"), dict) else None)):
        try:
            code = int(value)
        except (TypeError, ValueError):
            continue
        if code:
            return code
    return 0


def _reported_failure(response) -> bool:
    """A 200 that still launched nothing (`data.fail_amount` > 0).

    Counted as OTHER rather than as a cloud 500: the call succeeded, MultiLogin
    simply refused this profile (already running, quota, bad id), which is a
    different conversation from "their launcher is throwing".
    """
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    try:
        return int(data.get("fail_amount") or 0) > 0
    except (TypeError, ValueError):
        return False


def classify_launch(response, error: BaseException | None = None) -> str:
    """Bucket one launch answer: OK, MLX_500 (their cloud) or OTHER (ours).

    `error` is for callers that catch an exception instead of getting an
    envelope back -- it is classified on the same rules, since a raised
    HTTPError still carries the 500 text.
    """
    if error is not None:
        return MLX_500 if is_mlx_start_urls_failure(str(error)) else OTHER
    if is_mlx_start_urls_failure(response):
        return MLX_500
    code = _status_code(response)
    if 500 <= code <= 599:
        # Any 5xx from their launcher is their side, even when the message is a
        # wording we have not seen. Only the start-urls flavour is called out by
        # name in the summary; the rest still must not read as our breakage.
        return MLX_500
    if isinstance(response, dict) and response.get("status") == "error":
        return OTHER
    if _reported_failure(response):
        return OTHER
    return OK


class LaunchStats:
    """Per-run launch tally. Cheap, thread-safe, no I/O.

    Thread-safe because relaunches are issued from the worker threads (readiness
    relaunches a profile whose launch didn't take), not only from the serialised
    LaunchGate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.attempts = 0
        self.ok = 0
        self.mlx_500 = 0
        self.start_urls = 0
        self.other = 0
        self.retries_consumed = 0
        # Which profiles this run saw a cloud 500 for. Used to attribute a
        # queue row's retry bump to MultiLogin rather than to the bot.
        self.mlx_500_profiles: set[str] = set()

    def record(self, response, profile_ids=None, error: BaseException | None = None) -> str:
        """Tally one launch call and return its bucket."""
        bucket = classify_launch(response, error=error)
        ids = [str(pid) for pid in (profile_ids or []) if pid]
        with self._lock:
            self.attempts += 1
            if bucket == OK:
                self.ok += 1
            elif bucket == MLX_500:
                self.mlx_500 += 1
                if error is None and is_mlx_start_urls_failure(response):
                    self.start_urls += 1
                elif error is not None and is_mlx_start_urls_failure(str(error)):
                    self.start_urls += 1
                self.mlx_500_profiles.update(ids)
            else:
                self.other += 1
        return bucket

    def hit_mlx_500(self, profile_id) -> bool:
        """Whether this profile's launch hit a cloud 500 during this run."""
        with self._lock:
            return str(profile_id) in self.mlx_500_profiles

    def note_retry_consumed(self, profile_id) -> bool:
        """Count a retry bump that a cloud 500 is responsible for.

        Only counted for a profile whose launch actually 500ed this run --
        otherwise the number would quietly absorb our own failures, which is the
        one thing these counters exist to prevent.
        """
        if not self.hit_mlx_500(profile_id):
            return False
        with self._lock:
            self.retries_consumed += 1
        return True

    @property
    def rate(self) -> float:
        """Share of launch attempts that hit a MultiLogin-side 500 (0.0-1.0).

        Zero attempts is a real state (every due profile busy in another loop),
        not an error: it reports 0.0 rather than dividing by zero.
        """
        with self._lock:
            if self.attempts <= 0:
                return 0.0
            return self.mlx_500 / self.attempts

    def summary(self) -> str:
        """The one line a bad night should be readable from."""
        line = (f"MLX launch health: {self.attempts} attempt(s), {self.ok} ok, "
                f"{self.mlx_500} MLX-side 500 ({self.rate * 100:.1f}%), "
                f"{self.other} our-side/other failure(s), "
                f"{self.retries_consumed} queue retry(s) burned by MLX 500s")
        if self.start_urls:
            line += f" [{self.start_urls} x 'failed to get profiles start urls']"
        return line

    def as_dict(self) -> dict:
        """The same numbers for the run-result dict the loops log and return."""
        return {
            "launch_attempts": self.attempts,
            "launch_ok": self.ok,
            "mlx_500": self.mlx_500,
            "mlx_500_rate": round(self.rate, 4),
            "launch_failures_other": self.other,
            "mlx_500_retries": self.retries_consumed,
        }


class CountingLauncherClient:
    """A launcher client that tallies its answers into a LaunchStats.

    Wrapping the client rather than instrumenting each call site means the
    readiness relaunch inside `prepare_profile_for_adb` is counted too -- it gets
    the same object -- and no launch path can be added later that silently
    escapes the count.
    """

    def __init__(self, client, stats: LaunchStats | None = None) -> None:
        self._client = client
        self.stats = stats if stats is not None else LaunchStats()

    def start_profiles(self, profile_ids):
        try:
            response = self._client.start_profiles(profile_ids)
        except Exception as exc:
            self.stats.record(None, profile_ids=profile_ids, error=exc)
            raise
        self.stats.record(response, profile_ids=profile_ids)
        return response

    def __getattr__(self, name):
        # Everything else (bearer_token, base_url, ...) belongs to the real
        # client; the wrapper only has an opinion about start_profiles.
        return getattr(self._client, name)
