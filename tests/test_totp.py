"""TOTP codes, checked against the standard's own vectors.

Worth pinning rather than trusting: a wrong implementation here does not look
wrong. Google answers a bad authenticator code with a generic "something went
wrong", the same message it gives for half a dozen other failures, so a broken
secret decode would read as "Google refuses to provision on these phones" --
which is a conclusion this project has already drawn once and had to retract.
"""

from adb_bot.automation import totp

# RFC 6238 appendix B, SHA-1: the ASCII secret "12345678901234567890" in base32.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
]


def test_rfc6238_vectors():
    for when, eight_digits in RFC_VECTORS:
        assert totp.code_at(RFC_SECRET, when) == eight_digits[-6:]


def test_a_secret_is_accepted_the_way_the_base_stores_it():
    """The `2FA Secret Key` column holds Google's display form."""
    spaced = "geza gnbv gy3t qojq geza gnbv gy3t qojq"
    assert totp.normalise_secret(spaced) == "GEZAGNBVGY3TQOJQGEZAGNBVGY3TQOJQ"
    # Same secret, two spellings, one code.
    assert totp.code_at("gezdgnbvgy3tqojqgezdgnbvgy3tqojq", 59) == \
        totp.code_at(RFC_SECRET, 59)


def test_a_secret_missing_its_padding_still_decodes():
    """Pasted secrets rarely carry the `=` padding."""
    assert len(totp.code_at("JBSWY3DPEHPK3PXP", 59)) == 6


def test_a_code_is_always_six_digits():
    for when, _ in RFC_VECTORS:
        code = totp.code_at(RFC_SECRET, when)
        assert len(code) == 6 and code.isdigit()


def test_seconds_left_is_within_the_window():
    for when in (0, 15, 29, 30, 59, 1755374400):
        assert 1 <= totp.seconds_left(when) <= 30


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, start):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_a_dying_code_is_waited_out_rather_than_handed_over():
    """Typing one of these into a phone costs ~20s; a code with 3s left is a
    rejected login that looks like a wrong password."""
    clock = FakeClock(1755374425)              # 25s into a window -> 5 left
    code, left = totp.fresh_code(RFC_SECRET, min_seconds=8,
                                 sleep=clock.sleep, now=clock)
    assert clock.slept == [6]                  # waited the window out
    assert left >= 8                           # and reports the NEW window
    assert code == totp.code_at(RFC_SECRET, clock.now)


def test_a_code_with_time_on_it_is_handed_over_immediately():
    clock = FakeClock(1755374402)              # 2s in -> 28 left
    code, left = totp.fresh_code(RFC_SECRET, min_seconds=8,
                                 sleep=clock.sleep, now=clock)
    assert clock.slept == []
    assert left == 28
    assert code == totp.code_at(RFC_SECRET, 1755374402)
