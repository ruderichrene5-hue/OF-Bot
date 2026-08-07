import logging
import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.automation import attachments
from adb_bot.automation import spoof_pipeline
from adb_bot.automation.spoof_pipeline import LocalRawSource, RawVideo, _seed_for, run_pipeline

LOG = logging.getLogger("test")


class FakePipelineClient:
    def __init__(self, existing=None, active=None, model_ids=None, profiles=None):
        self._existing = set(existing or [])
        self._active = active or {}
        self._model_ids = model_ids or {}
        self._profiles = profiles or {}
        self.content_rows = []
        self.variant_rows = []
        self.variant_profile_rows = []
        self.variant_slots = []
        self.spoofed_marks = []

    def content_pipeline_names(self):
        return set(self._existing)

    def active_accounts_by_model(self):
        return self._active

    def profile_targets_by_model(self):
        return self._profiles

    def models_by_name(self):
        return self._model_ids

    def create_content_pipeline(self, name, model_id=None, raw_link=None):
        rec = f"recCP{len(self.content_rows) + 1}"
        self.content_rows.append((rec, name, model_id))
        return rec

    def create_spoof_variant(self, source_content_id, target_account_id, file_path, method=None,
                             variant_id=None, target_profile_id=None,
                             target_handle=None, account_slot=None):
        self.variant_rows.append((source_content_id, target_account_id, file_path))
        self.variant_profile_rows.append((source_content_id, target_profile_id, file_path))
        self.variant_slots.append((target_profile_id, target_handle, account_slot, file_path))
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


