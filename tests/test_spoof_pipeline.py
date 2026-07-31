import logging
from pathlib import Path
from unittest import TestCase

from adb_bot.automation import attachments
from adb_bot.automation import spoof_pipeline
from adb_bot.automation.spoof_pipeline import LocalRawSource, RawVideo, _seed_for, run_pipeline

LOG = logging.getLogger("test")


class FakePipelineClient:
    def __init__(self, existing=None, active=None, model_ids=None):
        self._existing = set(existing or [])
        self._active = active or {}
        self._model_ids = model_ids or {}
        self.content_rows = []
        self.variant_rows = []
        self.spoofed_marks = []

    def content_pipeline_names(self):
        return set(self._existing)

    def active_accounts_by_model(self):
        return self._active

    def models_by_name(self):
        return self._model_ids

    def create_content_pipeline(self, name, model_id=None, raw_link=None):
        rec = f"recCP{len(self.content_rows) + 1}"
        self.content_rows.append((rec, name, model_id))
        return rec

    def create_spoof_variant(self, source_content_id, target_account_id, file_path, method=None, variant_id=None):
        self.variant_rows.append((source_content_id, target_account_id, file_path))
        return f"recSV{len(self.variant_rows)}"

    def set_content_pipeline_spoofed(self, record_id, failed=False):
        self.spoofed_marks.append((record_id, failed))
        return True


class FakeSource:
    """A raw source with the full contract: list_by_model / resolve / release."""

    def __init__(self, by_model):
        self._by_model = by_model
        self.released = []

    def list_by_model(self):
        return self._by_model

    def resolve(self, video):
        return video.path or None

    def release(self, video, path):
        self.released.append(path)


def _one_video(model="Nikki", name="clip1.mp4"):
    return {model: [RawVideo(model=model, name=name, path=f"/raw/{model}/{name}")]}


class SeedTest(TestCase):
    def test_seed_is_deterministic_and_per_account(self):
        self.assertEqual(_seed_for("clip1.mp4", "nikki_1"), _seed_for("clip1.mp4", "nikki_1"))
        self.assertNotEqual(_seed_for("clip1.mp4", "nikki_1"), _seed_for("clip1.mp4", "nikki_2"))
        self.assertNotEqual(_seed_for("clip1.mp4", "nikki_1"), _seed_for("clip2.mp4", "nikki_1"))


class LocalRawSourceTest(TestCase):
    def test_scans_per_model_video_files(self, ):
        import tempfile, os
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "Nikki"))
        open(os.path.join(root, "Nikki", "a.mp4"), "w").close()
        open(os.path.join(root, "Nikki", "notes.txt"), "w").close()  # ignored
        by_model = LocalRawSource(root).list_by_model()
        self.assertIn("Nikki", by_model)
        self.assertEqual([v.name for v in by_model["Nikki"]], ["a.mp4"])

    def test_missing_root_is_empty(self):
        self.assertEqual(LocalRawSource("/no/such/dir").list_by_model(), {})


class RunPipelineTest(TestCase):
    def test_dry_run_counts_but_writes_nothing(self):
        client = FakePipelineClient(active={"nikki": [{"account_id": "a1", "handle": "nikki_1"},
                                                       {"account_id": "a2", "handle": "nikki_2"}]})
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.variants_created, 2)   # 2 active accounts
        self.assertEqual(client.content_rows, [])       # nothing written
        self.assertEqual(client.variant_rows, [])

    def test_apply_fans_out_one_variant_per_account(self):
        client = FakePipelineClient(
            active={"nikki": [{"account_id": "a1", "handle": "nikki_1"},
                              {"account_id": "a2", "handle": "nikki_2"}]},
            model_ids={"nikki": "recModelN"},
        )
        made = []

        def fake_spoof(raw_path, out_dir, seed, logger=None):
            p = Path(out_dir) / f"variant_{seed}.mp4"
            made.append((raw_path, out_dir, seed))
            return p

        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), spoof_fn=fake_spoof, dry_run=False)
        self.assertEqual(report.variants_created, 2)
        self.assertEqual(len(client.content_rows), 1)
        self.assertEqual(client.content_rows[0][2], "recModelN")   # model linked
        self.assertEqual(len(client.variant_rows), 2)
        # distinct output dirs per handle, distinct seeds
        self.assertNotEqual(made[0][1], made[1][1])
        self.assertNotEqual(made[0][2], made[1][2])
        self.assertEqual(client.spoofed_marks, [("recCP1", False)])

    def test_already_processed_video_skipped(self):
        client = FakePipelineClient(existing={"clip1.mp4"},
                                    active={"nikki": [{"account_id": "a1", "handle": "nikki_1"}]})
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), spoof_fn=lambda *a, **k: Path("x.mp4"), dry_run=False)
        self.assertEqual(report.variants_created, 0)
        self.assertEqual(client.content_rows, [])

    def test_no_active_accounts_skips_with_reason(self):
        client = FakePipelineClient(active={})
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), dry_run=True)
        self.assertEqual(report.variants_created, 0)
        self.assertIn("no active accounts", report.skipped[0][1])

    def test_spoof_failure_marks_content_failed(self):
        client = FakePipelineClient(active={"nikki": [{"account_id": "a1", "handle": "nikki_1"}]})
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), spoof_fn=lambda *a, **k: None, dry_run=False)
        self.assertEqual(report.variants_created, 0)
        self.assertEqual(client.spoofed_marks, [("recCP1", True)])   # failed=True
        self.assertTrue(report.errors)

    def test_no_raw_root_is_noop(self):
        client = FakePipelineClient()
        report = run_pipeline(client, LOG, raw_root="", out_root="/out", dry_run=True)
        self.assertEqual(report.variants_created, 0)


