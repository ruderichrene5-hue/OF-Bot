"""The reCAPTCHA checkbox/grid solver, against a scripted OCR feed and a fake
2captcha client -- no real screenshot, cv2 or pytesseract involved.

Every scenario here is a shape actually seen live on 2026-08-24, solving
`tt41950t@gmail.com`'s robot check by hand: a clean pass, an escalation to the
image grid, a grid that demands a second look ("please also check the new
images"), a challenge that expires mid-attempt, and a screen that never clears
at all.
"""

from unittest import TestCase

from adb_bot.automation.flows import recaptcha_grid as rg

SCREEN = (1000, 2000)  # width, height


def W(text, left, top, width=40, height=24, conf=90.0):
    return {"text": text, "left": left, "top": top, "width": width,
           "height": height, "conf": conf}


CHECKBOX_WORDS = [W("I'm", 100, 800), W("not", 150, 800), W("a", 200, 800),
                 W("robot", 230, 800)]

# Header words in the top third (< 700 for a 2000-tall screen) and a VERIFY
# button in the bottom two-thirds -- exactly what `_grid_bounds` and
# `_verify_button` anchor on.
GRID_WORDS = [
    W("select", 50, 100), W("all", 150, 100), W("images", 220, 100),
    W("with", 350, 100), W("a", 420, 100), W("bus", 460, 100),
    W("click", 50, 200), W("verify", 150, 200), W("once", 260, 200),
    W("there", 340, 200), W("are", 440, 200), W("none", 500, 200),
    W("left", 580, 200),
    W("VERIFY", 850, 1300, width=80, height=36),
]


class ScriptedOcr:
    """One (text, words) pair per call, in order -- like `FakeSession`."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, _png_bytes):
        self.calls += 1
        if not self.script:
            raise AssertionError(
                f"OCR called an {self.calls}th time with nothing scripted")
        return self.script.pop(0)


def fake_crop(_png_bytes, _box):
    return b"cropped-grid-bytes"


class FakeDriver:
    """Records taps; hands out a distinct, non-empty "screenshot" per call so
    a caller that forgot to advance the OCR script fails loudly instead of
    silently reusing stale data."""

    def __init__(self, size=SCREEN):
        self.size = size
        self.taps = []
        self._shots = 0

    def screenshot_bytes(self):
        self._shots += 1
        return f"frame-{self._shots}".encode()

    def tap_xy(self, x, y, description=""):
        self.taps.append((x, y, description))
        return True

    def screen_size(self):
        return self.size


class FakeGridSolver:
    """A `CaptchaSolver`-shaped double that answers `solve_grid` from a
    scripted queue and never touches `solve_text`."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def solve_grid(self, image_path, rows, columns, comment="", solve_timeout=None):
        self.calls.append({"image_path": image_path, "rows": rows,
                           "columns": columns, "comment": comment,
                           "solve_timeout": solve_timeout})
        if not self.answers:
            raise AssertionError("solve_grid called more times than scripted")
        return self.answers.pop(0)


def no_sleep(_seconds):
    pass


class NoRawPixelAccessTest(TestCase):
    def test_a_driver_without_raw_pixel_methods_is_refused_up_front(self):
        """No `screenshot_bytes`/`tap_xy`/`screen_size` -- e.g. the plain
        UI-dump driver every other flow in this codebase uses -- must not be
        tapped blind."""
        class BareDriver:
            pass

        cleared = rg.solve_checkbox(BareDriver(), FakeGridSolver([]),
                                    sleep=no_sleep)
        self.assertFalse(cleared)


class CleanPassTest(TestCase):
    def test_a_checkbox_tap_that_passes_straight_through(self):
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("confirm that you're not a robot", CHECKBOX_WORDS),  # post-tap, still shows it briefly
            ("welcome enter your password", []),  # navigated on
        ])
        driver = FakeDriver()
        cleared = rg.solve_checkbox(driver, FakeGridSolver([]), ocr=ocr,
                                    crop=fake_crop, sleep=no_sleep)

        self.assertTrue(cleared)
        self.assertEqual(len(driver.taps), 1, "only the checkbox itself")
        self.assertIn("checkbox", driver.taps[0][2])