class ResolveModelTest(TestCase):
    def test_unaliased_folder_keeps_its_name(self):
        self.assertEqual(spoof_pipeline.resolve_model("Viktoria"), "Viktoria")

    def test_aliased_folder_maps_to_its_real_model(self):
        # The Drive folder 'Corina' holds Nikki's content; matching on the
        # folder name skipped every video under it as "no MLX profiles".
        self.assertEqual(spoof_pipeline.resolve_model("Corina"), "Nikki")
        self.assertEqual(spoof_pipeline.resolve_model("  mandy "), "Luisa")

    def test_aliased_folder_fans_out_to_the_real_model_targets(self):
        client = FakePipelineClient(
            profiles={"nikki": [{"profile_id": "p1", "handle": "nikki_1"},
                                {"profile_id": "p2", "handle": "nikki_2"}]},
            model_ids={"nikki": "recModelN"},
        )
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video(model="Corina")),
                              targets=spoof_pipeline.TARGETS_PROFILES, dry_run=True)
        # Without the alias this is 0 variants and one "no MLX profiles" skip.
        self.assertEqual(report.variants_created, 2)
        self.assertEqual(report.skipped, [])


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

        with tempfile.TemporaryDirectory() as out_root:
            def fake_spoof(raw_path, out_dir, seed, logger=None):
                # Mimic vtf: the output is named after the SOURCE, so every
                # account produces the same filename in the shared run folder.
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                p = Path(out_dir) / "clip_variant_001.mp4"
                p.write_text(str(seed))
                made.append((raw_path, out_dir, seed))
                return p

            report = run_pipeline(client, LOG, raw_root="/raw", out_root=out_root,
                                  source=FakeSource(_one_video()), spoof_fn=fake_spoof, dry_run=False)
            self.assertEqual(report.variants_created, 2)
            self.assertEqual(len(client.content_rows), 1)
            self.assertEqual(client.content_rows[0][2], "recModelN")   # model linked
            self.assertEqual(len(client.variant_rows), 2)
            # Both accounts share ONE run folder now, but get distinct seeds...
            self.assertEqual(made[0][1], made[1][1])
            self.assertNotEqual(made[0][2], made[1][2])
            # ...and, crucially, distinct FILES. Without the rename both rows
            # would point at the same overwritten path.
            paths = [row[2] for row in client.variant_rows]
            self.assertEqual(len(set(paths)), 2)
            for path in paths:
                self.assertTrue(Path(path).is_file())
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

    def test_profile_targets_fan_out_and_link_the_profile(self):
        """targets='profiles' spoofs for the MLX profile inventory instead of the
        Accounts table, and links the Profiles (Cloning) row on the variant."""
        client = FakePipelineClient(
            active={},   # deliberately empty: no Accounts rows exist for this model
            profiles={"nikki": [{"profile_id": "p1", "handle": "Nikki 1", "launch_id": "111"},
                                {"profile_id": "p2", "handle": "Nikki 2", "launch_id": "222"}]},
            model_ids={"nikki": "recModelN"},
        )
        with tempfile.TemporaryDirectory() as out_root:
            def fake_spoof(raw_path, out_dir, seed, logger=None):
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                p = Path(out_dir) / "clip_variant_001.mp4"
                p.write_text(str(seed))
                return p

            report = run_pipeline(client, LOG, raw_root="/raw", out_root=out_root,
                                  source=FakeSource(_one_video()), spoof_fn=fake_spoof,
                                  dry_run=False, targets="profiles")
        self.assertEqual(report.variants_created, 2)
        # The profile is linked and the account link is left empty -- writing both
        # would make the row ambiguous for the posting planner.
        self.assertEqual([row[1] for row in client.variant_profile_rows], ["p1", "p2"])
        self.assertEqual([row[1] for row in client.variant_rows], [None, None])
        # Distinct files per profile, same as the per-account fan-out.
        paths = [row[2] for row in client.variant_profile_rows]
        self.assertEqual(len(set(paths)), 2)

    def test_only_handles_narrows_to_one_target(self):
        client = FakePipelineClient(
            profiles={"nikki": [{"profile_id": "p1", "handle": "Nikki 1", "launch_id": "111"},
                                {"profile_id": "p2", "handle": "Nikki 2", "launch_id": "222"}]},
            model_ids={"nikki": "recModelN"},
        )
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), dry_run=True,
                              targets="profiles", only_handles=["nikki 1"])   # case-insensitive
        self.assertEqual(report.variants_created, 1)

    def test_only_handles_drops_models_with_no_match_rather_than_skipping(self):
        """A model nobody asked for is not a skip -- reporting it as one would
        bury the real skips under noise on every filtered run."""
        client = FakePipelineClient(
            profiles={"nikki": [{"profile_id": "p1", "handle": "Nikki 1", "launch_id": "111"}]},
            model_ids={"nikki": "recModelN"},
        )
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), dry_run=True,
                              targets="profiles", only_handles=["Jil 1"])
        self.assertEqual(report.variants_created, 0)
        self.assertEqual(report.skipped, [])

    def test_profile_targets_skip_reason_names_profiles(self):
        client = FakePipelineClient(active={"nikki": [{"account_id": "a1", "handle": "n1"}]},
                                    profiles={})
        report = run_pipeline(client, LOG, raw_root="/raw", out_root="/out",
                              source=FakeSource(_one_video()), dry_run=True, targets="profiles")
        self.assertEqual(report.variants_created, 0)
        self.assertIn("no MLX profiles", report.skipped[0][1])

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
            # spoof_fn's contract is a file that really exists -- the pipeline
            # renames it into <source>__<handle> before recording it.
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            out = Path(out_dir) / "out.mp4"
            out.write_text("video")
            return out

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


