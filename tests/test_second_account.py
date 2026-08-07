"""Two Instagram accounts on one phone, end to end.

Some phones carry a second Instagram account in the same cloned app, reachable
through Instagram's own account switcher. It is a full posting target: its own
spoofed videos, its own scheduled slots, its own post, its own verification.

The failure this guards against is not "the second account does not post". It is
the quieter one: the second account posting the *first* account's video, or the
first account's row posting on whichever account the last run happened to leave
in front. Both look like success everywhere the bot reports.
"""

import logging
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from adb_bot.automation import report, spoof_pipeline
from adb_bot.automation.posting_planner import plan_posting_queue
from adb_bot.automation.queue_runner import run_queue_slots
from adb_bot.automation.spoof_pipeline import RawVideo, _seed_for, run_pipeline
from adb_bot.clients import airtable as at
from adb_bot.clients.airtable import AirtableClient

from test_queue_runner import FakeQueueClient, _variant
from test_spoof_pipeline import FakePipelineClient, FakeSource

LOG = logging.getLogger("test")
BERLIN = ZoneInfo("Europe/Berlin")


def _profile_row(rec_id, name, api_id="111", has_second=False,
                 primary=None, second=None, status=None):
    fields = {"Profile Name": name, "MLX API ID": api_id}
    if status:
        fields[at.F_PROF_STATUS] = status
    if has_second:
        fields[at.F_PROF_HAS_SECOND] = True
    if primary:
        fields[at.F_PROF_PRIMARY_HANDLE] = primary
    if second:
        fields[at.F_PROF_SECOND_HANDLE] = second
    return {"id": rec_id, "fields": fields}


def _targets(rows, **kwargs):
    client = AirtableClient("tok", "app123", "Profiles")
    with patch.object(AirtableClient, "_list_table", return_value=rows):
        return client.profile_targets_by_model(**kwargs)


class ProfileTargetsTest(TestCase):
    """Where a second account becomes a target of its own."""

    def test_a_two_account_phone_is_two_targets(self):
        targets = _targets([_profile_row("p1", "Jil 5", has_second=True,
                                         primary="helenaiscutee", second="jiji.ll12")])["jil"]
        self.assertEqual(len(targets), 2)
        by_slot = {t["slot"]: t for t in targets}
        # Same phone, same launch key -- only the account differs.
        self.assertEqual({t["profile_id"] for t in targets}, {"p1"})
        self.assertEqual({t["launch_id"] for t in targets}, {"111"})
        self.assertEqual(by_slot["Primary"]["ig_handle"], "helenaiscutee")
        self.assertEqual(by_slot["Second"]["ig_handle"], "jiji.ll12")
        # The second target's handle is the IG handle, not the profile name:
        # `handle` names variant files and queue rows, and two targets called
        # "Jil 5" would overwrite each other's video.
        self.assertEqual(by_slot["Second"]["handle"], "jiji.ll12")

    def test_an_ordinary_phone_is_untouched(self):
        targets = _targets([_profile_row("p1", "Jil 5")])["jil"]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["slot"], "Primary")
        # No handle: the flow posts as whoever is signed in, exactly as before.
        self.assertIsNone(targets[0]["ig_handle"])

    def test_a_missing_handle_means_one_account_not_two(self):
        """The flow must be able to name the account it switches BACK to.

        With only the second handle known, one Second post would leave the phone
        on the second account and every later Primary post would go out there
        too -- silently, since both rows report success.
        """
        for primary, second in ((None, "jiji.ll12"), ("helenaiscutee", None), (None, None)):
            with self.subTest(primary=primary, second=second):
                targets = _targets([_profile_row("p1", "Jil 5", has_second=True,
                                                 primary=primary, second=second)])["jil"]
                self.assertEqual([t["slot"] for t in targets], ["Primary"])
                self.assertIsNone(targets[0]["ig_handle"])

    def test_the_at_sign_people_type_is_stripped(self):
        targets = _targets([_profile_row("p1", "Jil 5", has_second=True,
                                         primary="@helenaiscutee", second="@jiji.ll12")])["jil"]
        self.assertEqual({t["ig_handle"] for t in targets}, {"helenaiscutee", "jiji.ll12"})

    def test_a_base_without_the_fields_still_returns_targets(self):
        """Airtable 422s the whole request over one unknown column."""
        calls = []

        def fake_list(self, table, fields=None, **kwargs):
            calls.append(fields)
            if fields and at.F_PROF_HAS_SECOND in fields:
                raise RuntimeError("422 UNKNOWN_FIELD_NAME")
            return [_profile_row("p1", "Jil 5")]

        client = AirtableClient("tok", "app123", "Profiles")
        with patch.object(AirtableClient, "_list_table", fake_list):
            targets = client.profile_targets_by_model()
        self.assertEqual([t["handle"] for t in targets["jil"]], ["Jil 5"])
        self.assertEqual(len(calls), 2)


