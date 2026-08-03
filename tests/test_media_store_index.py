"""MediaStore index confirmation for a just-pushed file.

This check never once succeeded in production. Every push logged "MediaStore
index not confirmed after 4 attempts; proceeding anyway" and burned ~9 s doing
it. Two causes, both reproduced on a real MLX phone with the share-Intent probe:

- `_data` is the raw filesystem path column, deprecated and unqueryable under
  scoped storage on Android 10+.
- a video is not in the generic `external/file` table; it is in
  `external/video/media`. That query returned an empty result.

`_display_name` against the right collection resolved instantly, returning
`content://media/external/video/media/110`. These tests pin both halves so the
wait cannot silently regress to always-failing.
"""

import unittest
from unittest import mock

from adb_bot.automation.flows import instagram as ig


class CollectionRoutingTest(unittest.TestCase):
    def test_video_goes_to_the_video_collection(self):
        for name in ("/sdcard/Download/clip.mp4", "/sdcard/Download/a.mov",
                     "/sdcard/Download/b.WEBM"):
            self.assertEqual(ig._media_store_collection(name),
                             "content://media/external/video/media", name)

    def test_image_goes_to_the_images_collection(self):
        for name in ("/sdcard/Download/a.jpg", "/sdcard/Download/b.PNG"):
            self.assertEqual(ig._media_store_collection(name),
                             "content://media/external/images/media", name)

    def test_unknown_extension_falls_back_to_the_file_table(self):
        self.assertEqual(ig._media_store_collection("/sdcard/Download/x.bin"),
                         "content://media/external/file")


class IndexQueryTest(unittest.TestCase):
    REMOTE = "/sdcard/Download/laila_variant_001.mp4"

    def _run(self, responder):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(" ".join(command))
            return mock.Mock(stdout=responder(" ".join(command)), stderr="")

        with mock.patch.object(ig, "_run_hidden", side_effect=fake_run), \
             mock.patch.object(ig.time, "sleep"):
            ok = ig._adb_wait_for_media_store_index("1.2.3.4:5555", self.REMOTE)
        return ok, calls

    def test_display_name_on_the_video_table_confirms_immediately(self):
        """The combination that works on the real device."""
        def responder(command):
            if "_display_name" in command and "video/media" in command:
                return "Row: 0 _id=110"
            return "No result found."

        ok, calls = self._run(responder)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1, "should confirm on the first query, not poll")

    def test_queries_display_name_before_data(self):
        ok, calls = self._run(lambda command: "No result found.")
        self.assertFalse(ok)
        self.assertIn("_display_name", calls[0])

    def test_legacy_data_column_still_works_as_a_fallback(self):
        """Older builds where the deprecated path column does answer."""
        def responder(command):
            return "Row: 0 _id=7" if "_data=" in command else "No result found."

        ok, _calls = self._run(responder)
        self.assertTrue(ok)

    def test_the_old_broken_combination_would_now_fail_loudly(self):
        """Regression guard: `external/file` + `_data` is what shipped, and it
        always returned nothing. If someone restores it, this fails."""
        def responder(command):
            # Simulate the device: only _display_name on video/media answers.
            if "_display_name" in command and "video/media" in command:
                return "Row: 0 _id=110"
            if "external/file" in command:
                return ""
            return "No result found."

        ok, calls = self._run(responder)
        self.assertTrue(ok)
        self.assertNotIn("external/file", calls[0])

    def test_empty_output_is_not_treated_as_indexed(self):
        """The file table returned '' rather than 'No result found.' -- an empty
        string must not read as a hit."""
        ok, _calls = self._run(lambda command: "")
        self.assertFalse(ok)

    def test_failure_is_not_fatal(self):
        with mock.patch.object(ig, "_run_hidden", side_effect=OSError("adb gone")), \
             mock.patch.object(ig.time, "sleep"):
            self.assertFalse(
                ig._adb_wait_for_media_store_index("1.2.3.4:5555", self.REMOTE))

    def test_where_clause_is_quoted_for_the_device_shell(self):
        """adb joins post-`shell` args without escaping, so the where clause has
        to survive the device shell's own parsing."""
        _ok, calls = self._run(lambda command: "No result found.")
        self.assertIn("_display_name=", calls[0])
        self.assertIn("laila_variant_001.mp4", calls[0])
        self.assertIn("--projection _id", calls[0])


if __name__ == "__main__":
    unittest.main()
