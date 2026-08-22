"""Resolving a model's GeeLark material tag to her current profile picture."""

import unittest
from unittest import mock

from adb_bot.clients.geelark import library


class _FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def post(self, path, body):
        self.calls.append((path, body))
        if self._responses:
            return self._responses.pop(0)
        return {}


class TagIdForNameTest(unittest.TestCase):
    def test_an_exact_match_is_returned(self):
        transport = _FakeTransport(responses=[
            {"list": [{"id": "t1", "name": "Nikki"}]}])

        self.assertEqual(library.tag_id_for_name("Nikki", transport=transport),
                        "t1")

    def test_a_near_miss_is_not_treated_as_a_match(self):
        """Geelark's search matches substrings -- "Nikki" must not silently
        pick up a tag actually named "Nikki 2"."""
        transport = _FakeTransport(responses=[
            {"list": [{"id": "t2", "name": "Nikki 2"}]}])

        self.assertEqual(library.tag_id_for_name("Nikki", transport=transport), "")

    def test_no_matching_tag_returns_empty(self):
        transport = _FakeTransport(responses=[{"list": []}])

        self.assertEqual(library.tag_id_for_name("Nikki", transport=transport), "")


class PictureUrlForTagTest(unittest.TestCase):
    def test_the_newest_image_material_on_the_tag_is_returned(self):
        transport = _FakeTransport(responses=[
            {"list": [{"id": "t1", "name": "Nikki"}]},
            {"list": [
                {"id": "mat1", "fileUrl": "https://x/old.jpg", "createdTime": 100},
                {"id": "mat2", "fileUrl": "https://x/new.jpg", "createdTime": 200},
            ]},
        ])

        url = library.picture_url_for_tag("Nikki", transport=transport)

        self.assertEqual(url, "https://x/new.jpg")

    def test_a_tag_with_no_materials_returns_empty_not_a_guess(self):
        transport = _FakeTransport(responses=[
            {"list": [{"id": "t1", "name": "Nikki"}]},
            {"list": []},
        ])

        self.assertEqual(library.picture_url_for_tag("Nikki", transport=transport), "")

    def test_a_nonexistent_tag_never_even_searches_materials(self):
        transport = _FakeTransport(responses=[{"list": []}])

        url = library.picture_url_for_tag("Nikki", transport=transport)

        self.assertEqual(url, "")
        self.assertEqual(len(transport.calls), 1,
                         "searched materials without a tag id to search with")

    def test_material_search_asks_for_images_only(self):
        transport = _FakeTransport(responses=[
            {"list": [{"id": "t1", "name": "Nikki"}]},
            {"list": []},
        ])

        library.picture_url_for_tag("Nikki", transport=transport)

        _path, body = transport.calls[1]
        self.assertEqual(body["fileType"], [library.FILE_TYPE_IMAGE])
        self.assertEqual(body["tagIds"], ["t1"])


if __name__ == "__main__":
    unittest.main()
