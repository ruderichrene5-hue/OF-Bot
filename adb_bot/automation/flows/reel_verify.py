"""Decide whether a reel actually posted, without trusting the banner.

Instagram's "Your reel was shared" banner is the obvious signal but an unreliable
one -- it doesn't always appear, and it can vanish before the next poll. Reporting
a real post as Failed is the worst outcome: the Posting Queue row gets retried and
the account posts twice.

So success is decided from several independent signals:

1. **Banner / toast phrase** -- checked first on every pass. Not because it is
   the most reliable (it isn't -- it often doesn't show at all) but because it
   is the most *perishable*: it's on screen for a few seconds and any
   navigation wipes it. Reading it costs one passive screen dump, so there is
   no reason to make it wait behind a probe that would erase it.
2. **Post-count delta** -- the account's own profile post count going up. A
   number, not a phrase, so it can't be defeated by wording. The signal that
   catches a silent success. Skipped when the UI rounds the count ("1.2K"),
   where +1 is invisible.
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

# Timing for callers that have somewhere to put an unproven post -- i.e. a
# deferred recheck queue rather than a Failed row.
#
# The three-minute floor above exists for exactly one reason: when "unproven"
# meant "Failed", declaring it early turned slow-but-successful uploads into
# double posts. Once an unproven post becomes *Verifying, look again in 15
# minutes*, that reason evaporates. Waiting out five minutes on a phone to reach
# a verdict we're going to revisit anyway is pure cost -- and with a budget of
# five minutes per profile for the whole post, it was most of the budget.
#
# So: keep polling long enough for the common fast confirmations (banner,
# notification, one post-count refresh at ~25s), then stop and hand off.
FAST_MIN_WAIT_SECONDS = 20         # composer/no-upload can be called this early
FAST_TIMEOUT_SECONDS = 45          # then it's the recheck queue's problem

# Don't *start* a post-count probe without at least this long left on the clock.
# That probe is a full navigation round trip (Home -> feed -> profile ->
# pull-to-refresh) and measured at 10-15s on these devices. The loop used to
# test the deadline only after running every probe, so one started at 44s ran to
# completion regardless -- on a real 56-profile run a 45s budget produced
# timeouts of up to 148s. Skipping a probe that cannot finish costs nothing now
# that the deferred recheck reads the same counter later, under better
# conditions and without holding a phone.
POST_COUNT_MIN_REMAINING_SECONDS = 12.0

IG_PACKAGE = "com.instagram.android"

# How the post was confirmed (or why it wasn't) -- recorded for the Run Log.
VIA_POST_COUNT = "post_count"
VIA_BANNER = "banner"
VIA_NOTIFICATION = "notification_title"
VIA_UPLOAD_FINISHED = "upload_finished"
FAIL_TIMEOUT = "timeout"
FAIL_ERROR_DIALOG = "error_dialog"
FAIL_DRAFT = "draft_dialog"
FAIL_COMPOSER = "still_on_composer"

# Confirmations ordered by how much they prove. Recorded so the Run Log shows
# whether a post was proven or merely inferred.
CONFIRMATION_STRENGTH = {
    VIA_POST_COUNT: "strong",
    VIA_BANNER: "strong",
    VIA_NOTIFICATION: "strong",
    VIA_UPLOAD_FINISHED: "inferred",
}

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
    uncertain: bool = False        # no positive signal AND no proof of failure

    @property
    def strength(self) -> str:
        return CONFIRMATION_STRENGTH.get(self.method, "none") if self.confirmed else "none"

    def summary(self) -> str:
        if self.confirmed:
            state = f"CONFIRMED ({self.strength})"
        elif self.uncertain:
            # The distinction that matters: we could not tell, which is NOT the
            # same as knowing it failed. Retrying an uncertain post is how an
            # account ends up posting the same reel twice.
            state = "UNCERTAIN"
        else:
            state = "FAILED"
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
    post_count_min_remaining: float = POST_COUNT_MIN_REMAINING_SECONDS,
    should_stop=None,
    logger=None,
    emit=None,
    now=time.time,
    sleep=time.sleep,
    what: str = "reel post",
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
    # `what` names the thing being verified in the log only -- every signal
    # this reads (post count, banner, notification) is the same for a reel and
    # a feed photo. It exists so a photo run does not narrate itself as a reel.
    log("info", "Verifying %s: baseline=%s, min %.0fs / max %.0fs",
        what, baseline_count.value if baseline_count else "n/a", min_wait, timeout)

    last_negative = None
    saw_upload_in_flight = False
    upload_finished = False

    # A rounded counter ("1.2K") can never show a +1, so the strongest signal is
    # unavailable for this account. Say so rather than letting it silently never
    # fire and look like the post failed.
    if baseline_count is not None and not baseline_count.exact:
        log("warning", "Post count is rounded (%s) -- the +1 check cannot work for this "
                       "account; relying on banner/notification signals",
            baseline_count.value)
    elif baseline_count is None:
        log("warning", "No baseline post count -- the strongest signal is unavailable; "
                       "relying on banner/notification signals")

    while True:
        if callable(should_stop) and should_stop():
            return VerifyResult(False, FAIL_TIMEOUT, "aborted", now() - started)

        # --- what's on screen right now, plus conclusive negatives ------------
        # Read the screen FIRST, before the post-count probe. Both signals count
        # as "strong", so nothing is given up by asking this one first -- but the
        # two are not equally polite. Reading the screen is passive and cheap;
        # the count probe navigates off the feed to the profile and back, which
        # *destroys the banner it would have read*. Asking the expensive probe
        # first meant a reel that announced itself on the feed got no credit and
        # the flow went hunting for a counter instead.
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

        # --- the profile's post count went up ---------------------------------
        # Only start this if there is room to finish it (see
        # POST_COUNT_MIN_REMAINING_SECONDS) -- it is the one probe that can
        # overrun the whole budget on its own.
        if callable(get_post_count) and baseline_count is not None:
            remaining = deadline - now()
            if remaining < post_count_min_remaining:
                log("info", "Skipping the post-count probe: only %.0fs left of the "
                            "verification budget and it needs ~%.0fs", remaining, post_count_min_remaining)
            else:
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

        # --- upload notification: in flight, then finished --------------------
        if callable(get_notification_state):
            try:
                notif = get_notification_state()
            except Exception:
                notif = None
            if notif is not None:
                if notif.ongoing:
                    saw_upload_in_flight = True
                elif saw_upload_in_flight:
                    # It was uploading and now it isn't. Nothing else to wait for.
                    upload_finished = True

                # Instagram's own notification text is a positive signal in its
                # own right, and survives longer than the on-screen banner that
                # a 3-second poll routinely misses.
                for title in (notif.titles or ()):
                    if any(p in str(title).lower() for p in CONFIRMATION_PHRASES):
                        return VerifyResult(True, VIA_NOTIFICATION,
                                            f"notification: {str(title)[:60]}", now() - started)

        # An upload that started and finished, with no error, no draft prompt and
        # not sitting on the composer, means the reel went out. This is weaker
        # than seeing the count rise, but it is *evidence*, and the alternative is
        # reporting a real post as failed -- which gets it posted twice. The
        # profile counter is cached on Instagram's side and often does not move
        # inside our window, so waiting for it alone is what produced the
        # false negatives.
        if upload_finished and state not in (STATE_COMPOSER, STATE_ERROR, STATE_DRAFT):
            return VerifyResult(True, VIA_UPLOAD_FINISHED,
                                "upload notification appeared and then cleared with no error",
                                now() - started)

        elapsed = now() - started
        if elapsed >= timeout or now() >= deadline:
            break
        # Past the floor with the composer still up and nothing uploading: the
        # Share tap never registered, so stop early instead of waiting it out.
        if last_negative == FAIL_COMPOSER and elapsed >= min_wait and not saw_upload_in_flight:
            return VerifyResult(False, FAIL_COMPOSER,
                                "still on the composer and no upload in progress", elapsed)
        # Never sleep past the deadline -- on a short budget a full poll
        # interval is a meaningful fraction of it.
        sleep(max(0.0, min(poll_seconds, deadline - now())))

    waited = now() - started
    # Timing out is not proof of failure -- it is the absence of proof. Only the
    # conclusive negatives (error dialog / draft prompt / stuck on the composer)
    # mean the post definitely did not go out.
    method = last_negative or FAIL_TIMEOUT
    detail = "upload was still in progress at timeout" if saw_upload_in_flight else "no positive signal"
    return VerifyResult(False, method, detail, waited, uncertain=(method == FAIL_TIMEOUT))