class QueueSlotsTest(TestCase):
    """Slot creation: two accounts on one phone are two independent schedules."""

    PROFILES = {"jil": [
        {"profile_id": "p1", "handle": "Jil 5", "profile_name": "Jil 5",
         "launch_id": "111", "slot": "Primary", "ig_handle": "helenaiscutee"},
        {"profile_id": "p1", "handle": "jiji.ll12", "profile_name": "Jil 5",
         "launch_id": "111", "slot": "Second", "ig_handle": "jiji.ll12"},
    ]}

    def _client(self, variants, queue_rows=None):
        return FakeQueueClient(profiles=self.PROFILES, variants=variants,
                               queue_rows=queue_rows or [])

    def _sv(self, vid, slot):
        variant = _variant(vid, profile_id="p1")
        variant["slot"] = slot
        return variant

    def test_each_account_gets_its_own_row_for_the_same_slot(self):
        client = self._client([self._sv("v1", "Primary"), self._sv("v2", "Second")])
        report_ = run_queue_slots(client, LOG, slot_times=("09:00",),
                                  now=datetime(2026, 8, 3, 13, 0, tzinfo=BERLIN),
                                  dry_run=False, use_model_times=False)

        self.assertEqual(report_.rows_created, 2)
        rows = {row["account_slot"]: row for row in client.created}
        self.assertEqual(sorted(rows), ["Primary", "Second"])
        self.assertEqual(rows["Primary"]["target_handle"], "helenaiscutee")
        self.assertEqual(rows["Second"]["target_handle"], "jiji.ll12")
        # Both rows point at the same phone...
        self.assertEqual({r["profile_id"] for r in client.created}, {"p1"})
        # ...and each carries the video spoofed for its own account.
        self.assertEqual(rows["Primary"]["variant_id"], "v1")
        self.assertEqual(rows["Second"]["variant_id"], "v2")
        # The row name still says which phone it is on: the report reads a
        # model off its first word, and "jiji.ll12 / 09:00" has no model in it.
        self.assertEqual(rows["Primary"]["name"], "Jil 5 / 09:00")
        self.assertEqual(rows["Second"]["name"], "Jil 5 (jiji.ll12) / 09:00")

    def test_one_accounts_video_is_never_handed_to_the_other(self):
        """The pools are separate. Without the slot on the variant, the primary
        account drains the pile the second account's clips are sitting in --
        and then posts them, on the wrong account."""
        client = self._client([self._sv("v1", "Second"), self._sv("v2", "Second")])
        report_ = run_queue_slots(client, LOG, slot_times=("09:00",),
                                  now=datetime(2026, 8, 3, 13, 0, tzinfo=BERLIN),
                                  dry_run=False, use_model_times=False)

        self.assertEqual([r["account_slot"] for r in client.created], ["Second"])
        skipped = dict(report_.skipped)
        self.assertIn("Jil 5", skipped)
        self.assertIn("no unused Ready Spoof Variant", skipped["Jil 5"])

    def test_the_primary_rows_do_not_close_the_second_accounts_slots(self):
        """A phone that already posted on its first account still owes its
        second one a post at the same time."""
        existing = {"id": "q1", "fields": {
            at.F_PQ_POST_STATUS: at.POST_STATUS_POSTED,
            at.F_PQ_SCHEDULED: "2026-08-03T07:00:00.000Z",
            at.F_PQ_NAME: "Jil 5 / 09:00",
            at.F_PQ_TARGET_PROFILE: ["p1"],
            at.F_PQ_ACCOUNT_SLOT: "Primary",
        }, "createdTime": "2026-08-03T07:00:00.000Z"}
        client = self._client([self._sv("v2", "Second")], queue_rows=[existing])
        run_queue_slots(client, LOG, slot_times=("09:00",),
                        now=datetime(2026, 8, 3, 13, 0, tzinfo=BERLIN),
                        dry_run=False, use_model_times=False)

        self.assertEqual([r["account_slot"] for r in client.created], ["Second"])

    def test_a_second_accounts_row_does_not_refill_its_own_slot(self):
        """The guard still holds within an account -- one row per slot, per
        account, per day."""
        existing = {"id": "q1", "fields": {
            at.F_PQ_POST_STATUS: at.POST_STATUS_POSTED,
            at.F_PQ_SCHEDULED: "2026-08-03T07:00:00.000Z",
            at.F_PQ_NAME: "jiji.ll12 / 09:00",
            at.F_PQ_TARGET_PROFILE: ["p1"],
            at.F_PQ_ACCOUNT_SLOT: "Second",
        }, "createdTime": "2026-08-03T07:00:00.000Z"}
        client = self._client([self._sv("v2", "Second")], queue_rows=[existing])
        run_queue_slots(client, LOG, slot_times=("09:00",),
                        now=datetime(2026, 8, 3, 13, 0, tzinfo=BERLIN),
                        dry_run=False, use_model_times=False)

        self.assertEqual(client.created, [])

    def test_an_old_row_without_a_slot_still_guards_the_primary(self):
        """Every row written before two-account phones existed belongs to the
        account the phone signs in as."""
        existing = {"id": "q1", "fields": {
            at.F_PQ_POST_STATUS: at.POST_STATUS_POSTED,
            at.F_PQ_SCHEDULED: "2026-08-03T07:00:00.000Z",
            at.F_PQ_NAME: "Jil 5 / 09:00",
            at.F_PQ_TARGET_PROFILE: ["p1"],
        }, "createdTime": "2026-08-03T07:00:00.000Z"}
        client = self._client([self._sv("v1", "Primary")], queue_rows=[existing])
        run_queue_slots(client, LOG, slot_times=("09:00",),
                        now=datetime(2026, 8, 3, 13, 0, tzinfo=BERLIN),
                        dry_run=False, use_model_times=False)

        self.assertEqual(client.created, [])

    def test_a_single_account_phone_writes_no_handle(self):
        client = FakeQueueClient(
            profiles={"jil": [{"profile_id": "p1", "handle": "Jil 5", "launch_id": "111"}]},
            variants=[_variant("v1", profile_id="p1")])
        run_queue_slots(client, LOG, slot_times=("09:00",),
                        now=datetime(2026, 8, 3, 13, 0, tzinfo=BERLIN),
                        dry_run=False, use_model_times=False)

        self.assertEqual(len(client.created), 1)
        self.assertIsNone(client.created[0]["target_handle"])


