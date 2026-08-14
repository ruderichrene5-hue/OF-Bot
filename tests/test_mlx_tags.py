"""The MultiLogin tag endpoints, against what the API guide actually accepts.

Two of these encode limits found by hitting the live API rather than by reading
the docs, and both are silent failures if got wrong: `tag/search` 400s above
`limit: 100`, and the mobile tag endpoints take at most 10 tags per call.
"""

import json
import unittest
from unittest.mock import patch

from adb_bot.clients.multilogin.tags import MultiloginTagClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _tags(*names, start=0):
    return {"data": {"tags": [{"id": f"id{start + i}", "name": n, "color": "gray"}
                              for i, n in enumerate(names)]}}


class TagClientTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _post(self, responses):
        queue = list(responses)

        def fake_post(url, headers=None, json=None, timeout=None):
            self.calls.append((url, json))
            return queue.pop(0) if queue else FakeResponse({"data": {}})
        return fake_post

    def _client(self, responses=()):
        client = MultiloginTagClient("tok")
        return client, patch("adb_bot.clients.multilogin.tags.requests.post",
                             self._post(responses))

    def test_search_pages_at_a_hundred(self):
        """The endpoint answers 400 'limit invalid' above 100, so the page size
        is the API's, not ours."""
        page1 = _tags(*[f"t{i}" for i in range(100)])
        page2 = _tags("last", start=100)
        client, ctx = self._client([FakeResponse(page1), FakeResponse(page2)])
        with ctx:
            tags = client.list_tags()
        self.assertEqual(len(tags), 101)
        self.assertEqual([body["offset"] for _url, body in self.calls], [0, 100])
        self.assertTrue(all(body["limit"] == 100 for _url, body in self.calls))

    def test_search_text_is_sent_even_when_empty(self):
        """Omitting it is a 400: 'key search_text is required'."""
        client, ctx = self._client([FakeResponse(_tags("Created"))])
        with ctx:
            client.list_tags()
        self.assertIn("search_text", self.calls[0][1])

    def test_names_resolve_case_insensitively(self):
        client, ctx = self._client([FakeResponse(_tags("Warmup Day 1 Done"))])
        with ctx:
            self.assertEqual(client.tag_ids_by_name()["warmup day 1 done"], "id0")

    def test_the_name_map_is_fetched_once(self):
        client, ctx = self._client([FakeResponse(_tags("Created"))])
        with ctx:
            client.tag_ids_by_name()
            client.tag_ids_by_name()
        self.assertEqual(len(self.calls), 1)

    def test_ensure_tag_does_not_recreate_one_that_exists(self):
        client, ctx = self._client([FakeResponse(_tags("Created"))])
        with ctx:
            self.assertEqual(client.ensure_tag("created"), "id0")
        self.assertEqual([url for url, _ in self.calls],
                         ["https://api.multilogin.com/tag/search"])

    def test_ensure_tag_creates_a_missing_one_and_remembers_it(self):
        client, ctx = self._client([FakeResponse(_tags("Created")),
                                    FakeResponse({"data": {"ids": ["new-id"]}})])
        with ctx:
            self.assertEqual(client.ensure_tag("Warmup Day 2 Done", "blue"), "new-id")
            # Second ask must not create a duplicate tag in the workspace.
            self.assertEqual(client.ensure_tag("Warmup Day 2 Done", "blue"), "new-id")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][1], {"tags": [{"name": "Warmup Day 2 Done",
                                                      "color": "blue"}]})

    def test_an_invalid_colour_is_refused_here_not_by_the_api(self):
        client, _ctx = self._client()
        with self.assertRaises(ValueError):
            client.create_tag("x", "chartreuse")

    def test_assign_chunks_at_ten(self):
        client, ctx = self._client([FakeResponse({"data": {}}), FakeResponse({"data": {}})])
        with ctx:
            client.assign("619005104636297579", [f"t{i}" for i in range(12)])
        self.assertEqual([len(body["tags"]) for _url, body in self.calls], [10, 2])

    def test_assign_sends_the_profile_id_as_a_string(self):
        """The endpoint takes the 18-digit MLX API ID, and an int would be
        rendered without quotes."""
        client, ctx = self._client([FakeResponse({"data": {}})])
        with ctx:
            client.assign(619005104636297579, ["t1"])
        self.assertEqual(self.calls[0][1]["profile_id"], "619005104636297579")

    def test_nothing_to_assign_makes_no_call(self):
        client, ctx = self._client()
        with ctx:
            self.assertTrue(client.assign("111", []))
        self.assertEqual(self.calls, [])

    def test_retag_removes_before_it_adds(self):
        """Mid-sweep a profile must never be seen carrying both its old day tag
        and its new one -- a person filtering on the old one would read it as
        not having advanced."""
        client, ctx = self._client([FakeResponse({"data": {}}), FakeResponse({"data": {}})])
        with ctx:
            client.retag("111", add=["new"], remove=["old"])
        self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in self.calls],
                         ["unassign", "assign"])

    def test_an_http_error_is_raised_not_swallowed(self):
        client, ctx = self._client([FakeResponse({"status": {"message": "limit invalid"}}, 400)])
        with ctx, self.assertRaises(RuntimeError):
            client.list_tags()


if __name__ == "__main__":
    unittest.main()