class FakeDriveClient:
    """Stands in for adb_bot.clients.gdrive.DriveClient (no network, no Google libs)."""

    def __init__(self, folders, files):
        self._folders = folders          # [{'id','name'}]
        self._files = files              # {folder_id: [{'id','name','link'}]}
        self.downloaded = []

    def list_subfolders(self, parent_id):
        return list(self._folders)

    def list_files(self, parent_id):
        return list(self._files.get(parent_id, []))

    def download(self, file_id, dest_path):
        self.downloaded.append((file_id, dest_path))
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"video")
        return dest_path


class DriveRawSourceTest(TestCase):
    def _source(self, tmp):
        client = FakeDriveClient(
            folders=[{"id": "fNikki", "name": "Nikki"}],
            files={"fNikki": [
                {"id": "v1", "name": "clip1.mp4", "link": "https://drive/v1"},
                {"id": "d1", "name": "notes.txt", "link": None},   # ignored
            ]},
        )
        return spoof_pipeline.DriveRawSource(client, "root", temp_dir=tmp), client

    def test_lists_videos_per_model_folder(self):
        import tempfile
        src, _ = self._source(tempfile.mkdtemp())
        by_model = src.list_by_model()
        self.assertEqual(list(by_model), ["Nikki"])
        video = by_model["Nikki"][0]
        self.assertEqual(video.name, "clip1.mp4")
        self.assertEqual(video.source_id, "v1")
        self.assertEqual(video.raw_link, "https://drive/v1")
        self.assertEqual(video.path, "")        # not local until resolved
        self.assertEqual(len(by_model["Nikki"]), 1)   # .txt filtered out

    def test_resolve_downloads_and_release_deletes(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        src, client = self._source(tmp)
        video = src.list_by_model()["Nikki"][0]
        path = src.resolve(video)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(client.downloaded[0][0], "v1")
        src.release(video, path)
        self.assertFalse(Path(path).exists())    # temp copy cleaned up

    def test_release_never_deletes_a_local_file(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        local = Path(tmp) / "keep.mp4"
        local.write_bytes(b"x")
        src, _ = self._source(tmp)
        local_video = spoof_pipeline.RawVideo(model="Nikki", name="keep.mp4", path=str(local))
        src.release(local_video, str(local))     # no source_id -> not ours to delete
        self.assertTrue(local.exists())

    def test_pipeline_end_to_end_with_drive_source(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        src, client = self._source(tmp)
        airtable = FakePipelineClient(active={"nikki": [{"account_id": "a1", "handle": "nikki_1"}]})
        seen = {}

        def fake_spoof(raw_path, out_dir, seed, logger=None):
            seen["raw_path"] = raw_path
            # the raw file must exist at spoof time (downloaded, not yet released)
            seen["existed"] = Path(raw_path).is_file()
            return Path(out_dir) / "out.mp4"

        report = run_pipeline(airtable, LOG, raw_root=None, out_root=tmp,
                              source=src, spoof_fn=fake_spoof, dry_run=False)
        self.assertEqual(report.variants_created, 1)
        self.assertTrue(seen["existed"])
        self.assertFalse(Path(seen["raw_path"]).exists())   # released afterwards
        # the Drive web link is recorded on the Content Pipeline row
        self.assertEqual(len(airtable.content_rows), 1)


class BuildSourceTest(TestCase):
    def test_local_when_only_raw_root(self):
        src = spoof_pipeline.build_source(raw_root="/raw")
        self.assertIsInstance(src, spoof_pipeline.LocalRawSource)

    def test_none_when_nothing_configured(self):
        self.assertIsNone(spoof_pipeline.build_source())

    def test_pipeline_noop_without_any_source(self):
        report = run_pipeline(FakePipelineClient(), LOG, raw_root=None, out_root="/out", dry_run=True)
        self.assertEqual(report.variants_created, 0)


class AttachmentsTest(TestCase):
    def test_first_attachment_url(self):
        self.assertEqual(attachments.first_attachment_url([{"url": "http://x/a.jpg"}]), "http://x/a.jpg")
        self.assertIsNone(attachments.first_attachment_url([]))
        self.assertIsNone(attachments.first_attachment_url(None))

    def test_is_url(self):
        self.assertTrue(attachments.is_url("https://x/a.jpg"))
        self.assertFalse(attachments.is_url("C:/local/a.jpg"))
        self.assertFalse(attachments.is_url(None))
