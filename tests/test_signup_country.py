"""Typing a number from a country the picker is not set to.

Instagram pairs its number box with a country picker, and these phones open it
on `DE +49` because their proxy and locale are German. A German number goes in
as its national part. A number from anywhere else must go in whole, with its
`+` code -- otherwise the picker prefixes +49 to, say, a British national
number and Instagram texts a number that does not exist, which looks exactly
like the undelivered-code failure we are trying to escape.
"""

import unittest

from adb_bot.clients.sms.base import (
    COUNTRY_DE,
    COUNTRY_GB,
    COUNTRY_US,
    DIALLING_CODES,
    NumberOrder,
)
from adb_bot.clients.sms.router import NumberLease


def lease_for(digits, national, country):
    """`phone` is international digits with no leading `+`; `e164` adds it."""
    order = NumberOrder(provider="test", order_id="1", phone=digits,
                        country=country, national_number=national)
    return NumberLease(router=None, provider=None, order=order)


class TypedNumberTest(unittest.TestCase):

    def test_a_german_number_goes_in_as_its_national_part(self):
        lease = lease_for("4915905643069", "15905643069", COUNTRY_DE)
        self.assertEqual(lease.typed_number, "15905643069")

    def test_the_full_number_is_available_for_other_countries(self):
        """What the flow types for a non-German number, `+` and all."""
        lease = lease_for("447700900123", "7700900123", COUNTRY_GB)
        self.assertEqual(lease.e164, "+447700900123")
        # The national part alone would be the bug: the picker is on +49, so
        # this would go out as +49 7700900123.
        self.assertNotEqual(lease.e164, lease.typed_number)


class DiallingCodesTest(unittest.TestCase):

    def test_every_country_we_can_rent_from_has_a_dialling_code(self):
        """A missing code means the number cannot be split or rebuilt."""
        for country in (COUNTRY_DE, COUNTRY_US, COUNTRY_GB):
            with self.subTest(country=country):
                self.assertIn(country, DIALLING_CODES)

    def test_the_uk_code_is_right(self):
        self.assertEqual(DIALLING_CODES[COUNTRY_GB], "44")


class ProviderCountryMapsTest(unittest.TestCase):
    """Both providers must know every country, or the fallback is not one.

    The fallback exists to reach a *different pool*. On 2026-08-21 both
    providers were serving German numbers from the same +49 1590 56xx block,
    so falling back from one to the other reached the same burned range -- and
    a country only one provider knows would fail the same way for a different
    reason.
    """

    def test_smspool_knows_the_uk(self):
        from adb_bot.clients.sms.smspool import _COUNTRY_IDS

        self.assertEqual(_COUNTRY_IDS[COUNTRY_GB], 2)

    def test_fivesim_knows_the_uk(self):
        from adb_bot.clients.sms.fivesim import _COUNTRIES

        self.assertIn(COUNTRY_GB, _COUNTRIES)

    def test_both_providers_cover_the_same_countries(self):
        from adb_bot.clients.sms.fivesim import _COUNTRIES
        from adb_bot.clients.sms.smspool import _COUNTRY_IDS

        self.assertEqual(set(_COUNTRY_IDS), set(_COUNTRIES))


if __name__ == "__main__":
    unittest.main()
