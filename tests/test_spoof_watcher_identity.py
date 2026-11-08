"""Phone-side watcher vs. server-side iphone_farm_spoof: identity parity.

The watcher derives IDFV/IDFA/UDID/serial/IMEI locally on the phone. If any of
those drift from what `iphone_farm_spoof.identity_for` produces, the spoof plist
written by a manual Crane switch would carry a different device than the one the
server-side `apply()` would have written for the same container -- which is
exactly the bug the watcher is meant to prevent.

This test imports both implementations in-process and asserts that for a spread
of container names (bare letters, letter+digit, multi-word with space, repeats)
the derived identity dicts are byte-for-byte identical. Salt sourcing: the
watcher reads /var/jb/etc/rnxspoof.salt on the phone; here we monkey-patch its
_salt() to return the same value as the server-side _salt(), so we are testing
the derivation math, not file plumbing.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHER_PATH = REPO_ROOT / "iphone_farm" / "spoof" / "rnxspoof_watcher.py"


def _load_watcher_module():
    spec = importlib.util.spec_from_file_location("rnxspoof_watcher", WATCHER_PATH)
    assert spec and spec.loader, "watcher module cannot be loaded from disk"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rnxspoof_watcher"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pair():
    server = importlib.import_module("adb_bot.automation.iphone_farm_spoof")
    watcher = _load_watcher_module()

    # Force both to use the same deterministic test salt. We clear both caches,
    # then stub the salt readers.
    test_salt = "fixed-salt-for-parity-test-abc123"
    watcher._SALT_CACHE.clear()
    watcher._SALT_CACHE.append(test_salt)
    server_salt_orig = server._salt
    server._salt = lambda: test_salt
    try:
        yield server, watcher
    finally:
        server._salt = server_salt_orig
        watcher._SALT_CACHE.clear()


CONTAINERS = [
    "H4",
    "G3",
    "B7",
    "Nikki 3",
    "Nikki 23",
    "Jil 2",
    "Laura 10",
    "LAURA 10",       # case sensitivity matters: should produce a different identity
    "A6",
    "",               # empty-string edge case: must not crash and must agree
]


@pytest.mark.parametrize("container", CONTAINERS)
def test_identity_matches_server(pair, container):
    server, watcher = pair
    s = server.identity_for(container)
    w = watcher.identity_for(container)
    assert s == w, (
        f"watcher drifted from server for container {container!r}:\n"
        f"  server:  {s}\n"
        f"  watcher: {w}"
    )


def test_salt_is_actually_used(pair):
    """Sanity: changing the salt changes the identity, so the test above would catch
    a bug where either side silently used a different salt."""
    _server, watcher = pair
    before = watcher.identity_for("H4")
    watcher._SALT_CACHE.clear()
    watcher._SALT_CACHE.append("different-salt")
    try:
        after = watcher.identity_for("H4")
    finally:
        watcher._SALT_CACHE.clear()
        watcher._SALT_CACHE.append("fixed-salt-for-parity-test-abc123")
    assert before != after, "salt change did not alter identity -- salt wiring is wrong"


def test_strip_ig_suffix_matches_bare_name(pair):
    """Container names in the Crane plist are 'H4 IG'; the identity must key on 'H4'."""
    server, watcher = pair
    stripped = watcher._strip_ig_suffix("H4 IG")
    assert stripped == "H4"
    assert watcher.identity_for(stripped) == server.identity_for("H4")

    # Trailing whitespace tolerance.
    assert watcher._strip_ig_suffix("Nikki 3 IG  ") == "Nikki 3"
    assert watcher._strip_ig_suffix("B7") == "B7"               # no suffix: unchanged