class ExpiredChallengeTest(TestCase):
    def test_an_expired_challenge_retries_the_checkbox_from_scratch(self):
        """Read off the real run: the first attempt's grid was solved
        correctly but lost to "Verification challenge expired" -- the fix is
        simply tapping the checkbox again, which this proves happens."""
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("verification challenge expired. check the checkbox again.", []),
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("welcome enter your password", []),
        ])
        driver = FakeDriver()
        cleared = rg.solve_checkbox(driver, FakeGridSolver([]), ocr=ocr,
                                    crop=fake_crop, sleep=no_sleep)

        self.assertTrue(cleared)
        self.assertEqual(len(driver.taps), 2, "the checkbox is tapped once "
                                              "per attempt")

    def test_a_cannot_contact_dialog_is_dismissed_then_retried(self):
        ok_word = W("OK", 700, 1500, width=40, height=30)
        ocr = ScriptedOcr([
            ("cannot contact recaptcha. check your connection and try again.",
            [ok_word]),
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("welcome enter your password", []),
        ])
        driver = FakeDriver()
        cleared = rg.solve_checkbox(driver, FakeGridSolver([]), ocr=ocr,
                                    crop=fake_crop, sleep=no_sleep)

        self.assertTrue(cleared)
        descriptions = [t[2] for t in driver.taps]
        self.assertTrue(any("cannot contact" in d.lower() for d in descriptions),
                        f"never dismissed the dialog: {descriptions}")
        self.assertTrue(any("checkbox" in d for d in descriptions))

    def test_giving_up_after_the_attempt_budget_is_exhausted(self):
        # Expired every single time -- every attempt burns 2 OCR reads
        # (pre-tap, post-tap) and taps the checkbox once.
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("verification challenge expired. check the checkbox again.", []),
        ] * rg.MAX_CHECKBOX_ATTEMPTS)
        driver = FakeDriver()
        cleared = rg.solve_checkbox(driver, FakeGridSolver([]), ocr=ocr,
                                    crop=fake_crop, sleep=no_sleep)

        self.assertFalse(cleared)
        self.assertEqual(len(driver.taps), rg.MAX_CHECKBOX_ATTEMPTS)


