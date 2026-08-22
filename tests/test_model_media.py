import json
import tempfile
import unittest
from pathlib import Path

from adb_bot.automation import model_media as m


class LoadPicturesTest(unittest.TestCase):
    def test_a_missing_file_reads_as_empty_not_an_error(self):
        self.assertEqual(m.load_pictures(Path("/nonexistent/x.json")), {})

    def test_a_malformed_file_reads_as_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text("{not json")
            self.assertEqual(m.load_pictures(path), {})

    def test_a_real_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pictures.json"
            path.write_text(json.dumps({"Nikki": "https://example.com/nikki.jpg"}))
            self.assertEqual(m.load_pictures(path),
                            {"Nikki": "https://example.com/nikki.jpg"})

    def test_an_empty_url_for_a_model_is_dropped_not_kept_as_empty(self):
        """A blank entry someone left while filling the file in must not
        look different from the model never having been added."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pictures.json"
            path.write_text(json.dumps({"Nikki": ""}))
            self.assertEqual(m.load_pictures(path), {})


class PictureUrlForTest(unittest.TestCase):
    def test_a_configured_model_returns_its_url(self):
        pictures = {"Nikki": "https://example.com/nikki.jpg"}
        self.assertEqual(m.picture_url_for("Nikki", pictures=pictures),
                        "https://example.com/nikki.jpg")

    def test_an_unconfigured_model_returns_empty_not_a_guess(self):
        """Empty, not a fabricated placeholder -- a caller has to be able to
        tell "not set up yet" apart from "here is the real picture"."""
        self.assertEqual(m.picture_url_for("Unknown Model", pictures={}), "")


if __name__ == "__main__":
    unittest.main()