class SpoofPipelineTest(TestCase):
    """Each account gets its OWN encode. Sharing one is duplicate content."""

    PROFILES = {"nikki": [
        {"profile_id": "p1", "handle": "Nikki 5", "launch_id": "111",
         "slot": "Primary", "ig_handle": "nikki.kie20"},
        {"profile_id": "p1", "handle": "nikkiisthierr", "launch_id": "111",
         "slot": "Second", "ig_handle": "nikkiisthierr"},
    ]}

    def _run(self, out_root):
        client = FakePipelineClient(profiles=self.PROFILES, model_ids={"nikki": "recM1"})
        source = FakeSource({"Nikki": [RawVideo(model="Nikki", name="clip1.mp4",
                                                path="/raw/Nikki/clip1.mp4")]})
        produced = []

        def spoof_fn(raw_path, out_dir, seed, logger=None):
            path = Path(out_dir) / "clip1.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Seeded output, so two accounts sharing a seed would be visible as
            # two identical files rather than passing silently.
            path.write_text(f"encoded-{seed}")
            produced.append(seed)
            return path

        result = run_pipeline(client, LOG, raw_root=None, out_root=out_root,
                              spoof_fn=spoof_fn, source=source, dry_run=False,
                              targets=spoof_pipeline.TARGETS_PROFILES)
        return client, result, produced

    def test_one_raw_video_becomes_two_different_variants(self):
        import tempfile
        with tempfile.TemporaryDirectory() as out_root:
            client, result, seeds = self._run(out_root)

            self.assertEqual(result.variants_created, 2)
            # Different seeds -> genuinely different encodes, not one file twice.
            self.assertEqual(len(set(seeds)), 2)
            files = [row[3] for row in client.variant_slots]
            self.assertEqual(len(set(files)), 2)
            self.assertEqual({Path(f).read_text() for f in files},
                             {f"encoded-{seed}" for seed in seeds})

    def test_each_variant_says_which_account_it_is_for(self):
        import tempfile
        with tempfile.TemporaryDirectory() as out_root:
            client, _result, _seeds = self._run(out_root)

            by_slot = {row[2]: row for row in client.variant_slots}
            self.assertEqual(sorted(by_slot), ["Primary", "Second"])
            self.assertEqual(by_slot["Primary"][1], "nikki.kie20")
            self.assertEqual(by_slot["Second"][1], "nikkiisthierr")
            # Both link the same phone: one Profiles row, two accounts.
            self.assertEqual({row[0] for row in client.variant_slots}, {"p1"})

    def test_the_seed_differs_per_account(self):
        self.assertNotEqual(_seed_for("clip1.mp4", "Nikki 5"),
                            _seed_for("clip1.mp4", "nikkiisthierr"))


