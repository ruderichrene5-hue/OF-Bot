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

import logging
from types import SimpleNamespace
from unittest import mock

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


# --- the username carries the model's name (2026-08-23) --------------------

def test_the_username_starts_with_the_models_name():
    """The join key everywhere else in this codebase is the first word of
    the profile name (ONBOARDING_A_MODEL.md) -- "Cloe new 1" -> "Cloe" --
    reused here so the signup itself needs no extra model field threaded
    through from the caller."""
    out = signup_phone.run_phone(
        {"id": "1", "serial_name": "Cloe new 1"}, None,
        host=None, adb_client=None, args=_args(apply=False), logger=None)

    assert out["username"].lower().startswith("cloe")


def test_a_profile_with_no_serial_name_falls_back_to_organic():
    """No profile name means no model to derive -- the id alone (used as
    the fallback name) must never be typed in as a username stem."""
    out = signup_phone.run_phone(
        {"id": "633822713504334096"}, None,
        host=None, adb_client=None, args=_args(apply=False), logger=None)

    assert not out["username"].startswith("633822713504334096")


# --- a robot check leaves the phone open for a human (2026-08-24) ----------
#
# There is no automating past Google's recaptcha -- confirmed the same day by
# reading a live robot-check UI dump: the challenge lives inside Google's own
# account webview with no sitekey exposed to `uiautomator`, so a solved
# 2captcha token would have nowhere to be injected even if we built the
# endpoint. The only real lever left is getting a human onto the phone fast,
# which needs the phone (and its lock) left alone rather than torn down.

class _FakeHost:
    def __init__(self):
        self.shutdowns = []

    def launch(self, profile_id, logger):
        return "a-profile"

    def shutdown(self, profile_id, logger):
        self.shutdowns.append(profile_id)


def _wire_robot_check_run(monkeypatch, tmp_path, verdict, sent=None,
                          released=None):
    """Everything a `run_phone` call needs to reach the google_signin step,
    with mailbox_robot_check pointed at an isolated file so tests never
    touch the real ~/.adb_bot/mailbox_robot_check.json."""
    sent = sent if sent is not None else []
    released = released if released is not None else []
    monkeypatch.setattr(signup_phone.mailbox_robot_check, "STATE_PATH",
                        tmp_path / "mailbox_robot_check.json")
    monkeypatch.setattr(signup_phone.locks, "acquire", lambda *a, **kw: True)
    monkeypatch.setattr(signup_phone.locks, "release",
                        lambda name: released.append(name))
    monkeypatch.setattr(signup_phone, "record_account", lambda *a, **kw: None)
    monkeypatch.setattr(signup_phone, "connect_with_retries",
                        lambda *a, **kw: "target:1")
    monkeypatch.setattr(signup_phone, "AdbSignupDriver",
                        lambda *a, **kw: object())
    monkeypatch.setattr(signup_phone.google_signin, "sign_in_with_retries",
                        lambda *a, **kw: verdict)
    monkeypatch.setattr(signup_phone, "TelegramNotifier",
                        lambda *a, **kw: SimpleNamespace(
                            send=lambda text, **kw: sent.append(text)))
    return sent, released


def test_run_phone_uses_the_retrying_signin_not_the_single_shot_one(
        monkeypatch, tmp_path):
    """`elizabethclarkncv773@gmail.com`, 2026-08-25 (GeeLark/Android 16): a
    genuinely retryable `unknown_screen` ended the whole run, because this
    call site used `google_signin.sign_in` -- one attempt, no retries --
    instead of `sign_in_with_retries`, which force-stops Play Store/GMS and
    tries again up to `DEFAULT_SIGNIN_RETRIES` times and already existed,
    proven live for this exact shape of problem (`oukroaicha@gmail.com`,
    2026-08-23). The retry logic was real; this call site just never used
    it."""
    calls = []
    monkeypatch.setattr(signup_phone.mailbox_robot_check, "STATE_PATH",
                        tmp_path / "mailbox_robot_check.json")
    monkeypatch.setattr(signup_phone.locks, "acquire", lambda *a, **kw: True)
    monkeypatch.setattr(signup_phone.locks, "release", lambda name: None)
    monkeypatch.setattr(signup_phone, "record_account", lambda *a, **kw: None)
    monkeypatch.setattr(signup_phone, "connect_with_retries",
                        lambda *a, **kw: "target:1")
    monkeypatch.setattr(signup_phone, "AdbSignupDriver", lambda *a, **kw: object())
    monkeypatch.setattr(signup_phone.google_signin, "sign_in",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("the single-shot sign_in must not "
                                          "be called directly")))
    monkeypatch.setattr(
        signup_phone.google_signin, "sign_in_with_retries",
        lambda *a, **kw: calls.append((a, kw))
        or signup_phone.google_signin.RESULT_SIGNED_IN)
    monkeypatch.setattr(signup_phone, "TelegramNotifier",
                        lambda *a, **kw: SimpleNamespace(send=lambda *a, **kw: None))

    host = _FakeHost()
    box = {"address": "a@gmail.com", "password": "pw", "totp_secret": "s"}
    signup_phone.run_phone(
        {"id": "profile-1", "serial_name": "Cloe new 1"}, box,
        host=host, adb_client=object(), args=_args(apply=True),
        logger=logging.getLogger("test-signup-phone"))

    assert len(calls) == 1
    args, _kwargs = calls[0]
    assert "a@gmail.com" in args
    assert "pw" in args


