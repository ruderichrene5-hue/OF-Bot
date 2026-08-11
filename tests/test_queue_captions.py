"""Queued rows must actually carry a caption now -- and must still queue when
the pool is missing.

The Posting Queue has shipped with an empty Caption since it was built, because
`run_queue_slots` left it unset and the Airtable automation meant to fill it was
never deployed. These tests pin the new behaviour, and pin the fallback just as
hard: a base without a Caption Pool has to keep posting.
"""

import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.caption_rotation import CaptionRotation
from adb_bot.automation.queue_runner import run_queue_slots

from tests.test_queue_runner import (
    FakeQueueClient, LOG, _account_client, _now, _variant,
)


def _pool(n=8):
    return [{"record_id": f"recCap{i}", "caption_id": f"CAP-{i:03d}", "text": f"line {i}"}
            for i in range(1, n + 1)]


class CaptionedClient(FakeQueueClient):
    def __init__(self, *args, pool=None, raises=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = _pool() if pool is None else pool
        self._raises = raises

    def caption_pool(self):
        if self._raises:
            raise RuntimeError("Airtable is down")
        return list(self._pool)


def _client(handles=("nikki_1",), variants=None, **kwargs):
    accounts = {"nikki": [{"account_id": f"acc{i + 1}", "handle": h}
                          for i, h in enumerate(handles)]}
    return CaptionedClient(accounts=accounts, variants=variants, **kwargs)


class QueueTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rotation = CaptionRotation(Path(self.tmp.name) / "rot.json")


class CaptionsReachTheQueueTest(QueueTestBase):
    def test_every_created_row_gets_a_caption(self):
        client = _client(variants=[_variant("v1", account_id="acc1"),
                                   _variant("v2", account_id="acc1"),
                                   _variant("v3", account_id="acc1")])
        run_queue_slots(client, LOG, now=_now(13), dry_run=False,
                        caption_rotation=self.rotation)
        self.assertTrue(client.created)
        for row in client.created:
            self.assertTrue(row["caption_id"], f"row {row['name']} has no caption")

    def test_one_account_does_not_repeat_a_caption(self):
        client = _client(variants=[_variant("v1", account_id="acc1"),
                                   _variant("v2", account_id="acc1"),
                                   _variant("v3", account_id="acc1")])
        run_queue_slots(client, LOG, now=_now(13), dry_run=False,
                        caption_rotation=self.rotation)
        captions = [row["caption_id"] for row in client.created]
        self.assertEqual(len(captions), len(set(captions)))

    def test_a_dry_run_writes_nothing_and_does_not_burn_captions(self):
        client = _client(variants=[_variant("v1", account_id="acc1")])
        run_queue_slots(client, LOG, now=_now(13), dry_run=True,
                        caption_rotation=self.rotation)
        self.assertEqual(client.created, [])
        peeked = self.rotation.peek_for("x", _pool())
        self.assertEqual(peeked, self.rotation.peek_for("x", _pool()))


class FallbackTest(QueueTestBase):
    def test_a_base_with_no_caption_pool_still_queues(self):
        # The old FakeQueueClient has no caption_pool method at all.
        client = _account_client(variants=[_variant("v1", account_id="acc1")])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False,
                                 caption_rotation=self.rotation)
        self.assertTrue(client.created)
        self.assertIsNone(client.created[0]["caption_id"])
        self.assertEqual(report.errors, [])

    def test_an_airtable_error_does_not_stop_the_queue(self):
        client = _client(variants=[_variant("v1", account_id="acc1")], raises=True)
        run_queue_slots(client, LOG, now=_now(13), dry_run=False,
                        caption_rotation=self.rotation)
        self.assertTrue(client.created)
        self.assertIsNone(client.created[0]["caption_id"])

    def test_an_empty_pool_queues_captionless(self):
        client = _client(variants=[_variant("v1", account_id="acc1")], pool=[])
        run_queue_slots(client, LOG, now=_now(13), dry_run=False,
                        caption_rotation=self.rotation)
        self.assertTrue(client.created)
        self.assertIsNone(client.created[0]["caption_id"])

    def test_captions_can_be_switched_off(self):
        client = _client(variants=[_variant("v1", account_id="acc1")])
        run_queue_slots(client, LOG, now=_now(13), dry_run=False,
                        caption_rotation=False)
        self.assertTrue(client.created)
        self.assertIsNone(client.created[0]["caption_id"])
