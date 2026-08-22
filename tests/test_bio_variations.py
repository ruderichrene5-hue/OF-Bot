import random
import unittest

from adb_bot.automation import bio_variations as bv


class RandomCtaTest(unittest.TestCase):
    def test_always_returns_something_from_the_pool(self):
        rand = random.Random(1)
        for _ in range(20):
            self.assertIn(bv.random_cta(rand=rand), bv.DEFAULT_CTAS)

    def test_a_custom_pool_is_respected(self):
        pool = ("Only option",)
        self.assertEqual(bv.random_cta(pool=pool), "Only option")


class BuildBioTest(unittest.TestCase):
    def test_no_base_is_just_the_cta(self):
        rand = random.Random(2)
        bio = bv.build_bio(rand=rand)
        self.assertIn(bio, bv.DEFAULT_CTAS)

    def test_a_base_gets_the_cta_appended(self):
        rand = random.Random(3)
        bio = bv.build_bio(base="23 | coffee lover", rand=rand)
        self.assertTrue(bio.startswith("23 | coffee lover "))
        cta = bio[len("23 | coffee lover "):]
        self.assertIn(cta, bv.DEFAULT_CTAS)

    def test_repeated_calls_are_not_all_identical(self):
        """The whole point -- a batch of profiles must not all read the same
        way. Not a hard guarantee with a small pool, so this only checks
        that variation is possible, over enough draws to make an accidental
        all-same run implausible."""
        rand = random.Random(4)
        bios = {bv.build_bio(base="hey", rand=rand) for _ in range(30)}
        self.assertGreater(len(bios), 1)

    def test_surrounding_whitespace_on_the_base_does_not_leak_through(self):
        rand = random.Random(5)
        bio = bv.build_bio(base="  hey  ", rand=rand)
        self.assertTrue(bio.startswith("hey "))


if __name__ == "__main__":
    unittest.main()