class RunFolderLayoutTest(TestCase):
    """Output layout: <out_root>/<Model>/run<N>/<source>__<handle>.mp4 --
    one run folder per raw video, shared by every account under the model."""

    def test_safe_name_strips_spaces_and_punctuation(self):
        # Output paths reach the Android shell, which splits on spaces.
        self.assertEqual(spoof_pipeline.safe_name("viktoria 1 I 1 aug"), "viktoria_1_I_1_aug")
        self.assertEqual(spoof_pipeline.safe_name("Rodrigo (test)"), "Rodrigo_test")
        self.assertEqual(spoof_pipeline.safe_name("a/b\\c"), "a_b_c")
        self.assertEqual(spoof_pipeline.safe_name("  ...  "), "unnamed")

    def test_first_run_is_run1(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(spoof_pipeline.next_run_dir(root, "Rodrigo").name, "run1")

    def test_numbering_continues_past_existing_runs(self):
        with tempfile.TemporaryDirectory() as root:
            model = Path(root) / "Rodrigo"
            for name in ("run1", "run2", "run7", "notarun", "run_x"):
                (model / name).mkdir(parents=True)
            # Highest wins, non-run folders are ignored -- not a simple count.
            self.assertEqual(spoof_pipeline.next_run_dir(root, "Rodrigo").name, "run8")

    def test_numbering_is_per_model(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "Rodrigo" / "run5").mkdir(parents=True)
            self.assertEqual(spoof_pipeline.next_run_dir(root, "Jasmin").name, "run1")

    def test_finalize_variant_renames_to_source_and_handle(self):
        with tempfile.TemporaryDirectory() as root:
            produced = Path(root) / "clip_variant_001.mp4"
            produced.write_text("x")
            out = spoof_pipeline.finalize_variant(produced, "my clip 1.mp4", "Rodrigo (test)")
            self.assertEqual(out.name, "my_clip_1__Rodrigo_test.mp4")
            self.assertTrue(out.is_file())
            self.assertFalse(produced.exists())

    def test_each_video_gets_its_own_run_folder(self):
        client = FakePipelineClient(
            active={"rodrigo": [{"account_id": "a1", "handle": "acct_one"},
                                {"account_id": "a2", "handle": "acct_two"}]},
            model_ids={"rodrigo": "recModelR"},
        )
        videos = {"Rodrigo": [RawVideo(model="Rodrigo", name="v1.mp4", path="/raw/Rodrigo/v1.mp4"),
                              RawVideo(model="Rodrigo", name="v2.mp4", path="/raw/Rodrigo/v2.mp4")]}

        with tempfile.TemporaryDirectory() as out_root:
            def fake_spoof(raw_path, out_dir, seed, logger=None):
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                p = Path(out_dir) / f"{Path(raw_path).stem}_variant_001.mp4"
                p.write_text(str(seed))
                return p

            report = run_pipeline(client, LOG, raw_root="/raw", out_root=out_root,
                                  source=FakeSource(videos), spoof_fn=fake_spoof, dry_run=False)

            self.assertEqual(report.variants_created, 4)   # 2 videos x 2 accounts
            self.assertEqual(report.errors, [])

            model_dir = Path(out_root) / "Rodrigo"
            runs = sorted(p.name for p in model_dir.iterdir() if p.is_dir())
            self.assertEqual(runs, ["run1", "run2"])

            # Two distinct files per run, one per account, all four unique.
            for run in runs:
                files = sorted(p.name for p in (model_dir / run).iterdir())
                self.assertEqual(len(files), 2, f"{run} should hold one file per account")
            paths = [row[2] for row in client.variant_rows]
            self.assertEqual(len(set(paths)), 4)

    def test_rename_failure_is_reported_not_recorded(self):
        # A variant that cannot be named must NOT reach Airtable: the un-renamed
        # file gets overwritten by the next account, so the row would point at
        # the wrong account's video.
        client = FakePipelineClient(
            active={"rodrigo": [{"account_id": "a1", "handle": "acct_one"}]},
            model_ids={"rodrigo": "recModelR"},
        )
        videos = {"Rodrigo": [RawVideo(model="Rodrigo", name="v1.mp4", path="/raw/Rodrigo/v1.mp4")]}

        with tempfile.TemporaryDirectory() as out_root:
            def fake_spoof(raw_path, out_dir, seed, logger=None):
                return Path(out_dir) / "never_created.mp4"   # nothing on disk

            report = run_pipeline(client, LOG, raw_root="/raw", out_root=out_root,
                                  source=FakeSource(videos), spoof_fn=fake_spoof, dry_run=False)

            self.assertEqual(report.variants_created, 0)
            self.assertEqual(client.variant_rows, [])
            self.assertEqual(len(report.errors), 1)
            self.assertEqual(client.spoofed_marks, [("recCP1", True)])   # marked failed