def test_a_robot_check_leaves_the_phone_and_lock_alone(monkeypatch, tmp_path):
    sent, released = _wire_robot_check_run(
        monkeypatch, tmp_path, signup_phone.google_signin.RESULT_ROBOT_CHECK)

    host = _FakeHost()
    box = {"address": "a@gmail.com", "password": "pw", "totp_secret": "s"}
    out = signup_phone.run_phone(
        {"id": "profile-1", "serial_name": "Cloe new 1"}, box,
        host=host, adb_client=object(), args=_args(apply=True),
        logger=logging.getLogger("test-signup-phone"))

    assert out["status"] == f"mailbox-{signup_phone.google_signin.RESULT_ROBOT_CHECK}"
    assert out["keep_open"] is True
    assert host.shutdowns == [], "the phone must stay open for a human to clear it"
    assert released == [], "the lock must stay held so nobody else grabs this phone"
    assert sent and "robot check" in sent[0].lower()
    assert "a@gmail.com" in sent[0]


# --- a retry pins the same proxy, not a fresh one (2026-08-24) -------------

def test_a_second_robot_check_on_the_same_profile_is_allowed_to_retry(
        monkeypatch, tmp_path):
    """The very case this exists for: retrying on the profile it already
    failed on must never be refused by its own history."""
    sent, released = _wire_robot_check_run(
        monkeypatch, tmp_path, signup_phone.google_signin.RESULT_ROBOT_CHECK)
    host = _FakeHost()
    box = {"address": "a@gmail.com", "password": "pw", "totp_secret": "s"}
    item = {"id": "profile-1", "serial_name": "Cloe new 1"}

    first = signup_phone.run_phone(item, box, host=host, adb_client=object(),
                                   args=_args(apply=True),
                                   logger=logging.getLogger("test"))
    second = signup_phone.run_phone(item, box, host=host, adb_client=object(),
                                    args=_args(apply=True),
                                    logger=logging.getLogger("test"))

    assert first["status"] == second["status"] == "mailbox-google_robot_check"


def test_a_fresh_profile_is_refused_once_the_rotation_budget_is_spent(
        monkeypatch, tmp_path):
    sent, released = _wire_robot_check_run(
        monkeypatch, tmp_path, signup_phone.google_signin.RESULT_ROBOT_CHECK)
    box = {"address": "a@gmail.com", "password": "pw", "totp_secret": "s"}

    # profile-1 fails, then a rotation onto profile-2 is spent too.
    signup_phone.run_phone({"id": "profile-1", "serial_name": "p1"}, box,
                           host=_FakeHost(), adb_client=object(),
                           args=_args(apply=True), logger=logging.getLogger("t"))
    signup_phone.run_phone({"id": "profile-2", "serial_name": "p2"}, box,
                           host=_FakeHost(), adb_client=object(),
                           args=_args(apply=True), logger=logging.getLogger("t"))

    out = signup_phone.run_phone({"id": "profile-3", "serial_name": "p3"}, box,
                                 host=_FakeHost(), adb_client=object(),
                                 args=_args(apply=True), logger=logging.getLogger("t"))

    assert out["status"] == "mailbox-wrong-profile"
    assert out["pinned_profile"] == "profile-2"


