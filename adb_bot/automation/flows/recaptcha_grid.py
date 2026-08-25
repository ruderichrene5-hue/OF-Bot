"""Clear a Google reCAPTCHA v2 challenge (checkbox, then maybe an image grid).

Solved live for the first time 2026-08-24, by hand, on `tt41950t@gmail.com`
mid-sign-in (`google_signin.SCREEN_ROBOT_CHECK`). Everything below was learned
on that run:

* **The checkbox is not in the UI dump.** It renders inside a `WebView`, which
  `uiautomator` reports as one opaque node -- no `checkable`, no bounds worth
  anything. The only way to find it, tap it, or read what it turned into is
  pixels: a screenshot and OCR, never the dump the rest of this codebase's
  flows are built on.
* **Passing the checkbox is not always enough.** Sometimes it goes straight to
  a green check; sometimes it escalates to a 3x3 image grid ("select all
  images with a bus"). Both are real, both have to be handled -- there is no
  way to predict which one a given attempt gets.
* **The grid answer is not a one-shot.** Clicking the matching tiles gets some
  of them replaced with fresh images and the banner "Please also check the new
  images" -- reCAPTCHA's own defence against a static answer. The tiles that
  are genuinely done turn blank with a checkmark; solving means looping this
  until a round comes back with nothing left to click, then pressing VERIFY.
* **The whole thing is on a clock.** The first live attempt was solved
  correctly (by eye) but lost anyway: "Verification challenge expired. Check
  the checkbox again." -- the time spent looking at the picture and deciding
  cost the challenge itself. There is no published number for the budget; this
  module keeps its own attempts fast rather than trying to find one.
* **A dialog can interrupt mid-flow.** "Cannot contact reCAPTCHA. Check your
  connection and try again." appeared once, unprompted, between a checkbox tap
  and the grid rendering -- transient (a proxy hiccup, going by timing), and
  gone after dismissing it and tapping the checkbox again.

What actually answers a grid is 2captcha's `GridTask`
(`adb_bot.clients.captcha.TwoCaptchaSolver.solve_grid`) -- the same account and
API already wired up for Instagram's text captcha. This module's job is
finding the checkbox and the grid on screen, cropping the picture 2captcha
needs, and driving the tap/re-check loop around its answers; it does no image
classification of its own.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

try:
    import cv2
    import numpy as np
    import pytesseract
    from PIL import Image
except ImportError:  # pragma: no cover -- exercised via the `ocr=None` guard
    cv2 = None
    np = None
    pytesseract = None
    Image = None

GRID_ROWS = 3
GRID_COLUMNS = 3

# Substrings, always matched against lowercased OCR text -- same convention as
# `google_signin.classify_google_screen`.
_CHECKBOX_MARKER = "not a robot"
_GRID_HEADER_MARKER = "select all images"
_GRID_RECHECK_MARKER = "check the new images"
_CHALLENGE_EXPIRED_MARKER = "challenge expired"
_CANNOT_CONTACT_MARKER = "cannot contact recaptcha"
_VERIFY_WORD = "verify"
_OK_WORD = "ok"

# How many times to re-tap the checkbox from scratch -- covers an expired
# challenge or a "cannot contact" dialog, each of which throws the whole
# attempt away and starts over. Bounded because each attempt costs real
# phone-life, and a mailbox that fails this many times is worth a fresh look
# by a person rather than a fourth identical try.
MAX_CHECKBOX_ATTEMPTS = 3

# How many "solve the visible tiles, check for new ones" rounds to run before
# giving up on a single grid and pressing VERIFY anyway. reCAPTCHA's own
# behaviour tonight needed 2; this leaves room without chasing it forever.
MAX_GRID_ROUNDS = 4

# Paced to move fast without outrunning the phone -- the expired-challenge
# failure was about total elapsed time, not any one wait here.
SETTLE_SECONDS = 2.0
VERIFY_SETTLE_SECONDS = 3.5
# After a successful VERIFY, Google's own page still shows the (now solved)
# checkbox for a moment before navigating on. Polled rather than a single
# fixed sleep, so a fast transition is not held up and a slow one is not
# mistaken for a failure.
TRANSITION_POLL_ATTEMPTS = 5
TRANSITION_POLL_SECONDS = 2.0

# 2captcha's own solve is fast (10-20s typically) -- capped well under that
# combined with everything else in a round, because a slow answer nobody can
# use before the challenge expires is worth abandoning rather than sitting out
# 2captcha's full two-minute default.
GRID_SOLVE_TIMEOUT_SECONDS = 45

# The attempt/round counts above bound how many *tries* this makes, not how
# long they can take -- and a single grid round can itself run up to
# `GRID_SOLVE_TIMEOUT_SECONDS`. Nothing here has a measured number for how
# long Google actually leaves a challenge live before expiring it (the one
# data point is "long enough to lose one solved by a slow human"), so this is
# a guess at a ceiling comfortably past a real solve and well short of
# burning the phone's ~15-minute life on a challenge almost certainly already
# gone stale.
OVERALL_DEADLINE_SECONDS = 150

# Fallback fractional grid bounds, calibrated against the real device this was
# solved on (2026-08-24, 1268x2756). Used only when OCR anchoring fails to
# find both edges -- normal operation locates the grid from the actual
# instruction text and VERIFY button on screen, which adapts to whatever the
# header wrapped to and to a different screen size.
_FALLBACK_GRID_TOP_FRAC = 0.18
_FALLBACK_GRID_BOTTOM_FRAC = 0.62
_GRID_LEFT_FRAC = 0.02
_GRID_RIGHT_FRAC = 0.98


def _emit(logger, level, message, *args):
    if logger is not None:
        getattr(logger, level)("recaptcha_grid: " + message, *args)


def _default_ocr(image_bytes: bytes):
    """(lowercased full text, [{"text","left","top","width","height","conf"}]).

    ([], "") -- not an exception -- if OCR is unavailable or the image will
    not decode. Every caller here already has to handle "read nothing" as a
    normal outcome, the same as every other captcha path in this codebase.
    """
    if cv2 is None or np is None or pytesseract is None or Image is None:
        return "", []
    try:
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if image is None:
            return "", []
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
    except Exception:
        return "", []

    words = []
    texts = []
    for i in range(len(data.get("text", []))):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        try:
            conf = float(data.get("conf", [])[i])
        except (ValueError, TypeError, IndexError):
            conf = -1.0
        texts.append(word)
        words.append({
            "text": word,
            "left": int(data["left"][i]),
            "top": int(data["top"][i]),
            "width": int(data["width"][i]),
            "height": int(data["height"][i]),
            "conf": conf,
        })
    return " ".join(texts).lower(), words


def _crop_png(image_bytes: bytes, box: tuple[int, int, int, int]) -> bytes | None:
    """PNG bytes of `box` (x1, y1, x2, y2) cropped out of `image_bytes`."""
    if cv2 is None or np is None:
        return None
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    x1, x2 = max(0, min(width, x1)), max(0, min(width, x2))
    y1, y2 = max(0, min(height, y1)), max(0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    ok, encoded = cv2.imencode(".png", image[y1:y2, x1:x2])
    return encoded.tobytes() if ok else None


# Per-word horizontal offset (in label-heights) from that word's own left
# edge back to the checkbox -- calibrated against a real device (2026-08-24,
# 1268x2756): "I'm" at left=254 -> checkbox at ~169 is 2.4 label-heights;
# "not" at left=324 -> ~5.0; "robot" at left=437 -> ~7.7. Each word sits
# further right within "I'm not a robot", so each needs its own multiplier
# rather than one constant applied to whichever word OCR happened to find.
_CHECKBOX_LABEL_OFFSETS = {"i'm": 2.4, "not": 5.0, "robot": 7.7}


# How close (px) a candidate word must sit to the "reCAPTCHA" wordmark's own
# row to count as the widget's own label rather than Google's static heading
# above it -- calibrated against a real device: the real label sits ~56px
# from the logo's own row, the heading ~290px. Comfortably between the two.
_LOGO_ROW_TOLERANCE_PX = 150


def _locate_checkbox(words, screen_size) -> tuple[int, int] | None:
    """The checkbox square, from the "I'm not a robot" label beside it.

    reCAPTCHA draws the checkbox immediately left of its own label,
    vertically centred on it -- consistent with the widget's own fixed CSS
    layout regardless of device resolution.

    Google's outer "Confirm that you're not a robot" heading, which sits
    *above* the checkbox, carries the same words "not"/"robot" -- sometimes
    at the same OCR confidence as the checkbox's own label, sometimes as the
    *only* readable instance at all (the WebView-rendered inner label failed
    to OCR at all on one real run, 2026-08-24, leaving the heading as the
    only candidate and mistapping ~240px right of the real widget). The
    "reCAPTCHA" wordmark is unique to the widget itself and was reliably read
    on every real capture so far, so it anchors the row the real label sits
    on; candidates far from that row are the heading and are dropped first.
    Falls back to the single lowest candidate (the real label is always
    below the heading) only when the logo itself could not be read either.
    """
    candidates = [w for w in words
                  if w["text"].strip(".,!?'").lower() in _CHECKBOX_LABEL_OFFSETS]
    if not candidates:
        return None
    logo = _find_word(words, "recaptcha", 0.0, 1.0, screen_size[1])
    if logo is not None:
        # The logo is unique and was reliably read on every real capture so
        # far -- when it is present, trust it completely. A candidate list
        # with nothing near it means only the heading's "not"/"robot" OCR'd
        # this time (confirmed live, 2026-08-24: the real label failed to
        # OCR at all, 3 attempts running, each mistapping the heading the
        # same ~240px-off way). Reporting "not found" here, rather than
        # guessing with the heading anyway, is what lets the caller's own
        # retry take a fresh screenshot instead of repeating a tap already
        # proven to land nowhere near the widget.
        logo_cy = logo["top"] + logo["height"] // 2
        candidates = [w for w in candidates if abs(
            (w["top"] + w["height"] // 2) - logo_cy) < _LOGO_ROW_TOLERANCE_PX]
        if not candidates:
            return None
    best = max(candidates, key=lambda w: w["top"])
    multiplier = _CHECKBOX_LABEL_OFFSETS[best["text"].strip(".,!?'").lower()]
    label_height = best["height"] or 20
    label_cy = best["top"] + best["height"] // 2
    checkbox_cx = max(0, int(best["left"] - label_height * multiplier))
    return checkbox_cx, label_cy


def _find_word(words, target: str, y_min_frac: float, y_max_frac: float,
               screen_height: int):
    """Highest-confidence word equal to `target`, within a vertical band."""
    y_min, y_max = y_min_frac * screen_height, y_max_frac * screen_height
    candidates = [w for w in words
                  if w["text"].strip(".,!?").lower() == target
                  and y_min <= w["top"] <= y_max]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w["conf"])


def _grid_bounds(words, screen_size) -> tuple[int, int, int, int]:
    """(x1, y1, x2, y2) of the 3x3 photo area, between the header and VERIFY.

    Anchored on real text rather than fixed pixels: the header's own bottom
    edge (the lowest word in the top third of the screen -- in practice the
    end of "...none left.") and the VERIFY button's top edge. Falls back to
    fractions calibrated on the run this was built from if either anchor is
    missing, rather than refusing outright.
    """
    width, height = screen_size
    top_words = [w for w in words if w["top"] < 0.35 * height]
    grid_top = (max(w["top"] + w["height"] for w in top_words) + 12
               if top_words else int(_FALLBACK_GRID_TOP_FRAC * height))

    verify_word = _find_word(words, _VERIFY_WORD, 0.35, 1.0, height)
    grid_bottom = (verify_word["top"] - 12 if verify_word is not None
                  else int(_FALLBACK_GRID_BOTTOM_FRAC * height))

    if grid_bottom <= grid_top:
        grid_top = int(_FALLBACK_GRID_TOP_FRAC * height)
        grid_bottom = int(_FALLBACK_GRID_BOTTOM_FRAC * height)

    return (int(_GRID_LEFT_FRAC * width), grid_top,
           int(_GRID_RIGHT_FRAC * width), grid_bottom)


def _cell_center(box, index: int, rows: int, columns: int) -> tuple[int, int]:
    """Centre of 1-indexed cell `index` (2captcha's own numbering: left to
    right, then top to bottom) within `box`."""
    x1, y1, x2, y2 = box
    cell_w, cell_h = (x2 - x1) / columns, (y2 - y1) / rows
    row, col = divmod(index - 1, columns)
    return int(x1 + cell_w * (col + 0.5)), int(y1 + cell_h * (row + 0.5))


def _header_text(words, screen_size) -> str:
    """The instruction banner's own words, for 2captcha's `comment` -- e.g.
    "select all images with a bus click verify once there are none left"."""
    _width, height = screen_size
    top_words = sorted((w for w in words if w["top"] < 0.35 * height),
                       key=lambda w: (w["top"], w["left"]))
    return " ".join(w["text"] for w in top_words)[:200]


def solve_checkbox(driver, solver, logger=None, sleep=time.sleep,
                   clock=time.monotonic, ocr=None, crop=None) -> bool:
    """Try to clear an on-screen reCAPTCHA v2 challenge. True if it cleared.

    `driver` needs `screenshot_bytes()`, `tap_xy(x, y, description)` and
    `screen_size()` (see `AdbChallengeDriver`) -- everything here works by
    pixels, never the UI dump. `solver` is a `CaptchaSolver`
    (`adb_bot.clients.captcha`); image grids are answered by its `solve_grid`.
    `ocr` and `crop` default to real OCR/cv2 (`_default_ocr`, `_crop_png`) and
    exist as seams so tests can exercise the tap/round-trip logic without
    either dependency installed.

    Never raises. A screenshot that will not decode, OCR that finds nothing,
    or a solver with no answer are all "could not clear it this attempt" --
    the same category as an already-flagged mailbox, which is what a caller
    falls back to reporting.
    """
    read = ocr or _default_ocr
    crop_fn = crop or _crop_png
    if not (hasattr(driver, "screenshot_bytes") and hasattr(driver, "tap_xy")
            and hasattr(driver, "screen_size")):
        _emit(logger, "info", "driver has no raw-pixel access; cannot attempt "
                              "the checkbox")
        return False

    size = driver.screen_size()
    if not size:
        _emit(logger, "warning", "could not read the screen size")
        return False

    deadline = clock() + OVERALL_DEADLINE_SECONDS
    for attempt in range(1, MAX_CHECKBOX_ATTEMPTS + 1):
        if clock() > deadline:
            _emit(logger, "warning", "over the %ss overall budget; giving up",
                 OVERALL_DEADLINE_SECONDS)
            break
        png = driver.screenshot_bytes()
        if not png:
            _emit(logger, "warning", "no screenshot; checkbox attempt %d/%d "
                                     "cannot proceed", attempt, MAX_CHECKBOX_ATTEMPTS)
            continue
        text, words = read(png)
        if _CANNOT_CONTACT_MARKER in text:
            ok_word = _find_word(words, _OK_WORD, 0.0, 1.0, size[1])
            if ok_word is not None:
                driver.tap_xy(ok_word["left"] + ok_word["width"] // 2,
                             ok_word["top"] + ok_word["height"] // 2,
                             "OK (cannot contact reCAPTCHA)")
                sleep(1.5)
            _emit(logger, "info", "reCAPTCHA could not be reached; retrying "
                                  "the checkbox (%d/%d)", attempt,
                  MAX_CHECKBOX_ATTEMPTS)

        # A grid can already be up when this attempt starts: the checkbox tap
        # on a PRIOR attempt escalated to it, but the post-tap check below
        # missed it (the grid had not finished drawing within `SETTLE_SECONDS`)
        # and `_wait_for_transition` false-positived on "the checkbox's own
        # text is gone" -- true, but only because the screen had moved to a
        # *harder* challenge, not a cleared one. Confirmed live 2026-08-25
        # (brendv748@gmail.com, lucas18anosff@gmail.com): "the reCAPTCHA
        # challenge cleared" was logged, the very next screen read was the
        # same robot check, and every retry after that searched for a
        # checkbox that the image grid had already replaced -- "checkbox not
        # found by OCR" three times, then gave up, never once calling
        # `_solve_grid`. Checking for the grid here, before assuming a
        # checkbox is what's on screen, is what the post-tap check further
        # down was already supposed to guarantee.
        if _GRID_HEADER_MARKER in text:
            cleared = _solve_grid(driver, solver, read, crop_fn, size, text,
                                  deadline, logger=logger, sleep=sleep, clock=clock)
            if cleared:
                return True
            _emit(logger, "info", "grid attempt %d/%d did not clear; trying "
                                  "the checkbox again", attempt,
                 MAX_CHECKBOX_ATTEMPTS)
            continue

        checkbox = _locate_checkbox(words, size)
        if checkbox is None:
            _emit(logger, "warning", "checkbox not found by OCR (attempt %d/%d)",
                 attempt, MAX_CHECKBOX_ATTEMPTS)
            continue
        # Kept cheap and always-on rather than debug-only: the one thing
        # that has actually gone wrong here (mistapping Google's static
        # heading instead of the checkbox's own label) is invisible from the
        # tapped coordinate alone, and re-diagnosing it live costs a phone
        # launch each time.
        _emit(logger, "info", "checkbox candidates: %s",
             [(w["text"], w["left"], w["top"], round(w["conf"])) for w in words
              if w["text"].strip(".,!?'").lower() in
              set(_CHECKBOX_LABEL_OFFSETS) | {"recaptcha"}])
        driver.tap_xy(checkbox[0], checkbox[1], "reCAPTCHA checkbox")
        sleep(SETTLE_SECONDS)

        png = driver.screenshot_bytes()
        if not png:
            continue
        text, words = read(png)

        if _GRID_HEADER_MARKER not in text:
            # No grid appeared -- either a clean pass, or the challenge
            # expired before the checkbox even registered. Either way there
            # is nothing to solve visually; let the transition check below
            # decide which one it was.
            if _CHALLENGE_EXPIRED_MARKER in text:
                _emit(logger, "info", "challenge expired before a grid even "
                                      "appeared; retrying (%d/%d)", attempt,
                      MAX_CHECKBOX_ATTEMPTS)
                continue
            if _wait_for_transition(driver, read, size, logger=logger, sleep=sleep):
                return True
            continue

        cleared = _solve_grid(driver, solver, read, crop_fn, size, text,
                              deadline, logger=logger, sleep=sleep, clock=clock)
        if cleared:
            return True
        _emit(logger, "info", "grid attempt %d/%d did not clear; trying the "
                              "checkbox again", attempt, MAX_CHECKBOX_ATTEMPTS)

    _emit(logger, "warning", "gave up after %d checkbox attempts",
         MAX_CHECKBOX_ATTEMPTS)
    return False


def _wait_for_transition(driver, read, size, logger=None, sleep=time.sleep) -> bool:
    """Poll until the checkbox screen's own text is gone -- Google has moved
    on to the next step, which is the only real evidence anything cleared.

    False if the budget runs out with the checkbox text still there: that is
    a stuck screen, not a slow success, and must not be reported as one -- a
    caller that believed a no-op tap had worked would tell the rest of the
    sign-in flow to carry on past a challenge that never actually cleared.

    Also False the moment an image grid appears: the checkbox's own text IS
    gone by then, which used to read as cleared, but the screen has moved to
    a *harder* challenge, not a solved one. Confirmed live 2026-08-25
    (brendv748@gmail.com, lucas18anosff@gmail.com) -- the single check right
    after the tap missed a grid that had not finished drawing yet within
    `SETTLE_SECONDS`, this poll then saw the checkbox gone and declared
    victory, and the sign-in carried on straight past an unsolved captcha
    until the very same robot check reappeared. False here instead sends the
    caller back around to its next attempt, where the grid -- now fully
    drawn -- is checked for before anything else.
    """
    for _ in range(TRANSITION_POLL_ATTEMPTS):
        png = driver.screenshot_bytes()
        if png:
            text, _words = read(png)
            if _GRID_HEADER_MARKER in text:
                return False
            if _CHECKBOX_MARKER not in text:
                return True
        sleep(TRANSITION_POLL_SECONDS)
    return False


def _solve_grid(driver, solver, read, crop_fn, size, header_text, deadline,
                logger=None, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Round-trip 2captcha against the visible grid until nothing is left to
    click, then press VERIFY. True if the challenge cleared afterwards."""
    comment = header_text
    for round_number in range(1, MAX_GRID_ROUNDS + 1):
        if clock() > deadline:
            _emit(logger, "warning", "over budget mid-grid (round %d); "
                                     "giving up on this attempt", round_number)
            return False
        png = driver.screenshot_bytes()
        if not png:
            return False
        text, words = read(png)
        if _GRID_HEADER_MARKER not in text:
            # Nothing left to check -- the previous round's answer was empty
            # and this is just confirming it, or the page already moved.
            break
        box = _grid_bounds(words, size)
        crop = crop_fn(png, box)
        if crop is None:
            _emit(logger, "warning", "could not crop the grid image")
            return False

        header = _header_text(words, size) or comment
        comment = header
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(crop)
            tmp_path = handle.name
        try:
            cells = solver.solve_grid(tmp_path, rows=GRID_ROWS, columns=GRID_COLUMNS,
                                      comment=comment,
                                      solve_timeout=GRID_SOLVE_TIMEOUT_SECONDS)
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

        if cells is None:
            _emit(logger, "warning", "2captcha could not answer round %d",
                 round_number)
            return False
        if not cells:
            _emit(logger, "info", "round %d: nothing left to click", round_number)
            break

        _emit(logger, "info", "round %d: clicking cells %s", round_number, cells)
        for cell in cells:
            cx, cy = _cell_center(box, cell, GRID_ROWS, GRID_COLUMNS)
            driver.tap_xy(cx, cy, f"grid cell {cell}")
        sleep(SETTLE_SECONDS)

        png = driver.screenshot_bytes()
        if not png:
            return False
        text, _words = read(png)
        if _GRID_RECHECK_MARKER not in text:
            # reCAPTCHA always shows this prompt when it has replaced tiles
            # that still need checking -- its absence, header gone or not, is
            # what "nothing left to click" looks like.
            break

    driver.tap_xy(*_verify_button(words, size), "VERIFY")
    sleep(VERIFY_SETTLE_SECONDS)
    return _wait_for_transition(driver, read, size, logger=logger, sleep=sleep)


def _verify_button(words, size) -> tuple[int, int]:
    word = _find_word(words, _VERIFY_WORD, 0.35, 1.0, size[1])
    if word is not None:
        return word["left"] + word["width"] // 2, word["top"] + word["height"] // 2
    width, height = size
    return int(width * 0.87), int(height * 0.65)
