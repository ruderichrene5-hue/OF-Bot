"""`ADBClient` remembering a profile's glogin password so a session that
silently drops mid-run can be re-authenticated, rather than every caller
reading empty screens forever.

Confirmed live 2026-08-25: a 22-minute Gmail-install retry loop burned its
entire budget this way -- glogin's session expired partway through, nothing
detected it, and `play_install` just saw "nothing to tap" on every poll.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from adb_bot.clients.adb import ADBClient, GLOGIN_REQUIRED_MARKER


def _profile(target="host:1", pwd="s3cret"):
    return SimpleNamespace(id="p1", target=target, pwd=pwd)


class AuthenticateRemembersPasswordTest(TestCase):
    def test_a_successful_authenticate_remembers_the_password(self):
        client = ADBClient()
        with patch.object(client, "_run_capture", return_value=(0, "glogin success", "")):
            self.assertTrue(client.authenticate(_profile()))
        self.assertEqual(client._pwds["host:1"], "s3cret")

    def test_a_silent_success_still_remembers_the_password(self):
        """Some glogin builds print nothing at all on success."""
        client = ADBClient()
        with patch.object(client, "_run_capture", return_value=(0, "", "")):
            self.assertTrue(client.authenticate(_profile()))
        self.assertEqual(client._pwds["host:1"], "s3cret")

    def test_a_failed_authenticate_does_not_remember_anything(self):
        client = ADBClient()
        with patch.object(client, "_run_capture",
                          return_value=(1, "", "error: denied")):
            self.assertFalse(client.authenticate(_profile()))
        self.assertNotIn("host:1", client._pwds)


class ReauthenticateTest(TestCase):
    def test_it_replays_glogin_with_the_remembered_password(self):
        client = ADBClient()
        client._pwds["host:1"] = "s3cret"
        calls = []
        with patch.object(client, "_run_capture",
                          side_effect=lambda cmd: (calls.append(cmd), (0, "ok", ""))[1]):
            self.assertTrue(client.reauthenticate("host:1"))
        self.assertEqual(calls, ["adb -s host:1 shell glogin s3cret"])

    def test_no_remembered_password_refuses_rather_than_guessing(self):
        client = ADBClient()
        with patch.object(client, "_run_capture") as capture:
            self.assertFalse(client.reauthenticate("host:1"))
        capture.assert_not_called()

    def test_a_failed_reauth_reports_false(self):
        client = ADBClient()
        client._pwds["host:1"] = "s3cret"
        with patch.object(client, "_run_capture",
                          return_value=(1, "", "error: denied")):
            self.assertFalse(client.reauthenticate("host:1"))


def test_the_marker_matches_the_real_device_text():
    """Pinned against the literal text seen live, lowercased and stripped --
    a regression here means the detector stops firing on the real thing."""
    real = "error: you should run glogin to login first"
    assert GLOGIN_REQUIRED_MARKER in real.lower()