def test_a_cooldown_refuses_before_touching_the_phone(monkeypatch, tmp_path):
    sent, released = _wire_robot_check_run(
        monkeypatch, tmp_path, signup_phone.google_signin.RESULT_ROBOT_CHECK)
    box = {"address": "a@gmail.com", "password": "pw", "totp_secret": "s"}
    item = {"id": "profile-1", "serial_name": "p1"}
    launched = []
    host = _FakeHost()
    monkeypatch.setattr(host, "launch",
                        lambda *a, **kw: launched.append(1) or "a-profile")

    for _ in range(signup_phone.mailbox_robot_check.MAX_ATTEMPTS_BEFORE_COOLDOWN):
        signup_phone.run_phone(item, box, host=host, adb_client=object(),
                               args=_args(apply=True), logger=logging.getLogger("t"))

    out = signup_phone.run_phone(item, box, host=host, adb_client=object(),
                                 args=_args(apply=True), logger=logging.getLogger("t"))

    assert out["status"] == "mailbox-cooldown"
    assert out["cooldown_until"] > 0
    assert len(launched) == signup_phone.mailbox_robot_check.MAX_ATTEMPTS_BEFORE_COOLDOWN, (
        "the cooldown run itself must never launch the phone")


def test_a_successful_signin_clears_the_pin_for_next_time(monkeypatch, tmp_path):
    sent, released = _wire_robot_check_run(
        monkeypatch, tmp_path, signup_phone.google_signin.RESULT_SIGNED_IN)
    box = {"address": "a@gmail.com", "password": "pw", "totp_secret": "s"}
    item = {"id": "profile-1", "serial_name": "p1"}

    signup_phone.run_phone(item, box, host=_FakeHost(), adb_client=object(),
                           args=_args(apply=True), logger=logging.getLogger("t"))

    assert signup_phone.mailbox_robot_check.pinned_profile(
        "a@gmail.com", tmp_path / "mailbox_robot_check.json") is None


# --- GeelarkHost leases its phone's proxy port (2026-08-23) -----------------
#
# Confirmed live: the batch pipeline was starting phones on their statically
# assigned proxy with no check that another phone was already running on the
# same port -- the four-modem pool cannot tell two devices apart on the same
# port at the same time. `proxy_pool` already solved this for the single-
# phone manual path (`adb_bot.clients.geelark.session`); these pin that
# `GeelarkHost` now goes through the same mechanism.

class _FakeLease:
    def __init__(self, port):
        self.port = port


class _FakePhoneClient:
    """Stands in for GeelarkPhoneClient in the auto-lookup path."""

    def __init__(self, rows):
        self._rows = rows

    def __call__(self, transport):
        return self

    def list_phones(self):
        return self._rows


def test_no_proxy_port_given_falls_back_to_an_auto_lookup(monkeypatch):
    """Nothing may construct a `GeelarkHost` that starts a phone without a
    lease -- a caller that does not already know the port (any future
    one-off script included) still gets it looked up and leased."""
    leased = []
    monkeypatch.setattr(
        "adb_bot.clients.geelark.phones.GeelarkPhoneClient",
        _FakePhoneClient([{"id": "profile-1", "proxy": {"port": 54018}}]))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.proxy_pool.acquire_proxy",
        lambda ports, **kw: leased.append(ports) or _FakeLease(ports[0]))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: "a-profile")

    host = signup_phone.GeelarkHost(transport=object(), args=None)
    result = host.launch("profile-1", logger=None)

    assert result == "a-profile"
    assert leased == [[54018]]


def test_the_proxys_own_username_is_leased_as_the_identity(monkeypatch):
    """Multilogin's mobile relay: every phone reports the identical
    `gate.multilogin.com:1080`, told apart only by which credential (a
    distinct `sid-` in the username) connects. Leasing by port alone
    serialised phones that were never sharing anything -- confirmed live
    2026-08-25, 4 freshly created phones, 3 of 4 refused to launch on a port
    none of them actually contended for. The auto-lookup must carry the
    username through so the lease is keyed on the resource that is actually
    exclusive."""
    leased_kwargs = []
    monkeypatch.setattr(
        "adb_bot.clients.geelark.phones.GeelarkPhoneClient",
        _FakePhoneClient([{"id": "profile-1",
                          "proxy": {"port": 1080, "username": "sid-aaa"}}]))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.proxy_pool.acquire_proxy",
        lambda ports, **kw: leased_kwargs.append(kw) or _FakeLease(ports[0]))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: "a-profile")

    host = signup_phone.GeelarkHost(transport=object(), args=None)
    host.launch("profile-1", logger=None)

    assert leased_kwargs[0]["identity"] == "sid-aaa"


