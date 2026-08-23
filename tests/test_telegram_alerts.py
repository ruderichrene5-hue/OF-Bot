"""Narrowing which alerts reach Telegram.

One token feeds every alerting loop, so switching the token on switches all of
them on together. That is not hypothetical: re-enabling it on 2026-08-21, after
ten days commented out, silently re-armed the daily digest and the profile-flag
alerts as well as the proxy warning somebody actually asked for.
`ADBBOT_TELEGRAM_ALERTS` is the one place that decision gets made, so these are
the cases that keep it honest.
"""

import pytest

from adb_bot.clients.telegram import CATEGORIES, TelegramNotifier


def _notifier():
    return TelegramNotifier(token="123:AA", chat_id="-100123")


# --- the allowlist ---------------------------------------------------------

def test_an_unset_allowlist_allows_everything(monkeypatch):
    """A box that has never heard of categories must behave exactly as before.
    Narrowing is opt-in; a default of "nothing" would silence a fleet quietly."""
    monkeypatch.delenv("ADBBOT_TELEGRAM_ALERTS", raising=False)
    notifier = _notifier()

    assert all(notifier.allows(name) for name in CATEGORIES)


def test_only_the_listed_categories_are_allowed(monkeypatch):
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "minutes,proxy")
    notifier = _notifier()

    assert notifier.allows("minutes")
    assert notifier.allows("proxy")
    assert not notifier.allows("digest")
    assert not notifier.allows("flags")
    assert not notifier.allows("fleet")


def test_spaces_and_stray_commas_are_tolerated(monkeypatch):
    """This is typed into /etc/adbbot/env by hand at a moment when something is
    already on fire; it should not need to be typed carefully."""
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "  minutes ,, proxy , ")
    notifier = _notifier()

    assert notifier.allowed == {"minutes", "proxy"}


def test_an_uncategorised_message_is_always_allowed(monkeypatch):
    """A caller that predates categories keeps working rather than going quiet
    without anybody deciding it should."""
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "minutes")

    assert _notifier().allows("")


def test_the_allowlist_is_re_read_not_cached(monkeypatch):
    """Notifiers are built once per loop run. An edit to /etc/adbbot/env should
    take effect on the next tick, not the next deploy."""
    notifier = _notifier()
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "proxy")
    assert not notifier.allows("digest")

    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "proxy,digest")
    assert notifier.allows("digest")


# --- send() honours it -----------------------------------------------------

def test_a_suppressed_category_never_reaches_the_network(monkeypatch):
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "minutes")
    called = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("should not have been called")))

    assert _notifier().send("hi", category="digest") is False
    assert called == []


def test_an_allowed_category_still_sends(monkeypatch):
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "minutes")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: FakeResponse())

    assert _notifier().send("hi", category="minutes") is True


# --- what a person reads ---------------------------------------------------

def test_describe_names_the_narrowing(monkeypatch):
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "minutes,proxy")

    assert "sending only minutes, proxy" in _notifier().describe()


def test_describe_flags_a_typo_rather_than_silently_muting(monkeypatch):
    """A misspelt category silences a whole class of alert and raises no error,
    so the only defence is that it is visible where somebody looks."""
    monkeypatch.setenv("ADBBOT_TELEGRAM_ALERTS", "minutes,proxi")

    assert "unrecognised: proxi" in _notifier().describe()


# --- the loops agree with the spec -----------------------------------------

def test_every_guard_alert_maps_to_a_real_category():
    """A typo in the loop's own mapping would silence an alert on a box that
    narrowed the channel, and nothing else would notice."""
    # mlx-guard is not on this branch yet; the mapping it declares is still
    # part of this spec, so the check activates the moment the guard lands.
    mlx_guard = pytest.importorskip("adb_bot.automation.mlx_guard")

    assert set(mlx_guard.ALERT_CATEGORIES.values()) <= set(CATEGORIES)


def test_proxy_and_minutes_are_separate_categories():
    """Separate products with separate top-ups: somebody who wants to hear
    about one may not want to hear about the other."""
    # mlx-guard is not on this branch yet; the mapping it declares is still
    # part of this spec, so the check activates the moment the guard lands.
    mlx_guard = pytest.importorskip("adb_bot.automation.mlx_guard")

    assert mlx_guard.ALERT_CATEGORIES["proxy_out"] == "proxy"
    assert mlx_guard.ALERT_CATEGORIES["minutes_out"] == "minutes"