class PostingPlannerTest(TestCase):
    """The row tells the flow which account to post as."""

    PROFILES = {"recProf1": {"launch_id": "624354174112432228", "name": "Jil 5"}}
    VARIANTS = {"recVar1": {"file_path": "/spoofed/v1.mp4", "status": "Ready"}}

    def _row(self, handle=None, slot=None):
        fields = {
            at.F_PQ_NAME: "jiji.ll12 / 09:00",
            at.F_PQ_POST_STATUS: at.POST_STATUS_PENDING,
            at.F_PQ_SCHEDULED: "2026-08-03T07:00:00.000Z",
            at.F_PQ_TARGET_PROFILE: ["recProf1"],
            at.F_PQ_SPOOF_VARIANT: ["recVar1"],
        }
        if handle:
            fields[at.F_PQ_TARGET_HANDLE] = handle
        if slot:
            fields[at.F_PQ_ACCOUNT_SLOT] = slot
        return {"id": "recQ1", "fields": fields}

    def _plan(self, row):
        return plan_posting_queue([row], {}, self.PROFILES, self.VARIANTS, {},
                                  now=datetime(2026, 8, 3, 12, 0))

    def test_the_handle_reaches_the_flow(self):
        item = self._plan(self._row("jiji.ll12", "Second")).to_post[0]
        self.assertEqual(item.target_handle, "jiji.ll12")
        self.assertEqual(item.account_slot, "Second")
        # And the run is reported under the account, not under the phone --
        # otherwise both of a phone's posts are logged as "Jil 5".
        self.assertEqual(item.account_name, "jiji.ll12")

    def test_a_row_without_a_handle_posts_as_whoever_is_signed_in(self):
        item = self._plan(self._row()).to_post[0]
        self.assertIsNone(item.target_handle)
        self.assertEqual(item.account_name, "Jil 5")

    def test_a_typed_at_sign_does_not_reach_the_switcher(self):
        item = self._plan(self._row("@jiji.ll12", "Second")).to_post[0]
        self.assertEqual(item.target_handle, "jiji.ll12")