def test_a_phone_the_lookup_cannot_find_launches_without_a_lease(monkeypatch):
    """No proxy on record means no port to collide on -- this must not block
    the launch, just skip leasing."""
    calls = []
    monkeypatch.setattr(
        "adb_bot.clients.geelark.phones.GeelarkPhoneClient",
        _FakePhoneClient([{"id": "some-other-profile",
                          "proxy": {"port": 54018}}]))
    monkeypatch.setattr("adb_bot.clients.geelark.proxy_pool.acquire_proxy",
                        lambda *a, **kw: calls.append((a, kw)) or _FakeLease(1))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: "a-profile")

    host = signup_phone.GeelarkHost(transport=object(), args=None)
    result = host.launch("profile-1", logger=None)

    assert result == "a-profile"
    assert calls == []


def test_a_proxy_port_is_leased_before_the_phone_launches(monkeypatch):
    leased = []
    monkeypatch.setattr(
        "adb_bot.clients.geelark.proxy_pool.acquire_proxy",
        lambda ports, **kw: leased.append(ports) or _FakeLease(ports[0]))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: "a-profile")

    host = signup_phone.GeelarkHost(transport=object(), args=None,
                                    proxy_port=54018)
    result = host.launch("profile-1", logger=None)

    assert result == "a-profile"
    assert leased == [[54018]]


def test_a_port_already_held_by_another_phone_refuses_to_launch(monkeypatch):
    """None means the lease is held elsewhere right now -- this phone must
    not start on the same port a second one is already using."""
    started = []
    monkeypatch.setattr("adb_bot.clients.geelark.proxy_pool.acquire_proxy",
                        lambda *a, **kw: None)
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: started.append(1) or "a-profile")

    host = signup_phone.GeelarkHost(transport=object(), args=None,
                                    proxy_port=54018)
    result = host.launch("profile-1", logger=mock.Mock())

    assert result is None
    assert started == []


def test_a_lease_is_not_stranded_when_the_phone_never_comes_up(monkeypatch):
    """The lease must be given back immediately if the launch it was taken
    for never actually happens -- otherwise a phone that fails to start
    parks a proxy nobody else can use for the whole TTL."""
    released = []
    monkeypatch.setattr("adb_bot.clients.geelark.proxy_pool.acquire_proxy",
                        lambda *a, **kw: _FakeLease(54018))
    monkeypatch.setattr("adb_bot.clients.geelark.proxy_pool.release_proxy",
                        lambda lease: released.append(lease.port))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: None)  # phone never becomes ADB-ready

    host = signup_phone.GeelarkHost(transport=object(), args=None,
                                    proxy_port=54018)
    result = host.launch("profile-1", logger=None)

    assert result is None
    assert released == [54018]
    assert host._lease is None


def test_shutdown_releases_the_held_lease(monkeypatch):
    released = []
    monkeypatch.setattr("adb_bot.clients.geelark.proxy_pool.acquire_proxy",
                        lambda *a, **kw: _FakeLease(54018))
    monkeypatch.setattr("adb_bot.clients.geelark.proxy_pool.release_proxy",
                        lambda lease: released.append(lease.port))
    monkeypatch.setattr(
        "adb_bot.clients.geelark.prepare_geelark_profile_for_adb",
        lambda *a, **kw: "a-profile")
    monkeypatch.setattr("adb_bot.clients.geelark.release_geelark_phone",
                        lambda *a, **kw: True)

    host = signup_phone.GeelarkHost(transport=object(), args=None,
                                    proxy_port=54018)
    host.launch("profile-1", logger=None)
    host.shutdown("profile-1", logger=None)

    assert released == [54018]


def test_shutdown_without_a_held_lease_does_not_crash(monkeypatch):
    monkeypatch.setattr("adb_bot.clients.geelark.release_geelark_phone",
                        lambda *a, **kw: True)

    host = signup_phone.GeelarkHost(transport=object(), args=None)
    host.shutdown("profile-1", logger=None)  # must not raise
