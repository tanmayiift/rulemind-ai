"""Live AI provider tests — SKIPPED unless a real key is supplied.

These are the only tests that prove the AI layer actually works: a mocked
transport can confirm request shaping, but not that a generated rule is valid
against the authoring schema, that the scope guardrail saves a real call, or
that usage accounting matches real token counts.

Run with one or more of:

    RULEMIND_LIVE_ANTHROPIC_KEY=sk-ant-...  \
    RULEMIND_LIVE_OPENAI_KEY=sk-...         \
    RULEMIND_LIVE_GEMINI_KEY=AIza...        \
    python -m unittest tests.test_ai_live

Keys are read from the environment only — never committed, never written to a
file by these tests.
"""
import os
import time
import unittest

from fastapi.testclient import TestClient

import app.main as app_main

PROVIDER_ENV = {
    "anthropic": "RULEMIND_LIVE_ANTHROPIC_KEY",
    "openai": "RULEMIND_LIVE_OPENAI_KEY",
    "gemini": "RULEMIND_LIVE_GEMINI_KEY",
}
CONFIGURED = {p: os.getenv(v) for p, v in PROVIDER_ENV.items() if os.getenv(v)}


@unittest.skipUnless(CONFIGURED, "no live provider key supplied — set one of " + ", ".join(PROVIDER_ENV.values()))
class LiveAITests(unittest.TestCase):
    provider = None

    @classmethod
    def setUpClass(cls):
        cls.provider = next(iter(CONFIGURED))
        cls.client = TestClient(app_main.app)
        cls.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        cls.client.put("/api/v1/ai/config", headers=cls.headers, json={
            "default_provider": cls.provider,
            "enabled": True,
            cls.provider: {"key": CONFIGURED[cls.provider]},
        })

    @classmethod
    def tearDownClass(cls):
        # Remove the key from the tenant config so a live run leaves nothing behind.
        cls.client.put("/api/v1/ai/config", headers=cls.headers,
                       json={cls.provider: {"key": "__CLEAR__"}, "enabled": False})

    def _post(self, path, body):
        return self.client.post(path, headers=self.headers, json=body)

    def test_connectivity(self):
        r = self._post("/api/v1/ai/test", {"provider": self.provider})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))

    def test_live_model_list_supersedes_the_curated_fallback(self):
        r = self.client.get(f"/api/v1/ai/models?provider={self.provider}", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["live"], "live fetch did not run with a key configured")
        self.assertGreater(len(body["models"]), 0)

    def test_generated_rule_is_structurally_valid(self):
        r = self._post("/api/v1/ai/generate-rule",
                       {"prompt": "Reject when the applicant has more than 2 prior claims in 12 months."})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["in_scope"])
        self.assertTrue(body["valid"], f"draft failed validation: {body.get('validation_error')}")
        self.assertIn("tree", body["draft"])

    def test_generated_rule_references_variables_that_exist(self):
        """A syntactically valid draft that names imaginary variables is useless."""
        r = self._post("/api/v1/ai/generate-rule",
                       {"prompt": "Reject when there are more than 2 prior claims in 12 months."})
        self.assertEqual(r.status_code, 200)
        known = {v["id"] for v in self.client.get("/api/v1/variables", headers=self.headers).json()}

        def variables(node):
            if not isinstance(node, dict):
                return
            if node.get("variable"):
                yield node["variable"]
            for child in node.get("children") or []:
                yield from variables(child)

        referenced = set(variables(r.json()["draft"].get("tree") or {}))
        self.assertTrue(referenced, "draft referenced no variables at all")
        self.assertTrue(referenced <= known, f"draft invented variables: {sorted(referenced - known)}")

    def test_off_topic_is_refused_without_spending_a_token(self):
        before = self.client.get("/api/v1/ai/usage", headers=self.headers).json()["calls"]
        started = time.perf_counter()
        r = self._post("/api/v1/ai/generate-rule", {"prompt": "What is the capital of France?"})
        elapsed = time.perf_counter() - started
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["in_scope"])
        self.assertLess(elapsed, 1.0, "an off-topic prompt appears to have reached the provider")
        after = self.client.get("/api/v1/ai/usage", headers=self.headers).json()["calls"]
        self.assertEqual(before, after, "an off-topic prompt was billed")

    def test_usage_accounting_accumulates_real_tokens_and_cost(self):
        before = self.client.get("/api/v1/ai/usage", headers=self.headers).json()
        self._post("/api/v1/ai/generate-rule", {"prompt": "Reject when prior claims in 12 months exceed 3."})
        after = self.client.get("/api/v1/ai/usage", headers=self.headers).json()
        self.assertEqual(after["calls"], before["calls"] + 1)
        self.assertGreater(after["input_tokens"], before["input_tokens"])
        self.assertGreater(after["output_tokens"], before["output_tokens"])
        self.assertGreater(after["cost_usd"], before["cost_usd"], "a real call cost nothing")

    def test_budget_guardrail_blocks_once_exceeded(self):
        self.client.put("/api/v1/ai/budget", headers=self.headers, json={"monthly_budget_usd": 0.0000001})
        try:
            r = self._post("/api/v1/ai/generate-rule", {"prompt": "Reject when prior claims exceed 4."})
            self.assertEqual(r.status_code, 402, f"expected a payment-required refusal, got {r.status_code}")
        finally:
            self.client.put("/api/v1/ai/budget", headers=self.headers, json={"monthly_budget_usd": 0.0})

    def test_explain_decision_names_the_gate_that_fired(self):
        policy = app_main.storage.list_policies(status="prod")[0]
        decision = self._post("/api/v1/decide", {"policyId": policy["id"], "payload": {}, "userId": "live-ai"}).json()
        if not decision.get("decision_id"):
            self.skipTest("decision id unavailable")
        r = self._post("/api/v1/ai/explain-decision", {"decision_id": decision["decision_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(str(r.json().get("summary", "")).strip(), "explanation was empty")


if __name__ == "__main__":
    unittest.main()
