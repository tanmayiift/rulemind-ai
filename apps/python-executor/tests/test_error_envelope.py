"""One error envelope, so a client can write one parser.

There were two incompatible shapes. `middleware.py` returned `{"error": ..., "detail": ...}`
and sometimes only `{"error": ...}`; every route handler raised `HTTPException`, which FastAPI
renders as `{"detail": ...}` with no `error` at all; and request-body validation produced a
third, `{"detail": [ ...list... ]}`. On top of that `detail` is a string at 187 call sites and
a DICT at 3. A client had to sniff which field was present and then sniff the type of `detail`.

THE FIX IS ADDITIVE AND THAT IS THE POINT. The obvious "normalise detail to a string" would
destroy the three structured errors -- and those three are the good ones: `invalid_input`
carries per-field `issues`, the single most useful error this product produces, and
`idempotency_key_reused` carries the clashing key. Seven existing tests and any integrator
depend on that shape. So `error` is guaranteed present and guaranteed a string -- the field to
switch on -- and `detail` keeps whatever shape the handler chose, as context.

`test_a_structured_detail_keeps_its_own_error_code` is the one that matters: an envelope that
overwrote `invalid_input` with a generic `unprocessable_entity` would make the envelope
disagree with the body it wraps.
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import errors as app_errors  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class ErrorEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "err.db"))
        self.client = TestClient(app_main.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def _assert_envelope(self, response):
        body = response.json()
        self.assertIsInstance(body, dict, f"error body is not an object: {body!r}")
        self.assertIn("error", body, f"no `error` field to switch on: {body!r}")
        self.assertIsInstance(body["error"], str,
                              f"`error` must always be a string: {body['error']!r}")
        self.assertTrue(body["error"])
        self.assertIn("detail", body, f"no `detail` context: {body!r}")
        return body

    def test_a_handler_raised_404_carries_the_envelope(self):
        """This is the common case -- 85 of the raise sites are 404 -- and before the handler
        it was a bare `{"detail": "..."}` with nothing machine-readable in it."""
        body = self._assert_envelope(
            self.client.get("/api/v1/policies/no_such_policy", headers=self.headers))
        self.assertEqual(body["error"], "not_found")

    def test_an_unmatched_route_carries_the_envelope(self):
        """Raised by Starlette's router, not by FastAPI, so registering the handler on
        FastAPI's HTTPException subclass would have left the first error a client ever hits
        without an `error` field."""
        body = self._assert_envelope(
            self.client.get("/api/v1/definitely-not-a-route", headers=self.headers))
        self.assertEqual(body["error"], "not_found")

    def test_a_wrong_method_carries_the_envelope(self):
        body = self._assert_envelope(
            self.client.delete("/api/v1/decide", headers=self.headers))
        self.assertEqual(body["error"], "method_not_allowed")

    def test_an_unauthenticated_request_carries_the_envelope(self):
        """The middleware path, which had `error` but not always `detail`."""
        self._assert_envelope(self.client.get("/api/v1/policies"))

    def test_malformed_request_body_carries_the_envelope(self):
        """FastAPI's own validation error -- the third shape, and the one an integrator meets
        on their first malformed request. `detail` stays pydantic's list; `error` makes it
        parseable alongside everything else."""
        body = self._assert_envelope(
            self.client.post("/api/v1/decide", headers=self.headers, json={"nope": 1}))
        self.assertEqual(body["error"], "validation_error")
        self.assertIsInstance(body["detail"], list)

    def test_a_structured_detail_keeps_its_own_error_code(self):
        """THE test. `invalid_input` and `idempotency_key_reused` put their own code inside
        `detail`. An envelope that overwrote it with the generic status word would make the
        envelope disagree with the body it wraps -- and a client switching on `error` would
        never see the specific case."""
        from fastapi import HTTPException

        structured = {"error": "invalid_input", "issues": [{"path": "a.b", "message": "bad"}]}
        body = app_errors.error_body(422, structured)
        self.assertEqual(body["error"], "invalid_input")
        self.assertEqual(body["detail"], structured)
        self.assertIsNone(getattr(HTTPException(422, structured), "unused", None))

    def test_a_string_detail_is_left_alone(self):
        body = app_errors.error_body(404, "policy 'x' not found")
        self.assertEqual(body, {"error": "not_found", "detail": "policy 'x' not found"})

    def test_an_unmapped_status_still_gets_a_string_code(self):
        """Never absent, never non-string. A status outside the vocabulary still parses."""
        body = app_errors.error_body(418, "teapot")
        self.assertEqual(body["error"], "error")

    def test_a_reason_that_is_already_a_code_surfaces_as_the_error(self):
        """mTLS refusals carry reasons that ARE codes -- client_cert_missing,
        client_cert_expired, client_cert_not_allowed, client_cert_unparseable,
        proxy_verify_failed. The first version of the envelope put the generic "unauthorized" in
        `error` and the code in `detail`, which made a missing certificate indistinguishable
        from a missing API key for a client switching on `error`. Caught by the mTLS suite."""
        for reason in ("client_cert_missing", "client_cert_expired", "client_cert_not_allowed",
                       "client_cert_unparseable", "proxy_verify_failed"):
            with self.subTest(reason=reason):
                body = app_errors.error_body(401, {"error": reason, "message": "x"})
                self.assertEqual(body["error"], reason)

    def test_a_structured_detail_without_its_own_code_falls_back_to_the_status(self):
        body = app_errors.error_body(409, {"clash": "x"})
        self.assertEqual(body["error"], "conflict")

    def test_the_budget_handler_uses_the_same_envelope(self):
        """It predates the envelope and already had both fields, in the other order. Pinned so
        it cannot drift back to `detail`-first or lose `error`."""
        source = (APP_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"error": "evaluation_budget_exceeded"', source)

    def test_every_status_code_the_routers_raise_has_a_name(self):
        """A status raised by a handler but missing from the vocabulary degrades to the generic
        "error", which is exactly the unparseable case this exists to remove."""
        import re

        raised = set()
        for path in (APP_ROOT / "app" / "routers").glob("*.py"):
            raised |= {int(code) for code in
                       re.findall(r"status_code=(\d{3})", path.read_text(encoding="utf-8"))}
        raised |= {int(code) for code in re.findall(
            r"status_code=(\d{3})", (APP_ROOT / "app" / "main.py").read_text(encoding="utf-8"))}
        errors = {code for code in raised if code >= 400}
        unnamed = sorted(errors - set(app_errors.ERROR_CODES))
        self.assertEqual(unnamed, [],
                         f"these error statuses are raised but have no code: {unnamed}")


if __name__ == "__main__":
    unittest.main()
