"""What "can this account move to Geelark?" is allowed to mean.

The migration's whole state is written into each phone's Geelark `remark`,
because that field is the only per-phone note the platform gives us and it is
what a person sees when they open the phone. So these tests pin the *reading* of
that text -- and specifically the three distinctions that, if collapsed, would
each make the fleet look healthier or sicker than it is:

- **"wants a security code" is not "wrong password".** Instagram accepting the
  password and then challenging a new device means the credentials are proven
  good. Filing that under "cannot connect" would write off live accounts.
- **"the phone never booted" is not a verdict on the account.** It is an
  untested account, and counting it as a failure lets one bad afternoon of
  phone launches read as a judgement on the fleet. This is the same mistake the
  `Retries Exhausted` and `Human Verification Required` labels made on the
  MultiLogin side, where a counter and a screen-check got reported as diagnoses.
- **a new account is not a recovered one.** The signup flow tags a finished
  phone `IG connected` exactly like a migrated one, so reading the tag first
  would count accounts this fleet *created* as migration progress -- making a
  fleet that is losing accounts look like one holding steady.

The invariant at the bottom is the one that must never break: the buckets have
to partition the migration pool exactly, so the three headline numbers on the
tab always add up to the total beside them.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from adb_bot.automation import geelark_migration as gm


def _phone(name="Nikki 1", folder="Nikki", tags=(), remark=""):
    return {"name": name, "group": folder, "tags": list(tags),
            "remark": remark, "status": "stopped"}


class ParseRemark(unittest.TestCase):
    def test_reads_every_clause(self):
        parsed = gm.parse_remark(
            "IG:@nikki_lat | MAIL:a@b.com | PW:secret | MLX:Nikki 12 "
            "| LOGIN:OK-on-feed 2026-08-20")
        self.assertEqual(parsed["handle"], "nikki_lat")
        self.assertEqual(parsed["email"], "a@b.com")
        self.assertEqual(parsed["mlx_name"], "Nikki 12")
        self.assertEqual(parsed["login"], "OK-on-feed")
        self.assertEqual(parsed["login_day"], "2026-08-20")
        self.assertTrue(parsed["has_credentials"])

    def test_password_value_is_never_returned(self):
        """Presence is the fact that matters; the value must not travel.

        This text is rendered into a web page, and the password lives in a
        third-party metadata field that anyone with Geelark access can already
        read. There is no reason to widen that.
        """
        parsed = gm.parse_remark("IG:@x | PW:hunter2")
        self.assertNotIn("hunter2", json.dumps(parsed))
        self.assertTrue(parsed["has_credentials"])

    def test_password_may_contain_spaces_colons_and_slashes(self):
        """`|` is the separator precisely because passwords contain everything
        else. Splitting on ':' or ' ' would truncate them and, worse, would make
        a real password look absent -- turning a migratable phone into a
        "no credentials" one."""
        parsed = gm.parse_remark(
            "IG:@x | MAIL:a@b.com | PW:pa ss:w/ord | LOGIN:OK-on-feed")
        self.assertTrue(parsed["has_credentials"])
        self.assertEqual(parsed["login"], "OK-on-feed")

    def test_nocreds_marker(self):
        parsed = gm.parse_remark("NOCREDS | MLX:Jil 7 | ID:6285 | IG:@jil7")
        self.assertTrue(parsed["no_creds_marker"])
        self.assertFalse(parsed["has_credentials"])
        self.assertEqual(parsed["mlx_name"], "Jil 7")
        self.assertEqual(parsed["handle"], "jil7")

    def test_missing_day_is_tolerated(self):
        """The earliest outcomes were written before the date was added."""
        parsed = gm.parse_remark("IG:@x | PW:p | LOGIN:WRONG-PASSWORD")
        self.assertEqual(parsed["login"], "WRONG-PASSWORD")
        self.assertEqual(parsed["login_day"], "")

    def test_empty_remark_is_not_an_error(self):
        parsed = gm.parse_remark("")
        self.assertFalse(parsed["has_credentials"])
        self.assertEqual(parsed["handle"], "")


class Classify(unittest.TestCase):
    def _state(self, **kwargs):
        return gm.classify(_phone(**kwargs))["state"]

    def test_connected_tag_wins(self):
        self.assertEqual(
            self._state(tags=["IG connected"], remark="IG:@x | PW:p"),
            "connected")

    def test_stale_failure_does_not_beat_the_connected_tag(self):
        """A phone that reached the feed is connected even if an older failed
        attempt is still sitting in its remark."""
        self.assertEqual(
            self._state(tags=["IG connected"],
                        remark="IG:@x | PW:p | LOGIN:WRONG-PASSWORD 2026-08-19"),
            "connected")

    def test_security_code_is_not_a_failure(self):
        for label in ("OK-needs-email-code", "OK-needs-sms-code", "OK-needs-2fa"):
            with self.subTest(label=label):
                self.assertEqual(
                    self._state(remark=f"IG:@x | PW:p | LOGIN:{label} 2026-08-20"),
                    "needs_code")

    def test_account_side_failures_are_blocked(self):
        for label in ("WRONG-PASSWORD", "ACCOUNT-GONE", "HANDLE-NOT-FOUND",
                      "SUSPENDED"):
            with self.subTest(label=label):
                self.assertEqual(
                    self._state(remark=f"IG:@x | PW:p | LOGIN:{label} 2026-08-20"),
                    "blocked")

    def test_infrastructure_failures_are_untested_not_blocked(self):
        """The account was never actually tried. These must stay out of the
        "cannot connect" number or a flaky run becomes a verdict."""
        for label in ("PHONE-DID-NOT-BOOT", "INSTAGRAM-DID-NOT-OPEN",
                      "ADB-UNREACHABLE", "PHONE-NOT-READY", "UNKNOWN-SCREEN",
                      "STUCK"):
            with self.subTest(label=label):
                self.assertEqual(
                    self._state(remark=f"IG:@x | PW:p | LOGIN:{label} 2026-08-20"),
                    "retry")

    def test_unknown_label_falls_through_to_untested(self):
        """The vocabulary lives in the Geelark client and will grow. An
        unrecognised result must never be silently counted as good."""
        row = gm.classify(_phone(remark="IG:@x | PW:p | LOGIN:BRAND-NEW-THING"))
        self.assertEqual(row["state"], "retry")
        self.assertIn("BRAND-NEW-THING", row["why"])

    def test_code_wanted_but_mailbox_captchaed_reports_the_mailbox(self):
        """Both facts are true and the mailbox one is the actionable half: the
        account is fine and unreachable, which is a different errand from a bad
        password."""
        row = gm.classify(_phone(
            remark="IG:@x | PW:p | LOGIN:OK-needs-email-code 2026-08-20 "
                   "| MAILBOX:CAPTCHA 2026-08-20"))
        self.assertEqual(row["state"], "mailbox")
        self.assertIn("robot check", row["why"])

    def test_credentials_with_no_attempt_are_ready(self):
        self.assertEqual(self._state(remark="IG:@x | MAIL:a@b.com | PW:p"),
                         "ready")

    def test_no_password_is_no_credentials_even_with_a_handle(self):
        """A handle with no password is not a migratable account -- treating it
        as one is how a NOCREDS phone gets counted as ready."""
        self.assertEqual(self._state(remark="NOCREDS | MLX:Jil 7 | IG:@jil7"),
                         "no_credentials")
        self.assertEqual(self._state(remark="IG:@onlyhandle"), "no_credentials")

    def test_reserved_for_signup(self):
        self.assertEqual(self._state(tags=["new profile"], remark=""),
                         "new_account")

    def test_created_account_beats_the_connected_tag(self):
        """The signup flow writes BOTH `SIGNUP:created` and the connected tag.
        Reading the tag first would file a brand-new account as migration
        progress."""
        self.assertEqual(
            self._state(tags=["IG connected", "new profile"],
                        remark="IG:@fresh | PW:p | SIGNUP:created"),
            "signed_up")


class Summarise(unittest.TestCase):
    def _fleet(self):
        return [
            _phone("Nikki 1", "Nikki", ["IG connected"],
                   "IG:@a | PW:p | LOGIN:OK-on-feed 2026-08-20"),
            _phone("Nikki 2", "Nikki", [], "IG:@b | PW:p"),
            _phone("Nikki 3", "Nikki", [],
                   "IG:@c | PW:p | LOGIN:OK-needs-email-code 2026-08-20"),
            _phone("Nikki 4", "Nikki", [],
                   "IG:@d | PW:p | LOGIN:WRONG-PASSWORD 2026-08-20"),
            _phone("Nikki 5", "Nikki", [],
                   "IG:@e | PW:p | LOGIN:ADB-UNREACHABLE 2026-08-20"),
            _phone("Jil 1", "Jil", [], "NOCREDS | MLX:Jil 1 | IG:@f"),
            _phone("fresh", "Unassigned", ["new profile"], ""),
            _phone("made", "Unassigned", ["IG connected"],
                   "IG:@g | PW:p | SIGNUP:created"),
        ]

    def test_buckets_partition_the_migration_pool(self):
        """The invariant behind the tab's headline. If these three stop adding
        up to the total printed beside them, the page is lying."""
        out = gm.summarise(self._fleet())
        self.assertEqual(
            out["can_connect"] + out["cannot_connect"] + out["untested"],
            out["migration_total"])

    def test_new_accounts_are_outside_the_migration_total(self):
        out = gm.summarise(self._fleet())
        self.assertEqual(out["migration_total"], 6)   # 8 phones - 1 reserved - 1 made
        self.assertEqual(out["new_accounts_made"], 1)
        self.assertEqual(out["reserved_for_signup"], 1)

    def test_can_connect_counts_proven_and_untried_credentials(self):
        out = gm.summarise(self._fleet())
        # connected(1) + needs_code(1) + ready(1)
        self.assertEqual(out["can_connect"], 3)

    def test_cannot_connect_excludes_the_flaky_run(self):
        out = gm.summarise(self._fleet())
        # blocked(1) + no_credentials(1). The ADB-UNREACHABLE phone is untested.
        self.assertEqual(out["cannot_connect"], 2)
        self.assertEqual(out["untested"], 1)

    def test_folders_roll_up_per_model(self):
        out = gm.summarise(self._fleet())
        nikki = next(f for f in out["folders"] if f["folder"] == "Nikki")
        self.assertEqual(nikki["total"], 5)
        self.assertEqual(nikki["connected"], 1)
        self.assertEqual(nikki["blocked"], 1)

    def test_reasons_are_grouped_and_ordered_by_size(self):
        phones = [
            _phone(f"p{i}", "Nikki", [],
                   "IG:@x | PW:p | LOGIN:WRONG-PASSWORD 2026-08-20")
            for i in range(3)
        ] + [_phone("q", "Nikki", [],
                    "IG:@y | PW:p | LOGIN:ADB-UNREACHABLE 2026-08-20")]
        out = gm.summarise(phones)
        self.assertEqual(out["reasons"][0][1], 3)

    def test_no_password_survives_the_rollup(self):
        out = gm.summarise([_phone("x", "Nikki", [], "IG:@a | PW:hunter2")])
        self.assertNotIn("hunter2", json.dumps(out))

    def test_empty_account_does_not_divide_by_zero(self):
        out = gm.summarise([])
        self.assertEqual(out["migration_total"], 0)
        self.assertEqual(out["rows"], [])


class SignupProgress(unittest.TestCase):
    def test_missing_ledger_is_not_an_error(self):
        with TemporaryDirectory() as tmp:
            out = gm.signup_progress(Path(tmp) / "nope.jsonl")
        self.assertFalse(out["exists"])
        self.assertEqual(out["attempts"], 0)

    def test_counts_outcomes_and_keeps_recent_newest_first(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in [
                {"profile": "p1", "status": "created", "username": "a"},
                {"profile": "p2", "status": "stuck", "username": "b"},
                {"profile": "p3", "status": "created", "username": "c"},
            ]))
            out = gm.signup_progress(path)
        self.assertEqual(out["attempts"], 3)
        self.assertEqual(out["created"], 2)
        self.assertEqual(out["by_status"]["stuck"], 1)
        self.assertEqual(out["recent"][0]["profile"], "p3")

    def test_one_bad_line_does_not_cost_the_whole_ledger(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text('{"profile": "p1", "status": "created"}\n'
                            'not json at all\n'
                            '{"profile": "p2", "status": "created"}\n')
            out = gm.signup_progress(path)
        self.assertEqual(out["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
