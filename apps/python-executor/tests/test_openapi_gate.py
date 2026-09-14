"""The OpenAPI contract gate (Track B6).

Contract breaks used to ship silently: nothing compared the public API surface between
commits, so deleting an endpoint or making a field required was invisible in review unless a
reviewer happened to read the handler.

These tests exercise the CLASSIFIER, not the snapshot. Asserting "the snapshot matches" would
just restate what the gate does; what has to be proven is that each breaking-change shape is
actually recognised as breaking, and -- equally important -- that additive changes are NOT,
because a gate that fires on every addition gets regenerated reflexively and stops being a
check at all.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
for candidate in (APP_ROOT, APP_ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import openapi_gate  # noqa: E402


def spec(paths=None, schemas=None):
    return {"paths": paths or {}, "components": {"schemas": schemas or {}}}


class OpenApiGateTest(unittest.TestCase):
    # ── breaking ────────────────────────────────────────────────────────────

    def test_removing_an_operation_is_breaking(self):
        old = spec(paths={"/api/v1/rules": {"get": {}, "post": {}}})
        new = spec(paths={"/api/v1/rules": {"get": {}}})
        report = openapi_gate.compare(old, new)
        self.assertEqual(report["breaking"], ["removed operation: POST /api/v1/rules"])

    def test_removing_a_response_field_is_breaking(self):
        old = spec(schemas={"Rule": {"properties": {"id": {}, "name": {}}}})
        new = spec(schemas={"Rule": {"properties": {"id": {}}}})
        self.assertIn("removed field: Rule.name", openapi_gate.compare(old, new)["breaking"])

    def test_making_an_existing_field_required_is_breaking(self):
        """The subtle one. The field is unchanged and present in both documents -- what breaks
        is that a client which has never sent it now gets a 422."""
        old = spec(schemas={"RuleIn": {"properties": {"name": {}, "tag": {}}, "required": ["name"]}})
        new = spec(schemas={"RuleIn": {"properties": {"name": {}, "tag": {}}, "required": ["name", "tag"]}})
        self.assertIn("newly required field: RuleIn.tag", openapi_gate.compare(old, new)["breaking"])

    def test_a_new_field_that_is_required_is_breaking_not_additive(self):
        old = spec(schemas={"RuleIn": {"properties": {"name": {}}, "required": ["name"]}})
        new = spec(schemas={"RuleIn": {"properties": {"name": {}, "mode": {}}, "required": ["name", "mode"]}})
        report = openapi_gate.compare(old, new)
        self.assertIn("newly required field: RuleIn.mode", report["breaking"])
        self.assertNotIn("new optional field: RuleIn.mode", report["additive"])

    def test_making_a_request_body_newly_required_is_breaking(self):
        """Every client that was omitting the body starts getting 422s.

        This class of change was invisible until it bit: relaxing four /promote endpoints
        from a required body to an optional one changed the document and the gate reported
        "API contract unchanged", because it only inspected operations and component schemas.
        The harmless direction hid the fact that the harmful one was equally unseen.
        """
        old = spec(paths={"/api/v1/rules/{id}/promote": {"post": {"requestBody": {}}}})
        new = spec(paths={"/api/v1/rules/{id}/promote": {"post": {"requestBody": {"required": True}}}})
        report = openapi_gate.compare(old, new)
        self.assertIn("request body newly required: POST /api/v1/rules/{id}/promote",
                      report["breaking"])

    def test_making_a_request_body_optional_is_additive(self):
        """The direction actually taken. An existing client sending a body keeps working."""
        old = spec(paths={"/api/v1/rules/{id}/promote": {"post": {"requestBody": {"required": True}}}})
        new = spec(paths={"/api/v1/rules/{id}/promote": {"post": {"requestBody": {}}}})
        report = openapi_gate.compare(old, new)
        self.assertEqual(report["breaking"], [])
        self.assertIn("request body now optional: POST /api/v1/rules/{id}/promote",
                      report["additive"])

    def test_removing_an_enum_value_is_breaking(self):
        """A client may still be sending the removed value."""
        old = spec(schemas={"Out": {"properties": {"outcome": {"enum": ["approve", "review", "reject"]}}}})
        new = spec(schemas={"Out": {"properties": {"outcome": {"enum": ["approve", "reject"]}}}})
        self.assertIn("removed enum value: Out.outcome = 'review'", openapi_gate.compare(old, new)["breaking"])

    def test_removing_a_schema_is_breaking(self):
        self.assertIn("removed schema: Rule",
                      openapi_gate.compare(spec(schemas={"Rule": {}}), spec())["breaking"])

    # ── additive: must NOT block ────────────────────────────────────────────

    def test_a_new_operation_is_additive(self):
        report = openapi_gate.compare(spec(paths={}), spec(paths={"/api/v1/usage": {"get": {}}}))
        self.assertEqual(report["breaking"], [])
        self.assertEqual(report["additive"], ["new operation: GET /api/v1/usage"])

    def test_a_new_optional_field_is_additive(self):
        old = spec(schemas={"Rule": {"properties": {"id": {}}, "required": ["id"]}})
        new = spec(schemas={"Rule": {"properties": {"id": {}, "note": {}}, "required": ["id"]}})
        report = openapi_gate.compare(old, new)
        self.assertEqual(report["breaking"], [])
        self.assertIn("new optional field: Rule.note", report["additive"])

    def test_a_new_enum_value_is_additive(self):
        old = spec(schemas={"Out": {"properties": {"outcome": {"enum": ["approve"]}}}})
        new = spec(schemas={"Out": {"properties": {"outcome": {"enum": ["approve", "refer"]}}}})
        report = openapi_gate.compare(old, new)
        self.assertEqual(report["breaking"], [])
        self.assertIn("new enum value: Out.outcome = 'refer'", report["additive"])

    def test_an_unchanged_spec_reports_nothing(self):
        s = spec(paths={"/api/v1/rules": {"get": {}}}, schemas={"Rule": {"properties": {"id": {}}}})
        self.assertEqual(openapi_gate.compare(s, s), {"breaking": [], "additive": []})

    # ── the snapshot itself ─────────────────────────────────────────────────

    def test_the_committed_snapshot_matches_the_live_app(self):
        """The gate is only meaningful if the checked-in baseline is current. A stale snapshot
        would let the next real break through by comparing against fiction."""
        import json

        self.assertTrue(openapi_gate.SNAPSHOT.exists(), "openapi.json is missing")
        baseline = json.loads(openapi_gate.SNAPSHOT.read_text())
        report = openapi_gate.compare(baseline, openapi_gate.generate())
        self.assertEqual(report["breaking"], [], "the committed snapshot is behind the app")
        self.assertEqual(report["additive"], [],
                         "regenerate: python apps/python-executor/scripts/openapi_gate.py --write")


if __name__ == "__main__":
    unittest.main()