class FakeNode:
    def __init__(self, text=None, exists=True, on_click=None):
        self.info = {"text": text}
        self.exists = exists
        self._on_click = on_click

    def click(self):
        if self._on_click:
            self._on_click()


class FakeDevice:
    """Just enough uiautomator2 to drive the account switcher.

    `handle` is what the profile header shows; clicking a switcher row changes
    it, which is exactly the state the real flow is reading.
    """

    def __init__(self, handle="helenaiscutee", listed=("helenaiscutee", "jiji.ll12"),
                 header_readable=True):
        self.handle = handle
        self.listed = list(listed)
        self.header_readable = header_readable
        self.switcher_open = False
        self.tabs_opened = 0

    def __call__(self, **kwargs):
        rid = kwargs.get("resourceId") or ""
        if "profile_tab" in rid or "profile_tab" in (kwargs.get("resourceIdMatches") or ""):
            self.tabs_opened += 1
            return FakeNode(exists=True)
        if "action_bar" in rid or "action_bar" in (kwargs.get("resourceIdMatches") or ""):
            if not self.header_readable:
                return FakeNode(text="", exists=True)
            return FakeNode(text=self.handle, exists=True,
                            on_click=lambda: setattr(self, "switcher_open", True))
        pattern = kwargs.get("textMatches")
        if pattern and self.switcher_open:
            import re
            match = next((h for h in self.listed if re.match(pattern, h)), None)
            if match is None:
                return FakeNode(exists=False)
            return FakeNode(text=match, exists=True,
                            on_click=lambda: self._switch_to(match))
        return FakeNode(exists=False)

    def _switch_to(self, handle):
        self.handle = handle
        self.switcher_open = False


class AccountSwitchTest(TestCase):
    """The device half: prove which account is in front before posting."""

    def setUp(self):
        from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow
        self.flow = InstagramReelUploadU2Flow()
        self.emitted = []

    def _emit(self, level, message, *args):
        self.emitted.append(message % args if args else message)

    def _ensure(self, device, want):
        return self.flow._ensure_account_u2(device, "1.2.3.4:5555", want, self._emit)

    def test_no_handle_wanted_does_nothing_at_all(self):
        device = FakeDevice()
        self.assertTrue(self._ensure(device, None))
        self.assertEqual(device.tabs_opened, 0)

    def test_already_on_the_right_account_does_not_switch(self):
        device = FakeDevice(handle="helenaiscutee")
        self.assertTrue(self._ensure(device, "helenaiscutee"))
        self.assertFalse(device.switcher_open)
        self.assertEqual(device.handle, "helenaiscutee")

    def test_it_switches_to_the_second_account(self):
        device = FakeDevice(handle="helenaiscutee")
        self.assertTrue(self._ensure(device, "jiji.ll12"))
        self.assertEqual(device.handle, "jiji.ll12")

    def test_it_switches_back_to_the_first(self):
        """The state is sticky: whoever posted last left the phone on their
        account, so the next post has to put it back."""
        device = FakeDevice(handle="jiji.ll12")
        self.assertTrue(self._ensure(device, "helenaiscutee"))
        self.assertEqual(device.handle, "helenaiscutee")

    def test_an_account_the_phone_does_not_have_is_a_refusal(self):
        device = FakeDevice(handle="helenaiscutee", listed=["helenaiscutee"])
        self.assertFalse(self._ensure(device, "someoneelse"))
        self.assertEqual(device.handle, "helenaiscutee")

    def test_an_unreadable_header_is_a_refusal_not_a_guess(self):
        """Not being able to read the account is not the same as being on it.
        Posting a model's reel on the wrong account cannot be undone."""
        device = FakeDevice(handle="helenaiscutee", header_readable=False)
        self.assertFalse(self._ensure(device, "helenaiscutee"))

    def test_the_at_sign_is_ignored_on_both_sides(self):
        device = FakeDevice(handle="@helenaiscutee")
        self.assertTrue(self._ensure(device, "@helenaiscutee"))


