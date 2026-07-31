"""Decide whether a reel actually posted, without trusting the banner.

Instagram's "Your reel was shared" banner is the obvious signal but an unreliable
one -- it doesn't always appear, and it can vanish before the next poll. Reporting
a real post as Failed is the worst outcome: the Posting Queue row gets retried and
the account posts twice.

So success is decided from several independent signals, strongest first:

1. **Post-count delta** -- the account's own profile post count going up. A
   number, not a phrase, so it can't be defeated by wording. The primary signal.
   Skipped when the UI rounds the count ("1.2K"), where +1 is invisible.
2. **Banner / toast phrase** -- fast path when it does show.
3. **Upload notification** -- Instagram posts an *ongoing* notification while
   uploading. Its presence proves work is still in flight (so we keep waiting
   rather than giving up); its disappearance means the upload finished one way
   or another. Detected by package + ongoing flag, so it's language-independent.
4. **Conclusive negatives** -- an error dialog, a "discard/keep draft" prompt, or
   still sitting on the composer means Share never took. These fail fast instead
   of burning the full timeout.

Timing: never declare failure before `min_wait` (default 3 min) -- a slow upload
must not be misjudged -- and give up at `timeout` (default 5 min). A positive
signal returns immediately.

The parsers and the state machine are pure: `verify_reel_posted` takes probe
callables, so the whole thing is unit-tested without a device.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

# Default timing. min_wait is a floor on *failure*, not on success.
DEFAULT_MIN_WAIT_SECONDS = 180     # 3 min: never call it failed before this
DEFAULT_TIMEOUT_SECONDS = 300      # 5 min: hard ceiling
DEFAULT_POLL_SECONDS = 3.0

IG_PACKAGE = "com.instagram.android"

# How the post was confirmed (or why it wasn't) -- recorded for the Run Log.
VIA_POST_COUNT = "post_count"
VIA_BANNER = "banner"
FAIL_TIMEOUT = "timeout"
FAIL_ERROR_DIALOG = "error_dialog"
FAIL_DRAFT = "draft_dialog"
FAIL_COMPOSER = "still_on_composer"

# --- screen states ------------------------------------------------------------
STATE_CONFIRMED = "confirmed"
STATE_ERROR = "error"
STATE_DRAFT = "draft"
STATE_COMPOSER = "composer"
STATE_UNKNOWN = "unknown"

# Banner/toast wording that means the reel went out. Kept reel-specific and
# celebratory: bare "posted"/"shared" also appear on the home feed and would
# false-positive while we sit there.
CONFIRMATION_PHRASES = (
    "your reel", "reel shared", "reel was shared", "reel is being shared",
    "reel posted", "high five", "thumbs up", "nice work", "way to go", "great job",
)

# A failed upload -- conclusive, no point waiting out the timeout.
ERROR_PHRASES = (
    "couldn't be shared", "could not be shared", "couldn't share", "not shared",
    "failed to share", "failed to post", "something went wrong", "try again later",
    "unable to post", "upload failed",
)

# IG offers to keep a draft when a post is abandoned -- means it did not go out.
DRAFT_PHRASES = ("keep draft", "discard draft", "save draft", "discard post", "discard reel")

# Still on the caption/share screen => the Share tap never registered.
COMPOSER_PHRASES = ("write a caption", "add a caption", "share to", "also share to", "cover photo")


@dataclass
class Count:
    """A parsed profile counter. `exact` is False when Instagram rounded it
    ("1.2K"), where a +1 delta is invisible and must not be trusted."""

    value: int
    exact: bool


@dataclass
class NotificationState:
    ig_present: bool = False
    ongoing: bool = False          # an ongoing IG notification => upload in flight
    titles: tuple = ()


@dataclass
class VerifyResult:
    confirmed: bool
    method: str                    # VIA_* when confirmed, FAIL_* when not
    detail: str = ""
    waited_seconds: float = 0.0

    def summary(self) -> str:
        state = "CONFIRMED" if self.confirmed else "NOT CONFIRMED"
        return f"{state} via {self.method} after {self.waited_seconds:.0f}s" + (f" ({self.detail})" if self.detail else "")


# --- pure parsers -------------------------------------------------------------

_ROUNDED = re.compile(r"(?i)\d\s*(k|m|b|mil|mio|tsd)\b|\d\s*[.,]\d\s*(k|m|mil|mio|tsd)")
_NUMBER = re.compile(r"\d[\d.,\s ]*")


def parse_count(text) -> Count | None:
    """Parse a profile counter like '1,234' / '1.234' / '12' / '1.2K'.

    Thousands separators differ by locale (1,234 vs 1.234), so all of ',', '.'
    and spaces are treated as separators -- we only need the integer. Returns
    None when there's no number at all.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    match = _NUMBER.search(raw)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    return Count(value=int(digits), exact=not bool(_ROUNDED.search(raw)))