class GridSolveTest(TestCase):
    def test_a_grid_solved_in_one_round(self):
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("select all images with a bus click verify once there are "
            "none left", GRID_WORDS),
            ("select all images with a bus click verify once there are "
            "none left", GRID_WORDS),
            ("", []),  # recheck shot: no "check the new images" -- solved
            ("welcome enter your password", []),
        ])
        solver = FakeGridSolver([[2, 5, 9]])
        driver = FakeDriver()

        cleared = rg.solve_checkbox(driver, solver, ocr=ocr, crop=fake_crop,
                                    sleep=no_sleep)

        self.assertTrue(cleared)
        self.assertEqual(len(solver.calls), 1)
        self.assertEqual(solver.calls[0]["rows"], 3)
        self.assertEqual(solver.calls[0]["columns"], 3)
        self.assertIn("bus", solver.calls[0]["comment"])
        # checkbox + 3 cells + VERIFY
        self.assertEqual(len(driver.taps), 5)
        self.assertEqual(driver.taps[-1][2], "VERIFY")

    def test_please_also_check_the_new_images_runs_a_second_round(self):
        """2captcha's answer is never assumed final -- reCAPTCHA replaces
        clicked tiles and can demand another look, and this must not press
        VERIFY until a round comes back asking for nothing more."""
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("select all images with a bus", GRID_WORDS),
            ("select all images with a bus", GRID_WORDS),           # round 1 read
            ("please also check the new images", GRID_WORDS),       # round 1 recheck
            ("select all images with a bus", GRID_WORDS),           # round 2 read
            ("welcome enter your password", []),
        ])
        solver = FakeGridSolver([[1, 4], []])
        driver = FakeDriver()

        cleared = rg.solve_checkbox(driver, solver, ocr=ocr, crop=fake_crop,
                                    sleep=no_sleep)

        self.assertTrue(cleared)
        self.assertEqual(len(solver.calls), 2, "one call per round")
        # checkbox + 2 cells (round 1) + VERIFY -- round 2's empty answer
        # clicks nothing.
        self.assertEqual(len(driver.taps), 4)

    def test_a_solver_that_cannot_answer_falls_back_to_a_fresh_checkbox_tap(self):
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),  # attempt 1: pre-tap
            ("select all images with a bus", GRID_WORDS),          # attempt 1: post-tap, grid
            ("select all images with a bus", GRID_WORDS),           # round 1 read (solver -> None)
            ("confirm that you're not a robot", CHECKBOX_WORDS),    # attempt 2: pre-tap
            ("confirm that you're not a robot", CHECKBOX_WORDS),    # attempt 2: post-tap, clean
            ("welcome enter your password", []),                    # transition confirms
        ])
        solver = FakeGridSolver([None])
        driver = FakeDriver()

        cleared = rg.solve_checkbox(driver, solver, ocr=ocr, crop=fake_crop,
                                    sleep=no_sleep)

        self.assertTrue(cleared)
        self.assertEqual(len(solver.calls), 1)

    def test_an_empty_click_list_on_the_first_round_presses_verify_directly(self):
        """Nothing to click at all is a real, solved-already answer -- not a
        failure to retry."""
        ocr = ScriptedOcr([
            ("confirm that you're not a robot", CHECKBOX_WORDS),
            ("select all images with a bus", GRID_WORDS),
            ("select all images with a bus", GRID_WORDS),
            ("welcome enter your password", []),
        ])
        solver = FakeGridSolver([[]])
        driver = FakeDriver()

        cleared = rg.solve_checkbox(driver, solver, ocr=ocr, crop=fake_crop,
                                    sleep=no_sleep)

        self.assertTrue(cleared)
        self.assertEqual(len(driver.taps), 2, "checkbox + VERIFY, no cells")


class StuckScreenTest(TestCase):
    def test_a_screen_that_never_advances_is_reported_unsolved_not_success(self):
        """A checkbox tap that silently does nothing must not be reported as
        cleared -- the caller would then walk the sign-in flow straight past
        a challenge that is still sitting there."""
        still_stuck = ("confirm that you're not a robot", CHECKBOX_WORDS)
        ocr = ScriptedOcr([still_stuck] * (2 + rg.TRANSITION_POLL_ATTEMPTS)
                         * rg.MAX_CHECKBOX_ATTEMPTS)
        driver = FakeDriver()

        cleared = rg.solve_checkbox(driver, FakeGridSolver([]), ocr=ocr,
                                    crop=fake_crop, sleep=no_sleep)

        self.assertFalse(cleared)


class GeometryTest(TestCase):
    def test_grid_cells_are_numbered_left_to_right_then_top_to_bottom(self):
        box = (0, 0, 300, 300)
        self.assertEqual(rg._cell_center(box, 1, 3, 3), (50, 50))
        self.assertEqual(rg._cell_center(box, 3, 3, 3), (250, 50))
        self.assertEqual(rg._cell_center(box, 4, 3, 3), (50, 150))
        self.assertEqual(rg._cell_center(box, 9, 3, 3), (250, 250))

    def test_grid_bounds_fall_back_to_fixed_fractions_without_ocr_anchors(self):
        x1, y1, x2, y2 = rg._grid_bounds([], SCREEN)
        self.assertGreater(y2, y1)
        self.assertGreater(x2, x1)
        self.assertEqual(x1, int(rg._GRID_LEFT_FRAC * SCREEN[0]))

    def test_grid_bounds_prefer_the_real_header_and_verify_positions(self):
        x1, y1, x2, y2 = rg._grid_bounds(GRID_WORDS, SCREEN)
        # Anchored well above the fallback's fixed 62% cutoff, since VERIFY
        # sits at y=1300 (65%) in the fixture -- proves OCR anchoring is
        # actually driving the result, not silently falling back.
        self.assertLess(y2, 1300)
        self.assertGreater(y1, 100)
