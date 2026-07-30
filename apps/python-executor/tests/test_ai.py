"""AI Copilot layer — config (encrypted BYO key), scope guardrail, NL→rule.

Uses a mock provider (no network / no real key) so CI is deterministic.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

import app.ai as ai
import app.main as app_main
from app.storage import Storage

_RULE_JSON = json.dumps({
    "name": "Bureau gate",
    "tree": {"type": "group", "logic": "AND", "onPass": "approve", "onFail": "reject",
             "children": [{"type": "condition", "variable": "bureau_score", "operator": ">=", "value": 700}]},
})


class AITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "ai.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.calls = {"n": 0}

        def fake(api_key, model, system, user, max_tokens, temperature):
            self.calls["n"] += 1
            if "POLICY" in system:
                return json.dumps({"name": "Loan flow", "steps": [
                    {"id": "s1", "type": "rule", "ref_id": "rule_loan_bureau_gate", "label": "Bureau"},
                    {"id": "s2", "type": "outcome", "outcome": "approve", "label": "Approve"}]})
            if "explainer" in system.lower():
                return json.dumps({"summary": "Approved because the bureau score cleared the threshold.",
                                   "reason_codes": ["Bureau score above cutoff"]})
            return _RULE_JSON
        self._orig = dict(ai._PROVIDERS)
        ai._PROVIDERS["anthropic"] = fake
        ai._PROVIDERS["openai"] = fake

    def tearDown(self) -> None:
        ai._PROVIDERS.clear()
        ai._PROVIDERS.update(self._orig)
        self.client.close()
        self.tempdir.cleanup()

    def _set_key(self, provider="anthropic", key="sk-SECRET-KEY-123"):
        self.client.put("/api/v1/ai/config", headers=self.headers,
                        json={"default_provider": provider, provider: {"model": "m", "key": key}})

    def test_config_masks_keys(self) -> None:
        self._set_key()
        cfg = self.client.get("/api/v1/ai/config", headers=self.headers).json()
        self.assertTrue(cfg["providers"]["anthropic"]["configured"])
        self.assertNotIn("SECRET", json.dumps(cfg))

    def test_key_encrypted_at_rest(self) -> None:
        import sqlalchemy as sa
        self._set_key(key="sk-PLAINTEXT-XYZ")
        with app_main.storage.connect() as session:
            raw = str(session.execute(sa.text("select ai_config from settings")).scalar())
        self.assertNotIn("PLAINTEXT-XYZ", raw)

    def test_out_of_scope_spends_no_token(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                                json={"prompt": "what's the weather in Paris tomorrow?"}).json()
        self.assertFalse(resp["in_scope"])
        self.assertEqual(self.calls["n"], 0)  # no paid call for off-topic

    def test_in_scope_generates_validated_draft(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                                json={"prompt": "approve when the bureau score is at least 700"}).json()
        self.assertTrue(resp["in_scope"])
        self.assertEqual(self.calls["n"], 1)
        self.assertTrue(resp["valid"])
        self.assertEqual(resp["draft"]["name"], "Bureau gate")

    def test_generate_without_key_is_422(self) -> None:
        resp = self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                                json={"prompt": "approve when bureau score >= 700"})
        self.assertEqual(resp.status_code, 422)

    def test_connection_probe(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/test", headers=self.headers, json={"provider": "anthropic"}).json()
        self.assertTrue(resp["ok"])

    def test_scope_helper_units(self) -> None:
        self.assertTrue(ai.is_in_scope("tighten the DTI cutoff for the loan policy")[0])
        self.assertTrue(ai.is_in_scope("why was applicant rejected?")[0])
        self.assertFalse(ai.is_in_scope("write me a poem about the ocean")[0])
        self.assertFalse(ai.is_in_scope("hi")[0])

    def test_generate_policy_draft(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/generate-policy", headers=self.headers,
                                json={"prompt": "run the bureau gate rule then approve"}).json()
        self.assertTrue(resp["in_scope"])
        self.assertEqual(resp["draft"]["steps"][0]["type"], "rule")
        self.assertTrue(resp["valid"])

    def test_rejection_drivers_endpoint(self) -> None:
        # produce a couple of rejecting decisions on the seeded bureau-gate policy
        for score in (400, 420, 800):
            self.client.post("/api/v1/decide", headers=self.headers,
                             json={"policyId": "policy_instant_personal_loan", "payload": {"loan": {"bureau_score": score}}})
        resp = self.client.post("/api/v1/analytics/rejection-drivers", headers=self.headers, json={"limit": 100}).json()
        self.assertGreaterEqual(resp["analyzed"], 3)
        self.assertIsInstance(resp["drivers"], list)
        # no LLM key needed for this (pure compute)
        self.assertEqual(self.calls["n"], 0)

    def test_explain_decision(self) -> None:
        self._set_key()
        dec = self.client.post("/api/v1/decide", headers=self.headers,
                               json={"policyId": "policy_instant_personal_loan", "payload": {"loan": {"bureau_score": 780}}}).json()
        # find the stored decision id
        decisions = self.client.get("/api/v1/audit/decisions?limit=5", headers=self.headers).json()
        did = decisions[0]["id"]
        resp = self.client.post("/api/v1/ai/explain-decision", headers=self.headers, json={"decision_id": did}).json()
        self.assertIn("summary", resp)
        self.assertIn("reason_codes", resp)

    # ---- AI-feature gating (turn-on-later with a key) ----
    def test_ai_disabled_until_a_key_is_configured(self) -> None:
        cfg = self.client.get("/api/v1/ai/config", headers=self.headers).json()
        self.assertFalse(cfg["any_configured"])
        self.assertFalse(cfg["enabled"])  # no key -> AI stays hidden

    def test_ai_enabled_once_a_key_is_set(self) -> None:
        self._set_key()
        cfg = self.client.get("/api/v1/ai/config", headers=self.headers).json()
        self.assertTrue(cfg["any_configured"])
        self.assertTrue(cfg["enabled"])

    def test_admin_can_turn_ai_off_while_keeping_the_key(self) -> None:
        self._set_key()
        self.client.put("/api/v1/ai/config", headers=self.headers, json={"enabled": False})
        cfg = self.client.get("/api/v1/ai/config", headers=self.headers).json()
        self.assertTrue(cfg["any_configured"])   # key still stored
        self.assertFalse(cfg["enabled"])          # but AI is off
        # re-enable
        self.client.put("/api/v1/ai/config", headers=self.headers, json={"enabled": True})
        self.assertTrue(self.client.get("/api/v1/ai/config", headers=self.headers).json()["enabled"])

    def test_enabled_flag_cannot_be_true_without_a_key(self) -> None:
        # enabling with no key configured must not report AI as on
        self.client.put("/api/v1/ai/config", headers=self.headers, json={"enabled": True})
        self.assertFalse(self.client.get("/api/v1/ai/config", headers=self.headers).json()["enabled"])


if __name__ == "__main__":
    unittest.main()
