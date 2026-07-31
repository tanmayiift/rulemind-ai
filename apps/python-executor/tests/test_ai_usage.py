"""AI cost/usage tracking + budget cap (BYO-key visibility and guardrails).

The customer pays their provider directly, so there was previously zero visibility or
limit — a bill-shock / abuse risk. This records per-workspace tokens + an estimated
cost per call and blocks generation once a monthly budget is hit.
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.ai as ai  # noqa: E402
import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402

_RULE_JSON = json.dumps({
    "name": "Bureau gate",
    "tree": {"type": "group", "logic": "AND", "onPass": "approve", "onFail": "reject",
             "children": [{"type": "condition", "variable": "bureau_score", "operator": ">=", "value": 700}]},
})


class EstimateCostTests(unittest.TestCase):
    def test_anthropic_sonnet(self):
        # 1000 in * $3/1M + 500 out * $15/1M = 0.003 + 0.0075
        self.assertAlmostEqual(ai.estimate_cost("anthropic", "claude-sonnet-5", 1000, 500), 0.0105, places=6)

    def test_openai_mini_is_cheaper_than_4o(self):
        self.assertLess(ai.estimate_cost("openai", "gpt-4o-mini", 1000, 1000),
                        ai.estimate_cost("openai", "gpt-4o", 1000, 1000))

    def test_unknown_model_uses_default_not_zero(self):
        self.assertGreater(ai.estimate_cost("anthropic", "some-future-model", 1000, 1000), 0)


class AIUsageApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "usage.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

        async def fake(api_key, model, system, user, max_tokens, temperature):
            return _RULE_JSON, {"input_tokens": 1000, "output_tokens": 500}
        self._orig = dict(ai._PROVIDERS)
        ai._PROVIDERS["anthropic"] = fake
        ai._PROVIDERS["openai"] = fake
        self.client.put("/api/v1/ai/config", headers=self.headers,
                        json={"default_provider": "anthropic", "anthropic": {"model": "claude-sonnet-5", "key": "sk-x"}})

    def tearDown(self):
        ai._PROVIDERS.clear()
        ai._PROVIDERS.update(self._orig)
        self.client.close()
        self.tempdir.cleanup()

    def _generate(self):
        return self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                                json={"prompt": "approve when the bureau score is at least 700"})

    def test_usage_accrues_after_a_call(self):
        self.assertEqual(self.client.get("/api/v1/ai/usage", headers=self.headers).json()["calls"], 0)
        self.assertEqual(self._generate().status_code, 200)
        usage = self.client.get("/api/v1/ai/usage", headers=self.headers).json()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["output_tokens"], 500)
        self.assertAlmostEqual(usage["cost_usd"], 0.0105, places=6)
        self.assertIn("anthropic/claude-sonnet-5", usage["by_model"])

    def test_two_calls_accumulate(self):
        self._generate()
        self._generate()
        usage = self.client.get("/api/v1/ai/usage", headers=self.headers).json()
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["input_tokens"], 2000)

    def test_budget_blocks_further_generation(self):
        # Budget just above one call's cost: the 1st call runs, the 2nd is blocked.
        self.client.put("/api/v1/ai/budget", headers=self.headers, json={"monthly_budget_usd": 0.005})
        self.assertEqual(self._generate().status_code, 200)  # usage now 0.0105 > 0.005
        blocked = self._generate()
        self.assertEqual(blocked.status_code, 402)
        self.assertIn("budget", blocked.text.lower())

    def test_reset_clears_usage_and_unblocks(self):
        self.client.put("/api/v1/ai/budget", headers=self.headers, json={"monthly_budget_usd": 0.005})
        self._generate()
        self.assertEqual(self._generate().status_code, 402)
        self.client.post("/api/v1/ai/usage/reset", headers=self.headers)
        self.assertEqual(self.client.get("/api/v1/ai/usage", headers=self.headers).json()["calls"], 0)
        self.assertEqual(self._generate().status_code, 200)  # unblocked

    def test_zero_budget_means_no_cap(self):
        for _ in range(3):
            self.assertEqual(self._generate().status_code, 200)
        usage = self.client.get("/api/v1/ai/usage", headers=self.headers).json()
        self.assertFalse(usage["over_budget"])


if __name__ == "__main__":
    unittest.main()
