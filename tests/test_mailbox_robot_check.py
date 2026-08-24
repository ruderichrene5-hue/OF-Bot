"""A retry must pin the same proxy instead of rotating onto a fresh one.

2026-08-24: the same four test mailboxes hit google_robot_check on every
platform tried that day, one fresh profile (and proxy) per round -- Google's
own risk scoring reads a rotating set of IPs on one account as suspicious on
its own. This module is the guard that stops a retry from doing that again.
"""

from pathlib import Path

from adb_bot.automation import mailbox_robot_check as m


def _path(tmp_path) -> Path:
    return tmp_path / "mailbox_robot_check.json"


def _clock(seconds=[1_000_000.0]):
    def tick():
        return seconds[0]
    tick.advance = lambda n: seconds.__setitem__(0, seconds[0] + n)
    return tick


def test_a_first_attempt_pins_no_profile_yet(tmp_path):
    path = _path(tmp_path)

    assert m.pinned_profile("a@gmail.com", path) is None
    assert m.may_use_profile("a@gmail.com", "profile-1", path) is True


def test_the_first_robot_check_pins_that_profile(tmp_path):
    path = _path(tmp_path)

    m.record_robot_check("a@gmail.com", "profile-1", path)

    assert m.pinned_profile("a@gmail.com", path) == "profile-1"
    assert m.may_use_profile("a@gmail.com", "profile-1", path) is True


def test_a_different_profile_is_refused_until_the_rotation_budget_is_spent(tmp_path):
    path = _path(tmp_path)
    m.record_robot_check("a@gmail.com", "profile-1", path)

    assert m.may_use_profile("a@gmail.com", "profile-2", path) is True, (
        "one rotation is allowed")
    m.record_robot_check("a@gmail.com", "profile-2", path)
    assert m.may_use_profile("a@gmail.com", "profile-3", path) is False, (
        "a second rotation must be refused")
    assert m.may_use_profile("a@gmail.com", "profile-2", path) is True, (
        "the profile it just rotated onto stays usable")


def test_five_attempts_on_the_same_profile_start_a_six_hour_cooldown(tmp_path):
    path = _path(tmp_path)
    clock = _clock()

    for _ in range(5):
        m.record_robot_check("a@gmail.com", "profile-1", path, clock=clock)

    ready = m.ready_at("a@gmail.com", path, clock=clock)
    assert ready > clock()
    assert ready - clock() == m.COOLDOWN_SECONDS


def test_landing_on_a_different_profile_restarts_the_attempt_count(tmp_path):
    """A fresh proxy's first try is not the fifth failure of a streak that
    happened on a different one -- four fails on profile-1, then a rotation
    onto profile-2 must not immediately trip the cooldown."""
    path = _path(tmp_path)
    clock = _clock()

    for _ in range(4):
        m.record_robot_check("a@gmail.com", "profile-1", path, clock=clock)
    m.record_robot_check("a@gmail.com", "profile-2", path, clock=clock)

    assert m.ready_at("a@gmail.com", path, clock=clock) == 0.0


def test_cooldown_expires_on_its_own(tmp_path):
    path = _path(tmp_path)
    clock = _clock()
    for _ in range(5):
        m.record_robot_check("a@gmail.com", "profile-1", path, clock=clock)
    assert m.ready_at("a@gmail.com", path, clock=clock) > 0.0

    clock.advance(m.COOLDOWN_SECONDS + 1)

    assert m.ready_at("a@gmail.com", path, clock=clock) == 0.0


def test_a_successful_signin_clears_the_streak(tmp_path):
    path = _path(tmp_path)
    m.record_robot_check("a@gmail.com", "profile-1", path)

    m.clear("a@gmail.com", path)

    assert m.pinned_profile("a@gmail.com", path) is None
    assert m.may_use_profile("a@gmail.com", "profile-9", path) is True


def test_clearing_an_address_with_no_history_does_not_raise(tmp_path):
    m.clear("never-seen@gmail.com", _path(tmp_path))  # must not raise


def test_addresses_do_not_interfere_with_each_other(tmp_path):
    path = _path(tmp_path)
    m.record_robot_check("a@gmail.com", "profile-1", path)

    assert m.pinned_profile("b@gmail.com", path) is None
    assert m.may_use_profile("b@gmail.com", "profile-9", path) is True
