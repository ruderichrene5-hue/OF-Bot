"""Chaining the checkpoint onto the end of a signup.

Instagram holds a brand-new account behind "confirm you're human" within
seconds of creating it, so an account that stops there is real and unusable --
which is what every account this chain has ever made looks like. Clearing it has
to happen in the *same* launch, because a restarted Instagram comes back to
"Join Instagram" and the account cannot be picked up from a later run.

The expensive mistake these guard is not a crash. It is renting an SMS number --
real money, and 45 seconds before it is even usable -- on a phone that will die
before the code can be typed.
"""

from types import SimpleNamespace

from adb_bot.automation import signup_phone
from adb_bot.automation.flows import signup, verification


class _Recorded:
    """Captures what was written to the account file, instead of writing it."""

    def __init__(self):
        self.calls = []

    def __call__(self, profile_id, name, identity, phone_number="",
                 status="created"):
        self.calls.append(status)
        return None

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


def _args(**kw):
    return SimpleNamespace(screenshots=False, verify=True, country=None, **kw)


# --- the budget ------------------------------------------------------------

def test_verification_gets_what_is_left_of_the_phone_not_a_fixed_budget():
    """`run_verification` defaults to 900s. These phones die at about 780s, so
    the default would still be renting numbers after the device had gone."""
    left = signup_phone.seconds_left_for_verification(elapsed=300)

    assert left == signup_phone.PHONE_LIFE_SECONDS - 300
    assert left < verification.MAX_RUN_SECONDS


def test_a_phone_with_no_time_left_does_not_start_a_chain_at_all():
    """Starting one would rent a number and abandon it mid-chain. The account
    can be verified later; the number cannot be got back."""
    assert signup_phone.seconds_left_for_verification(
        elapsed=signup_phone.PHONE_LIFE_SECONDS - 1) is None


def test_the_floor_is_the_boundary_it_says_it_is():
    just_enough = signup_phone.PHONE_LIFE_SECONDS - signup_phone.MIN_VERIFY_SECONDS
    assert signup_phone.seconds_left_for_verification(just_enough) is not None
    assert signup_phone.seconds_left_for_verification(just_enough + 1) is None


# --- the hand-off ----------------------------------------------------------

def _run_verify(monkeypatch, verdict, recorded=None):
    recorded = recorded or _Recorded()
    seen = {}

    def fake_run_verification(driver, router, **kw):
        seen.update(kw)
        return verdict

    monkeypatch.setattr(signup_phone.verification, "run_verification",
                        fake_run_verification)
    monkeypatch.setattr(signup_phone, "AdbChallengeDriver",
                        lambda *a, **kw: object())
    monkeypatch.setattr(signup_phone, "record_account", recorded)
    monkeypatch.setattr("adb_bot.clients.sms.router.build_router",
                        lambda **kw: object())

    out = signup_phone.verify_account("id1", "Blank caio 2", SimpleNamespace(
        username="ida_kraus"), "host:1", object(), _args(), None, seconds=420)
    return out, seen, recorded


def test_the_remaining_time_is_handed_to_the_verification_run(monkeypatch):
    _, seen, _ = _run_verify(monkeypatch, verification.VerificationResult(
        status=verification.RESULT_SOLVED))

    assert seen["max_seconds"] == 420


def test_a_solved_checkpoint_is_recorded_as_a_usable_account(monkeypatch):
    """`created_unverified` is what an account nobody can log into looks like in
    the account file, and it is what every one of them has said so far."""
    out, _, recorded = _run_verify(monkeypatch, verification.VerificationResult(
        status=verification.RESULT_SOLVED))

    assert out["status"] == signup.RESULT_CREATED
    assert recorded.last == signup.RESULT_CREATED


def test_an_unsolved_checkpoint_keeps_the_account_and_names_the_reason(monkeypatch):
    """The account is real either way -- 48 identities have been recorded and
    three exist -- so the file must keep it, and say what stopped it rather than
    just that something did."""
    out, _, recorded = _run_verify(monkeypatch, verification.VerificationResult(
        status=verification.RESULT_NEEDS_HUMAN, detail="captcha"))

    assert out["status"] == signup.RESULT_CREATED_UNVERIFIED
    assert recorded.last == f"created_unverified-{verification.RESULT_NEEDS_HUMAN}"
    assert out["verification"] == verification.RESULT_NEEDS_HUMAN


def test_a_banned_account_is_not_reported_as_created(monkeypatch):
    """Instagram disabling the account seconds after making it is a real
    outcome, and calling it created would put it into the posting rotation."""
    out, _, _ = _run_verify(monkeypatch, verification.VerificationResult(
        status=verification.RESULT_BANNED))

    assert out["status"] != signup.RESULT_CREATED


def test_the_numbers_spent_are_reported(monkeypatch):
    """Each one is money, and a run that quietly burned three should say so."""
    out, _, _ = _run_verify(monkeypatch, verification.VerificationResult(
        status=verification.RESULT_NEEDS_HUMAN, numbers_used=3))

    assert out["numbers_used"] == 3


def test_the_verification_verdict_does_not_erase_how_the_run_got_here(monkeypatch):
    """`steps` carries the sign-in and both installs, and they are what say
    whether a phone was already prepared -- the difference between a six-minute
    run and a sixteen-minute one. Replacing the dict loses that."""
    out, _, _ = _run_verify(monkeypatch, verification.VerificationResult(
        status=verification.RESULT_SOLVED))

    assert "steps" not in out, (
        "verify_account must not own `steps`; run_phone merges the one key it "
        "contributes")