class ReportTest(TestCase):
    """The page has to show a second account that is configured but idle."""

    class FakeClient:
        def __init__(self, profiles):
            self._profiles = profiles

        def second_account_profiles(self):
            return self._profiles

    def _row(self, profile_id, slot, status, day="2026-08-07"):
        fields = {at.F_PQ_SCHEDULED: f"{day}T09:00:00.000Z",
                  at.F_PQ_POST_STATUS: status,
                  at.F_PQ_TARGET_PROFILE: [profile_id]}
        if slot:
            fields[at.F_PQ_ACCOUNT_SLOT] = slot
        return {"id": f"q{profile_id}{slot}{status}", "fields": fields}

    PROFILES = [
        {"record_id": "p1", "name": "Jil 5", "status": "Active",
         "primary": "helenaiscutee", "second": "jiji.ll12",
         "checked_at": "2026-08-05T18:49:12.000Z", "usable": True},
        {"record_id": "p2", "name": "Jil 6", "status": "Active",
         "primary": "helenaypurebabe", "second": None,
         "checked_at": None, "usable": False},
    ]

    def test_the_days_posts_are_split_per_account(self):
        rows = [self._row("p1", "Primary", "Posted"),
                self._row("p1", "Second", "Verifying"),
                self._row("p1", "Second", "Pending")]
        data = report.second_accounts(self.FakeClient(self.PROFILES), rows=rows,
                                      day="2026-08-07")

        jil5 = next(p for p in data["profiles"] if p["name"] == "Jil 5")
        self.assertEqual(jil5["primary_today"], {"Posted": 1})
        self.assertEqual(jil5["second_today"], {"Verifying": 1, "Pending": 1})
        self.assertEqual(jil5["second_queued"], 2)

    def test_a_second_account_with_nothing_queued_is_visible(self):
        rows = [self._row("p1", "Primary", "Posted")]
        data = report.second_accounts(self.FakeClient(self.PROFILES), rows=rows,
                                      day="2026-08-07")

        jil5 = next(p for p in data["profiles"] if p["name"] == "Jil 5")
        self.assertEqual(jil5["second_queued"], 0)
        self.assertEqual(data["counts"]["usable"], 1)
        self.assertEqual(data["counts"]["incomplete"], 1)

    def test_rows_from_another_day_are_not_counted(self):
        rows = [self._row("p1", "Second", "Posted", day="2026-08-06")]
        data = report.second_accounts(self.FakeClient(self.PROFILES), rows=rows,
                                      day="2026-08-07")
        self.assertEqual(data["profiles"][0]["second_today"], {})

    def test_an_old_row_without_a_slot_counts_as_the_first_account(self):
        rows = [self._row("p1", None, "Posted")]
        data = report.second_accounts(self.FakeClient(self.PROFILES), rows=rows,
                                      day="2026-08-07")
        jil5 = next(p for p in data["profiles"] if p["name"] == "Jil 5")
        self.assertEqual(jil5["primary_today"], {"Posted": 1})

    def test_a_base_without_the_field_says_so(self):
        data = report.second_accounts(self.FakeClient(None), rows=[], day="2026-08-07")
        self.assertFalse(data["supported"])
        self.assertEqual(data["profiles"], [])

    def test_the_section_renders(self):
        from adb_bot.automation import report_html
        rows = [self._row("p1", "Second", "Posted")]
        data = report.second_accounts(self.FakeClient(self.PROFILES), rows=rows,
                                      day="2026-08-07")
        html = report_html._section_second_accounts(data)
        self.assertIn("jiji.ll12", html)
        self.assertIn("helenaypurebabe", html)
        # The phone that cannot post yet is called out, not just listed.
        self.assertIn("handle missing", html)
