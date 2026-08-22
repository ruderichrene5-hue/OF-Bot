"""Labels that differ only in punctuation are the same label.

Android's permission dialog spells its button `DON'T ALLOW` with a typographic
apostrophe (U+2019). Every label list in this repo is typed with the ASCII one,
so the match failed silently and the flow tapped nothing -- on the dialog that
appears one screen AFTER the tap which creates the account, meaning a real
account was made and then abandoned mid-setup.

Normalising both sides beats adding a second spelling of every label: the same
character appears in "I didn't get the code" and anywhere else Instagram
writes an apostrophe, and each would otherwise be its own silent miss.
"""

import unittest

from adb_bot.automation.flows.signup_driver import normalise_label


class NormaliseLabelTest(unittest.TestCase):

    def test_the_dialog_button_matches_the_label_we_type(self):
        # Left: verbatim from the dump. Right: as written in the flow.
        self.assertEqual(normalise_label("DON’T ALLOW"),
                         normalise_label("DON'T ALLOW"))

    def test_the_allow_button_is_still_distinct(self):
        """Normalising must not collapse two different buttons together."""
        self.assertNotEqual(normalise_label("DON’T ALLOW"),
                            normalise_label("ALLOW"))

    def test_instagrams_other_curly_apostrophes(self):
        self.assertEqual(normalise_label("I didn’t get the code"),
                         normalise_label("I didn't get the code"))

    def test_case_and_surrounding_space_are_ignored(self):
        self.assertEqual(normalise_label("  Not Now  "),
                         normalise_label("not now"))

    def test_runs_of_whitespace_collapse(self):
        self.assertEqual(normalise_label("Confirm  with   a code"),
                         normalise_label("Confirm with a code"))

    def test_a_non_breaking_space_is_a_space(self):
        self.assertEqual(normalise_label("Not now"),
                         normalise_label("Not now"))

    def test_dashes_are_levelled(self):
        self.assertEqual(normalise_label("Sign–in"),
                         normalise_label("Sign-in"))

    def test_empty_and_none_are_empty(self):
        self.assertEqual(normalise_label(None), "")
        self.assertEqual(normalise_label(""), "")

    def test_exact_matching_is_preserved(self):
        """The safety property: "Not now" must never match a bare "now"."""
        self.assertNotEqual(normalise_label("Not now"), normalise_label("now"))


if __name__ == "__main__":
    unittest.main()