def parse_notification_state(dumpsys_text) -> NotificationState:
    """Read `adb shell dumpsys notification` output for Instagram's presence and
    whether any of its notifications is ONGOING (FLAG_ONGOING_EVENT = 0x2),
    which is how an in-progress upload shows up. Format varies across Android
    versions, so this stays deliberately tolerant."""
    if not dumpsys_text:
        return NotificationState()
    lines = str(dumpsys_text).splitlines()
    present = False
    ongoing = False
    titles: list = []
    in_ig_record = False

    for line in lines:
        low = line.lower()
        if "notificationrecord" in low or "pkg=" in low:
            # A new record begins; are we inside an Instagram one?
            in_ig_record = IG_PACKAGE in low
            if in_ig_record:
                present = True
            continue
        if not in_ig_record:
            continue
        if "android.title=" in line:
            titles.append(line.split("android.title=", 1)[1].strip())
        flags = re.search(r"flags=0x([0-9a-fA-F]+)", line)
        if flags and (int(flags.group(1), 16) & 0x2):
            ongoing = True
    return NotificationState(ig_present=present, ongoing=ongoing, titles=tuple(titles))


def classify_post_screen(text) -> str:
    """Classify the post-Share screen text. Conclusive negatives are checked
    before the composer, because an error dialog is drawn *over* the composer."""
    if not text:
        return STATE_UNKNOWN
    low = str(text).lower()
    if any(p in low for p in CONFIRMATION_PHRASES):
        return STATE_CONFIRMED
    if any(p in low for p in ERROR_PHRASES):
        return STATE_ERROR
    if any(p in low for p in DRAFT_PHRASES):
        return STATE_DRAFT
    if any(p in low for p in COMPOSER_PHRASES):
        return STATE_COMPOSER
    return STATE_UNKNOWN


def count_increased(baseline: Count | None, current: Count | None) -> bool:
    """True only when both counts are exact and the current one is higher.
    A rounded count ("1.2K") can't show a +1, so it never confirms."""
    if baseline is None or current is None:
        return False
    if not baseline.exact or not current.exact:
        return False
    return current.value > baseline.value


# --- verification state machine ----------------------------------------------

def verify_reel_posted(
    baseline_count: Count | None = None,
    get_post_count=None,
    get_screen_text=None,
    get_notification_state=None,
    min_wait: float = DEFAULT_MIN_WAIT_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    should_stop=None,
    logger=None,
    emit=None,
    now=time.time,
    sleep=time.sleep,
) -> VerifyResult:
    """Watch the phone until the reel is provably posted, provably failed, or the
    timeout expires.

    All probes are optional callables so this can run with whatever the flow can
    actually read, and be unit-tested with fakes:
      - `get_post_count() -> Count | None`
      - `get_screen_text() -> str`
      - `get_notification_state() -> NotificationState`

    Returns a VerifyResult; `confirmed` is True only on a positive signal.
    """
    def log(level, message, *args):
        if callable(emit):
            emit(level, message, *args)
        elif logger is not None:
            getattr(logger, level, logger.info)(message, *args)

    started = now()
    deadline = started + timeout
    log("info", "Verifying reel post: baseline=%s, min %.0fs / max %.0fs",
        baseline_count.value if baseline_count else "n/a", min_wait, timeout)

    last_negative = None
    saw_upload_in_flight = False

    while True:
        if callable(should_stop) and should_stop():
            return VerifyResult(False, FAIL_TIMEOUT, "aborted", now() - started)

        # --- strongest positive: the profile's post count went up -------------
        if callable(get_post_count) and baseline_count is not None:
            try:
                current = get_post_count()
            except Exception as exc:
                current = None
                log("warning", "Post-count probe failed: %s", exc)
            if count_increased(baseline_count, current):
                return VerifyResult(
                    True, VIA_POST_COUNT,
                    f"post count {baseline_count.value} -> {current.value}", now() - started,
                )

        # --- banner / toast, plus conclusive negatives ------------------------
        state = STATE_UNKNOWN
        if callable(get_screen_text):
            try:
                state = classify_post_screen(get_screen_text())
            except Exception as exc:
                log("warning", "Screen probe failed: %s", exc)
        if state == STATE_CONFIRMED:
            return VerifyResult(True, VIA_BANNER, "confirmation banner seen", now() - started)
        if state == STATE_ERROR:
            return VerifyResult(False, FAIL_ERROR_DIALOG, "Instagram reported the post failed", now() - started)
        if state == STATE_DRAFT:
            return VerifyResult(False, FAIL_DRAFT, "a draft prompt appeared -- the post was not shared", now() - started)
        if state == STATE_COMPOSER:
            last_negative = FAIL_COMPOSER   # not conclusive yet: the banner may still be pending

        # --- upload notification: proof that work is still in flight ----------
        if callable(get_notification_state):
            try:
                notif = get_notification_state()
            except Exception:
                notif = None
            if notif is not None and notif.ongoing:
                saw_upload_in_flight = True

        elapsed = now() - started
        if elapsed >= timeout or now() >= deadline:
            break
        # Past the floor with the composer still up and nothing uploading: the
        # Share tap never registered, so stop early instead of waiting it out.
        if last_negative == FAIL_COMPOSER and elapsed >= min_wait and not saw_upload_in_flight:
            return VerifyResult(False, FAIL_COMPOSER,
                                "still on the composer and no upload in progress", elapsed)
        sleep(poll_seconds)

    waited = now() - started
    detail = "upload was still in progress at timeout" if saw_upload_in_flight else "no positive signal"
    return VerifyResult(False, last_negative or FAIL_TIMEOUT, detail, waited)
