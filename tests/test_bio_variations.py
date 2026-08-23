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


class BuildUsernameTest(unittest.TestCase):
    def test_a_blank_model_returns_empty_not_a_bare_separator_and_digits(self):
        self.assertEqual(bv.build_username(""), "")
        self.assertEqual(bv.build_username("   "), "")

    def test_has_exactly_one_separator_and_a_digit_tail(self):
        rand = random.Random(1)
        for _ in range(30):
            name = bv.build_username("Nikki", rand=rand)
            self.assertEqual(sum(name.count(s) for s in bv.SEPARATORS), 1)
            tail = name.rsplit(".", 1)[-1] if "." in name else name.rsplit("_", 1)[-1]
            self.assertTrue(tail.isdigit())

    def test_the_stem_is_the_model_name_or_the_model_name_with_one_letter_doubled(self):
        rand = random.Random(2)
        for _ in range(30):
            name = bv.build_username("Nikki", rand=rand)
            stem = name.rsplit(".", 1)[0] if "." in name else name.rsplit("_", 1)[0]
            self.assertEqual(stem[0], "N", "the first letter must never change")
            if stem == "Nikki":
                continue
            # Otherwise `stem` must be "Nikki" with exactly one extra
            # character that is a duplicate of its neighbour.
            self.assertEqual(len(stem), len("Nikki") + 1)
            reconstructed = any(stem[:i] + stem[i + 1:] == "Nikki"
                               for i in range(len(stem)))
            self.assertTrue(reconstructed,
                            f"{stem!r} is not Nikki with one letter doubled")

    def test_both_a_plain_and_a_doubled_letter_stem_are_possible(self):
        rand = random.Random(3)
        stems = set()
        for _ in range(40):
            name = bv.build_username("Nikki", rand=rand)
            stem = name.rsplit(".", 1)[0] if "." in name else name.rsplit("_", 1)[0]
            stems.add(len(stem))
        self.assertIn(len("Nikki"), stems, "never used the name exactly")
        self.assertIn(len("Nikki") + 1, stems, "never doubled a letter")

    def test_the_digit_tail_is_not_a_suspiciously_round_or_short_number(self):
        """A one- or two-digit tail (or "0") reads as obviously generated --
        the same floor `next_username` in signup.py already uses."""
        rand = random.Random(4)
        for _ in range(30):
            name = bv.build_username("Nikki", rand=rand)
            tail = name.rsplit(".", 1)[-1] if "." in name else name.rsplit("_", 1)[-1]
            self.assertGreaterEqual(int(tail), bv._TAIL_MIN)


if __name__ == "__main__":
    unittest.main()
