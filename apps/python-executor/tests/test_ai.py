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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
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
        # A real seeded variable id so an AI-generated predictor draft validates against known ids.
        self.var_id = app_main.storage.list_variables()[0]["id"]

        async def fake(api_key, model, system, user, max_tokens, temperature):
            self.calls["n"] += 1
            sys_l = system.lower()
            if "POLICY" in system:
                return json.dumps({"name": "Loan flow", "steps": [
                    {"id": "s1", "type": "rule", "ref_id": "rule_loan_bureau_gate", "label": "Bureau"},
                    {"id": "s2", "type": "outcome", "outcome": "approve", "label": "Approve"}]})
            if "explainer" in sys_l:
                return json.dumps({"summary": "Approved because the bureau score cleared the threshold.",
                                   "reason_codes": ["Bureau score above cutoff"]})
            if "predictor copilot" in sys_l:
                return json.dumps({"name": "Risk predictor", "base_score": 300, "max_score": 900, "bins": [
                    {"variable_id": self.var_id, "weight": 1.0, "ranges": [
                        {"min": 0, "max": 3, "points": 10}, {"min": 3, "max": 100, "points": 40}]}]})
            if "experimentation analyst" in sys_l:
                return json.dumps({"summary": "Challenger B approves 4% more at equal risk.",
                                   "recommendation": "promote", "winning_variant": "B",
                                   "rationale": "Higher approval, comparable decline mix.", "cautions": ["Small sample"]})
            if "decision-quality analyst" in sys_l:
                return json.dumps({"summary": "Bureau-score failures drive most declines.",
                                   "top_reasons": [{"driver": "bureau_score < 700", "impact": "fails on 60% of declines"}],
                                   "recommendations": ["Review the bureau cutoff band"]})
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

    # ---- new AI actions: predictor / experiment-analysis / rejection-analysis ----
    def test_generate_predictor_draft_validates_against_known_variables(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/generate-predictor", headers=self.headers,
                                json={"definition": "build a scorecard predictor from bureau variables to rank credit risk"}).json()
        self.assertTrue(resp["in_scope"])
        self.assertEqual(self.calls["n"], 1)
        self.assertTrue(resp["valid"], resp.get("validation_error"))
        self.assertEqual(resp["draft"]["bins"][0]["variable_id"], self.var_id)

    def test_generate_predictor_off_topic_spends_no_token(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/generate-predictor", headers=self.headers,
                                json={"definition": "recommend a good pizza topping"}).json()
        self.assertFalse(resp["in_scope"])
        self.assertEqual(self.calls["n"], 0)

    def test_generate_predictor_flags_unknown_variable_ids(self) -> None:
        # Force the model to emit an unknown id — the draft must be marked invalid, never silently saved.
        async def bad(api_key, model, system, user, max_tokens, temperature):
            self.calls["n"] += 1
            return json.dumps({"name": "x", "bins": [{"variable_id": "does_not_exist", "weight": 1.0,
                                                      "ranges": [{"min": 0, "max": 1, "points": 5}]}]})
        ai._PROVIDERS["anthropic"] = bad
        self._set_key()
        resp = self.client.post("/api/v1/ai/generate-predictor", headers=self.headers,
                                json={"definition": "a scorecard predictor over the risk variables"}).json()
        self.assertFalse(resp["valid"])
        self.assertIn("does_not_exist", resp["validation_error"])

    def test_analyze_experiment(self) -> None:
        self._set_key()
        exp = self.client.post("/api/v1/experiments", headers=self.headers, json={
            "name": "Bureau cutoff A/B", "status": "running", "hash_key": "user_id",
            "target_policy_id": "policy_instant_personal_loan",
            "variants": [{"id": "A", "policy_id": "policy_instant_personal_loan", "weight": 50},
                         {"id": "B", "policy_id": "policy_instant_personal_loan", "weight": 50}]}).json()
        resp = self.client.post("/api/v1/ai/analyze-experiment", headers=self.headers,
                                json={"experiment_id": exp["id"]}).json()
        self.assertIn(resp["recommendation"], {"promote", "hold", "rollback"})
        self.assertEqual(resp["experiment_id"], exp["id"])
        self.assertIn("results", resp)

    def test_analyze_experiment_missing_is_404(self) -> None:
        self._set_key()
        resp = self.client.post("/api/v1/ai/analyze-experiment", headers=self.headers,
                                json={"experiment_id": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_analyze_rejections(self) -> None:
        self._set_key()
        for score in (400, 420, 800):
            self.client.post("/api/v1/decide", headers=self.headers,
                             json={"policyId": "policy_instant_personal_loan", "payload": {"loan": {"bureau_score": score}}})
        resp = self.client.post("/api/v1/ai/analyze-rejections", headers=self.headers,
                                json={"policy_id": "policy_instant_personal_loan", "limit": 100}).json()
        self.assertIn("summary", resp)
        self.assertIsInstance(resp["top_reasons"], list)
        self.assertIn("drivers", resp)

    def test_analyze_rejections_no_declines_skips_llm(self) -> None:
        self._set_key()
        # No decline/review decisions for this policy -> nothing to analyze -> no paid call.
        before = self.calls["n"]
        resp = self.client.post("/api/v1/ai/analyze-rejections", headers=self.headers,
                                json={"policy_id": "policy_instant_personal_loan", "limit": 100}).json()
        self.assertEqual(resp["top_reasons"], [])
        self.assertEqual(self.calls["n"], before)  # no LLM call when there is nothing to analyze

    # ---- provider request shape: `temperature` is omitted for models that reject it ----
    def test_model_omits_temperature_classification(self) -> None:
        # Claude 5 family + OpenAI o-series reject `temperature`; older models keep it.
        for m in ("claude-sonnet-5", "claude-opus-5", "claude-opus-5-20260101", "o1", "o3-mini"):
            self.assertTrue(ai._model_omits_temperature(m), m)
        for m in ("claude-3-5-sonnet-latest", "claude-3-7-sonnet", "gpt-4o", "gpt-4o-mini", ""):
            self.assertFalse(ai._model_omits_temperature(m), m)

    def test_anthropic_request_omits_temperature_for_claude5(self) -> None:
        # Live regression: the mock-provider tests never exercised the real request body, so a
        # Claude-5 `temperature` 400 shipped. Capture the outgoing body and assert the shape.
        import asyncio
        import httpx

        bodies = []

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                bodies.append(json)
                return _Resp()

        orig = httpx.AsyncClient
        httpx.AsyncClient = _Client
        try:
            asyncio.run(ai._call_anthropic("k", "claude-sonnet-5", "sys", "u", 100, 0.2))
            self.assertNotIn("temperature", bodies[-1])          # Claude 5 -> omitted
            asyncio.run(ai._call_anthropic("k", "claude-3-5-sonnet-latest", "sys", "u", 100, 0.2))
            self.assertIn("temperature", bodies[-1])             # older model -> kept
        finally:
            httpx.AsyncClient = orig

    def test_anthropic_retries_without_temperature_on_deprecation(self) -> None:
        # An unknown future model that rejects `temperature` must not hard-fail: retry once without it.
        import asyncio
        import httpx

        calls = []

        class _Resp:
            def __init__(self, code, text):
                self.status_code = code
                self.text = text

            def json(self):
                return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                calls.append(json)
                if "temperature" in json:
                    return _Resp(400, '{"error":{"message":"`temperature` is deprecated for this model."}}')
                return _Resp(200, "")

        orig = httpx.AsyncClient
        httpx.AsyncClient = _Client
        try:
            text, _ = asyncio.run(ai._call_anthropic("k", "some-future-model", "sys", "u", 100, 0.2))
            self.assertEqual(text, "ok")                 # succeeded after retry
            self.assertEqual(len(calls), 2)              # first with temperature, then without
            self.assertNotIn("temperature", calls[1])
        finally:
            httpx.AsyncClient = orig

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

    # ---- model dropdown: curated fallback + live fetch ----
    def test_models_curated_without_key(self) -> None:
        r = self.client.get("/api/v1/ai/models?provider=anthropic", headers=self.headers).json()
        self.assertFalse(r["live"])
        self.assertIn("claude-sonnet-5", r["models"])  # curated default present
        self.assertEqual(r["default"], "claude-sonnet-5")

    def test_models_live_fetch_merges_when_key_set(self) -> None:
        self._set_key(provider="anthropic")
        orig = dict(ai._LIVE_FETCHERS)
        async def _live_a(key):
            return ["claude-brand-new-9", "claude-sonnet-5"]
        ai._LIVE_FETCHERS["anthropic"] = _live_a
        try:
            r = self.client.get("/api/v1/ai/models?provider=anthropic", headers=self.headers).json()
        finally:
            ai._LIVE_FETCHERS.clear(); ai._LIVE_FETCHERS.update(orig)
        self.assertTrue(r["live"])
        self.assertIn("claude-brand-new-9", r["models"])  # newly launched model surfaced automatically

    def test_models_fall_back_to_curated_on_fetch_error(self) -> None:
        self._set_key(provider="openai")
        orig = dict(ai._LIVE_FETCHERS)
        async def boom(_key):
            raise RuntimeError("rate limited")
        ai._LIVE_FETCHERS["openai"] = boom
        try:
            r = self.client.get("/api/v1/ai/models?provider=openai", headers=self.headers).json()
        finally:
            ai._LIVE_FETCHERS.clear(); ai._LIVE_FETCHERS.update(orig)
        self.assertFalse(r["live"])
        self.assertIn("gpt-4o", r["models"])  # curated fallback


if __name__ == "__main__":
    unittest.main()
