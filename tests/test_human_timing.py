"""The dwell/jitter numbers themselves, independent of any driver."""

import random
import unittest

from adb_bot.core import human_timing as ht


class DwellTest(unittest.TestCase):
    def test_within_bounds_across_many_draws(self):
        rand = random.Random(1)
        for _ in range(200):
            d = ht.dwell_ms(rand=rand)
            self.assertGreaterEqual(d, ht.DWELL_MIN_MS)
            self.assertLessEqual(d, ht.DWELL_MAX_MS)

    def test_held_is_bounded_by_its_own_higher_floor(self):
        rand = random.Random(2)
        for _ in range(200):
            d = ht.dwell_ms(rand=rand, held=True)
            self.assertGreaterEqual(d, ht.HELD_MIN_MS)
            self.assertLessEqual(d, ht.HELD_MAX_MS)

    def test_held_is_typically_longer_than_a_plain_tap(self):
        rand = random.Random(3)
        plain = [ht.dwell_ms(rand=rand) for _ in range(300)]
        held = [ht.dwell_ms(rand=rand, held=True) for _ in range(300)]
        self.assertLess(sum(plain) / len(plain), sum(held) / len(held))

    def test_no_rand_given_still_produces_a_valid_value(self):
        """Production's default path -- a fresh `random.Random()` each call,
        not a shared or seeded one."""
        d = ht.dwell_ms()
        self.assertGreaterEqual(d, ht.DWELL_MIN_MS)
        self.assertLessEqual(d, ht.DWELL_MAX_MS)


class JitterTest(unittest.TestCase):
    def test_stays_inside_the_given_bounds(self):
        bounds = (100, 200, 300, 260)  # 200x60
        rand = random.Random(4)
        for _ in range(300):
            x, y = ht.jitter_point(200, 230, bounds=bounds, rand=rand)
            self.assertTrue(bounds[0] <= x <= bounds[2])
            self.assertTrue(bounds[1] <= y <= bounds[3])

    def test_a_small_element_is_not_pushed_toward_its_own_edge(self):
        """A 20x20 icon: the fractional jitter (28%) must win over the flat
        pixel ceiling, or a jitter proven safe on a big card gets reused
        here and can land right at the edge of a tiny target."""
        bounds = (100, 100, 120, 120)
        rand = random.Random(5)
        for _ in range(300):
            x, y = ht.jitter_point(110, 110, bounds=bounds, rand=rand)
            self.assertTrue(bounds[0] <= x <= bounds[2])
            self.assertTrue(bounds[1] <= y <= bounds[3])

    def test_without_bounds_the_offset_is_small_and_fixed(self):
        rand = random.Random(6)
        for _ in range(300):
            x, y = ht.jitter_point(500, 500, rand=rand)
            self.assertLessEqual(abs(x - 500), ht.JITTER_NO_BOUNDS_PX)
            self.assertLessEqual(abs(y - 500), ht.JITTER_NO_BOUNDS_PX)

    def test_it_is_not_always_dead_centre(self):
        rand = random.Random(7)
        points = {ht.jitter_point(500, 500, rand=rand) for _ in range(50)}
        self.assertGreater(len(points), 1, "50 taps all landed on one pixel")

    def test_same_seed_same_sequence(self):
        bounds = (0, 0, 1000, 1000)
        a = [ht.jitter_point(500, 500, bounds=bounds, rand=random.Random(9))
            for _ in range(10)]
        b = [ht.jitter_point(500, 500, bounds=bounds, rand=random.Random(9))
            for _ in range(10)]
        self.assertEqual(a, b)


class CurvedSwipePointsTest(unittest.TestCase):
    def test_starts_and_ends_exactly_on_the_requested_points(self):
        points = ht.curved_swipe_points(100, 2000, 100, 500, rand=random.Random(1))
        self.assertEqual(points[0], (100, 2000))
        self.assertEqual(points[-1], (100, 500))

    def test_returns_segments_plus_one_points(self):
        points = ht.curved_swipe_points(0, 0, 100, 1000, rand=random.Random(2),
                                        segments=5)
        self.assertEqual(len(points), 6)

    def test_zero_length_is_just_the_two_endpoints_no_crash(self):
        points = ht.curved_swipe_points(500, 500, 500, 500, rand=random.Random(3))
        self.assertEqual(points, [(500, 500), (500, 500)])

    def test_the_path_actually_bows_off_the_straight_line(self):
        """The whole point of this over `input swipe` -- if every
        intermediate point sat exactly on the line, a driver would be no
        better off using this than the plain two-endpoint gesture."""
        points = ht.curved_swipe_points(100, 2000, 100, 500, rand=random.Random(4))
        # A vertical line at x=100 -- any point off it has moved sideways.
        self.assertTrue(any(x != 100 for x, _y in points[1:-1]))

    def test_same_seed_same_path(self):
        a = ht.curved_swipe_points(100, 2000, 300, 500, rand=random.Random(5))
        b = ht.curved_swipe_points(100, 2000, 300, 500, rand=random.Random(5))
        self.assertEqual(a, b)

    def test_different_seeds_vary_the_bow(self):
        paths = {tuple(ht.curved_swipe_points(100, 2000, 300, 500,
                                              rand=random.Random(seed)))
                for seed in range(10)}
        self.assertGreater(len(paths), 1, "10 different seeds, one identical path")

    def test_waypoints_are_denser_near_both_ends_than_the_middle(self):
        """Ease-in/ease-out spacing -- with roughly even per-point timing,
        this is what makes the gesture read as accelerating away and
        decelerating into the target rather than one constant speed."""
        points = ht.curved_swipe_points(0, 0, 0, 1000, rand=random.Random(6),
                                        segments=4)
        # y-progress at each waypoint, ignoring the bow's own x-only drift.
        ys = [y for _x, y in points]
        first_step = ys[1] - ys[0]
        middle_step = ys[2] - ys[1]
        self.assertLess(first_step, middle_step,
                        "the first step should be the slow, easing-in one")


class SwipeDurationTest(unittest.TestCase):
    def test_within_the_requested_spread(self):
        rand = random.Random(10)
        for _ in range(200):
            d = ht.swipe_duration_ms(600, spread_frac=0.15, rand=rand)
            self.assertGreaterEqual(d, 600 * 0.85)
            self.assertLessEqual(d, 600 * 1.15)

    def test_never_zero_or_negative_even_for_a_tiny_base(self):
        rand = random.Random(11)
        for _ in range(200):
            self.assertGreaterEqual(ht.swipe_duration_ms(1, rand=rand), 1)

    def test_it_is_not_a_single_fixed_number(self):
        rand = random.Random(12)
        values = {ht.swipe_duration_ms(600, rand=rand) for _ in range(30)}
        self.assertGreater(len(values), 1)


if __name__ == "__main__":
    unittest.main()
