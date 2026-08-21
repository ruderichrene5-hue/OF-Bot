"""Believe Instagram about what the account is called.

@nora450960 was created and written down as @nora.brandt -- a real password
filed under a handle that does not exist. That is the one unrecoverable way to
lose an account: the credentials survive, and there is nothing to use them on.

Everything upstream of creation is a guess about what the username box ended up
holding. The flow may have accepted Instagram's own suggestion rather than the
handle it typed, and the field is known to render values it never received. The
post-creation screen is the only source that cannot be wrong, because Instagram
is naming its own account.
"""

import unittest

from adb_bot.automation.flows.signup import handle_from_text

# Verbatim from `Katja new 1`, the run that recorded the wrong handle.
CHECKPOINT = (
    "get support menu confirm you're human to use your account, nora450960 "
    "confirm you're human to use your account, nora450960 continue continue "
    "takes about 30 seconds takes about 30 seconds"
)

# Verbatim from `Laila new 5`, a handle with no digits.
CHECKPOINT_DOTTED = (
    "get support menu confirm you're human to use your account, miakoenig89 "
    "confirm you're human to use your account, miakoenig89 continue"
)


class HandleFromTextTest(unittest.TestCase):

    def test_it_reads_the_handle_instagram_names(self):
        self.assertEqual(handle_from_text(CHECKPOINT), "nora450960")

    def test_it_reads_a_second_real_one(self):
        self.assertEqual(handle_from_text(CHECKPOINT_DOTTED), "miakoenig89")

    def test_a_dotted_handle_survives_intact(self):
        text = "confirm you're human to use your account, clara.keller3770 ok"
        self.assertEqual(handle_from_text(text), "clara.keller3770")

    def test_an_underscore_handle_survives(self):
        text = "confirm you're human to use your account, lena_berg1968 ok"
        self.assertEqual(handle_from_text(text), "lena_berg1968")

    def test_a_screen_that_names_nobody_yields_nothing(self):
        """Empty leaves the flow's own guess alone; a wrong answer would
        overwrite a handle that was right."""
        self.assertEqual(handle_from_text("create a username next back"), "")
        self.assertEqual(handle_from_text(""), "")
        self.assertEqual(handle_from_text(None), "")

    def test_the_signup_phone_screen_is_not_mistaken_for_a_naming(self):
        text = ("what's your mobile number? enter the mobile number where you "
                "can be contacted. no one will see this on your profile.")
        self.assertEqual(handle_from_text(text), "")


if __name__ == "__main__":
    unittest.main()
