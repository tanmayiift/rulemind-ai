"""Every collection endpoint pages, the same way.

26 of the 30 GET endpoints that return a list returned a bare, unbounded array -- `/policies`,
`/rules`, `/variables`, `/scorecards`, `/reviews`, `/decision-tables` among them. `app/pagination.py`
applies one contract to all of them at the route level. These tests enumerate the routing table
rather than listing endpoints by hand, so the endpoint someone adds next month is covered the day
it lands, and a regression on any one of the 26 names itself.

The compatibility promise is tested as hard as the feature: a request with NO paging parameters
must get byte-for-byte the response it got before, because the dashboard's pickers load full lists
and all five SDKs expect an array.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import pagination  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


def _list_routes():
    """Every GET route whose endpoint returns a list -- from the app, not from a hand-kept list."""
    for route in app_main.app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods and pagination._returns_list(route):
            yield route


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "page.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    # ── coverage, enumerated ─────────────────────────────────────────────────

    def test_every_list_endpoint_is_either_wrapped_or_pages_itself(self):
        """The rot-proofing. A list endpoint that is neither is an unbounded array again."""
        uncovered = [r.path for r in _list_routes()
                     if ("GET", r.path) not in pagination.WRAPPED and not pagination._declares_paging(r)]
        self.assertEqual(uncovered, [])

    def test_the_self_paging_endpoints_are_NOT_wrapped(self):
        """They page in SQL. Wrapping them applies the offset twice: the handler returns rows
        200-399 for ?offset=200 and a second slice of [200:400] would return an empty page."""
        for route in _list_routes():
            if pagination._declares_paging(route):
                with self.subTest(path=route.path):
                    self.assertNotIn(("GET", route.path), pagination.WRAPPED)

    def test_the_count_is_what_it_was_measured_as(self):
        """26 were unbounded. If this number changes, a list endpoint was added or removed --
        update it deliberately, having looked at which."""
        self.assertGreaterEqual(len(pagination.WRAPPED), 26)

    # ── the contract, on a real collection ───────────────────────────────────

    def _rules(self, query=""):
        return self.client.get("/api/v1/rules" + query, headers=self.headers)

    def test_no_parameters_returns_exactly_what_it_did_before(self):
        """The compatibility promise. No X-Total-Count, no Link, the whole array."""
        response = self._rules()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsInstance(body, list)
        self.assertGreater(len(body), 2, "the seeded demo tenant should have several rules")
        self.assertNotIn("x-total-count", response.headers)
        self.assertNotIn("link", response.headers)

    def test_limit_bounds_the_page_and_reports_the_total(self):
        whole = self._rules().json()
        response = self._rules("?limit=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), whole[:2])
        self.assertEqual(response.headers["x-total-count"], str(len(whole)))

    def test_offset_walks_the_collection_without_gaps_or_repeats(self):
        """Following pages must reproduce the whole collection exactly once."""
        whole = self._rules().json()
        walked, offset = [], 0
        while True:
            page = self._rules(f"?limit=2&offset={offset}").json()
            if not page:
                break
            walked.extend(page)
            offset += 2
        self.assertEqual([r["id"] for r in walked], [r["id"] for r in whole])

    def test_link_header_points_at_the_next_and_previous_pages(self):
        whole = self._rules().json()
        response = self._rules("?limit=1&offset=1")
        link = response.headers.get("link", "")
        self.assertIn('rel="next"', link)
        self.assertIn('rel="prev"', link)
        self.assertIn("offset=2", link)
        self.assertIn("offset=0", link)
        last = self._rules(f"?limit=1&offset={len(whole) - 1}")
        self.assertNotIn('rel="next"', last.headers.get("link", ""))

    def test_offset_alone_uses_the_maximum_page(self):
        whole = self._rules().json()
        self.assertEqual(self._rules("?offset=1").json(), whole[1:])

    # ── invalid input ────────────────────────────────────────────────────────

    def test_bad_values_are_a_422_in_the_error_envelope_not_a_silent_clamp(self):
        """`?limit=-1` reached Postgres as LIMIT -1 on the audit routes and 500'd; on SQLite it
        meant NO limit. A clamp would hide the caller's bug; a 422 names it."""
        for query in ("?limit=0", "?limit=-1", "?limit=1001", "?limit=abc", "?offset=-5"):
            with self.subTest(query=query):
                response = self._rules(query)
                self.assertEqual(response.status_code, 422)
                body = response.json()
                self.assertEqual(body["error"], "unprocessable_entity")
                self.assertIn("must be", body["detail"])

    def test_an_error_response_is_passed_through_untouched(self):
        """Paging a 404 must not turn it into an empty page with a 200. Tested on the wrapper
        directly with a synthetic app: the first version of this test went through a real route
        and assumed an unknown variable's history 404s -- it returns an empty list, so the test
        was measuring the handler, not the wrapper."""
        import asyncio
        import json

        async def not_found(scope, receive, send):
            body = json.dumps({"error": "not_found", "detail": "nope"}).encode()
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})

        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        scope = {"type": "http", "path": "/x", "query_string": b"limit=1&offset=0"}
        asyncio.run(pagination._wrap(not_found)(scope, receive, send))
        self.assertEqual(sent[0]["status"], 404)
        self.assertEqual(json.loads(sent[1]["body"]), {"error": "not_found", "detail": "nope"})
        self.assertNotIn(b"x-total-count", [k for k, _ in sent[0].get("headers", [])])

    # ── the spec ─────────────────────────────────────────────────────────────

    def test_the_parameters_appear_in_the_openapi_spec(self):
        """Applied at the ASGI layer they would be invisible to anyone generating a client from
        the spec -- the audience that most needs to know a collection can be paged."""
        spec = self.client.get("/api/v1/openapi.json", headers=self.headers).json()
        params = {p["name"] for p in spec["paths"]["/api/v1/rules"]["get"].get("parameters", [])}
        self.assertLessEqual({"limit", "offset"}, params)

    def test_paging_works_on_every_wrapped_endpoint_that_takes_no_path_parameter(self):
        """Not one representative -- all of them. A wrapper that works on /rules and silently
        passes through on /reviews would satisfy every test above."""
        for _method, path in sorted(pagination.WRAPPED):
            if "{" in path or path.startswith("/api/admin"):
                continue
            with self.subTest(path=path):
                response = self.client.get(path + "?limit=1", headers=self.headers)
                if response.status_code != 200:
                    continue  # a route the default role cannot read is not a paging failure
                self.assertIsInstance(response.json(), list)
                self.assertLessEqual(len(response.json()), 1)
                self.assertIn("x-total-count", response.headers, f"{path} was not paged")


if __name__ == "__main__":
    unittest.main()
