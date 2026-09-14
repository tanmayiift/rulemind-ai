"""AI generation against a real provider, plus the guardrails that must hold without one (#123).

The AI endpoints have never been exercised against a real provider, because doing so needs a
BYO key. This file is in two halves, and the split is the point:

  * LOCAL (always runs) -- the guardrails that are supposed to work with no provider at all.
    The most valuable of these is that an off-topic prompt is refused BEFORE a paid call is
    made. That claim is testable without a key, and it had no test: test_ai.py checks
    is_in_scope() in isolation, which proves the predicate but not that the endpoint consults
    it before reaching for the provider. A refactor that moved the check below the API call
    would keep every existing test green while quietly billing for refused prompts.

  * LIVE (skipped unless RULEMIND_AI_LIVE=1 and a key is configured) -- the six generation
    endpoints against a real provider, asserting the outputs are STRUCTURALLY valid against
    the authoring schemas rather than merely non-empty. "It returned something" is what an
    LLM always does; the open question in #123 is whether what it returns can actually be
    saved.

Gated rather than mocked because a mock cannot answer that question: it returns whatever
shape the test author already believed in, which is precisely the assumption under doubt.

  RULEMIND_AI_LIVE=1 python -m unittest tests.test_ai_live
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import ai  # noqa: E402
from app.storage import Storage  # noqa: E402

LIVE = os.getenv("RULEMIND_AI_LIVE", "0") == "1"
LIVE_PROVIDER = os.getenv("RULEMIND_AI_LIVE_PROVIDER", "anthropic")
LIVE_KEY = os.getenv("RULEMIND_AI_LIVE_KEY", "")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "ai.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

        # Restore the global, and release this test's engine. Placed at the END of tearDown so
        # the teardown above still runs against THIS test's storage -- an earlier version put it
        # first and silently redirected those statements at the restored global.
        #
        # The restore is the load-bearing half. Leaving app_main.storage pointed at a deleted
        # tempdir made an UNRELATED module later fail with
        # `sqlite3.OperationalError: unable to open database file`: the failure lands on whoever
        # reads the global next, never on the test that leaked it, which is why it looked like a
        # random flake for so long.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass


class GuardrailTest(_Base):
    """No provider key required — these are the promises the AI layer makes when it is idle."""

    OUT_OF_SCOPE = "what is the weather in paris tomorrow"

    def test_an_off_topic_prompt_is_refused_without_calling_the_provider(self):
        """The token-saving claim, asserted where it matters: at the endpoint.

        ai.complete is the single entry to every paid call. Patching it to explode means any
        code path that reaches a provider fails loudly, so this passes only if the refusal
        genuinely happens first.
        """
        async def explode(*args, **kwargs):
            raise AssertionError("a paid provider call was made for an out-of-scope prompt")

        for endpoint, payload in (
            ("/api/v1/ai/generate-rule", {"prompt": self.OUT_OF_SCOPE}),
            ("/api/v1/ai/generate-policy", {"prompt": self.OUT_OF_SCOPE}),
            ("/api/v1/ai/generate-predictor", {"definition": self.OUT_OF_SCOPE}),
        ):
            with self.subTest(endpoint=endpoint):
                with mock.patch.object(ai, "complete", explode):
                    res = self.client.post(endpoint, headers=self.headers, json=payload)
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertFalse(body["in_scope"])
                self.assertEqual(body["reason"], "out-of-scope")
                self.assertIn("decisioning workspace", body["message"])

    def test_an_in_scope_prompt_reaches_the_provider_check(self):
        """The other half. If everything were refused the test above would pass vacuously, so
        an in-scope prompt must get PAST the guardrail -- here it stops at the missing-key
        422, which proves the guardrail let it through."""
        res = self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                               json={"prompt": "tighten the DTI cutoff for the loan policy"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("No AI provider configured", res.json()["detail"])

    def test_every_generation_endpoint_degrades_without_a_key(self):
        """422, never a 5xx: an unconfigured optional feature must not look like an outage."""
        endpoints = {
            "/api/v1/ai/generate-rule": {"prompt": "raise the bureau score cutoff"},
            "/api/v1/ai/generate-policy": {"prompt": "build a loan approval policy"},
            "/api/v1/ai/explain-decision": {"decision_id": "missing"},
            "/api/v1/ai/generate-predictor": {"definition": "predict default risk from bureau score"},
        }
        for endpoint, payload in endpoints.items():
            with self.subTest(endpoint=endpoint):
                res = self.client.post(endpoint, headers=self.headers, json=payload)
                self.assertLess(res.status_code, 500, f"{endpoint} returned {res.status_code}")

    def test_the_curated_catalogue_names_the_current_flagship(self):
        """The staleness bug class fixed in #120: the fallback list silently lagged the models
        an operator could actually pick. Only a floor -- the live /v1/models fetch supersedes
        this whenever a key is configured."""
        anthropic = ai.CURATED_MODELS["anthropic"]
        self.assertTrue(any("opus-5" in m for m in anthropic), anthropic)
        self.assertTrue(any("sonnet-5" in m for m in anthropic), anthropic)

    def test_pricing_prefers_the_more_specific_key(self):
        """estimate_cost takes the FIRST key that is a substring of the model id, so the table
        must stay specific-key-first. Getting this wrong over-billed Opus ~3x (#120)."""
        opus5 = ai.estimate_cost("anthropic", "claude-opus-5", 1_000_000, 0)
        opus41 = ai.estimate_cost("anthropic", "claude-opus-4-1", 1_000_000, 0)
        self.assertNotEqual(opus5, opus41,
                            "opus-4-1 must not resolve to the generic `opus` row (or vice versa)")


@unittest.skipUnless(LIVE, "set RULEMIND_AI_LIVE=1 (and RULEMIND_AI_LIVE_KEY) to run live provider tests")
class LiveGenerationTest(_Base):
    """Real provider calls. These cost money, so they are opt-in and deliberately few.

    Every assertion is about STRUCTURE, not wording: an LLM always returns prose, and the
    question #123 asks is whether the result is valid enough to save.
    """

    def setUp(self):
        super().setUp()
        if not LIVE_KEY:
            self.skipTest("RULEMIND_AI_LIVE_KEY is not set")
        # set_ai_config is the real API -- it encrypts the key at rest and is what
        # PUT /api/v1/ai/config calls. get_ai_credentials then decrypts it for the outbound
        # call. (An earlier draft invented a set_ai_credentials() that does not exist, so
        # every live test errored in setUp before reaching a provider.)
        app_main.storage.set_ai_config({
            "enabled": True,
            "default_provider": LIVE_PROVIDER,
            LIVE_PROVIDER: {"key": LIVE_KEY},
        }, tenant_id=self.tenant)
        creds = app_main.storage.get_ai_credentials(LIVE_PROVIDER, tenant_id=self.tenant)
        self.assertIsNotNone(creds, "the key did not round-trip through set_ai_config")
        self.assertEqual(creds["api_key"], LIVE_KEY)

    def test_generated_rule_is_structurally_valid(self):
        res = self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                               json={"prompt": "reject applicants whose bureau score is below 600"})
        self.assertEqual(res.status_code, 200, res.text[:400])
        body = res.json()
        self.assertTrue(body.get("in_scope", True))
        nodes = (body.get("rule") or body).get("nodes")
        self.assertTrue(nodes, f"no nodes in {body}")
        types = {n.get("type") for n in nodes}
        self.assertTrue(types & {"approve", "reject", "review"},
                        "a rule with no outcome node is rejected by the authoring schema")

    def test_a_generated_rule_can_actually_be_saved(self):
        """The assertion that makes this suite worth its cost. Everything short of POSTing the
        draft back proves only that the model produced JSON."""
        res = self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                               json={"prompt": "reject applicants whose bureau score is below 600"})
        # Assert the generation succeeded before reaching into its body, so an upstream 502
        # reports as "the provider rejected the call" rather than a bare KeyError.
        self.assertEqual(res.status_code, 200, res.text[:400])
        draft = res.json()
        rule = draft.get("rule") or draft
        self.assertIn("nodes", rule, f"no nodes to save in {str(draft)[:300]}")
        saved = self.client.post("/api/v1/rules", headers=self.headers, json={
            "name": "ai-generated-live-test",
            "nodes": rule["nodes"],
            "outcome": rule.get("outcome", "reject"),
        })
        self.assertLess(saved.status_code, 300, f"generated rule was rejected: {saved.text[:400]}")

    def test_generated_policy_is_structurally_valid(self):
        res = self.client.post("/api/v1/ai/generate-policy", headers=self.headers,
                               json={"prompt": "a loan approval policy that runs a bureau rule then a scorecard"})
        self.assertEqual(res.status_code, 200, res.text[:400])
        steps = (res.json().get("policy") or res.json()).get("steps")
        self.assertTrue(steps, "a policy with no steps is rejected by the authoring schema")

    def test_usage_accounting_accumulates_against_real_tokens(self):
        """#123 asks whether the counters move for real traffic, not just for mocked calls."""
        before = (app_main.storage.get_tenant(self.tenant) or {}).get("config", {}).get("ai_usage", {})
        self.client.post("/api/v1/ai/generate-rule", headers=self.headers,
                         json={"prompt": "approve applicants with a bureau score above 700"})
        after = (app_main.storage.get_tenant(self.tenant) or {}).get("config", {}).get("ai_usage", {})
        self.assertGreater(after.get("calls", 0), before.get("calls", 0))
        self.assertGreater(after.get("input_tokens", 0), before.get("input_tokens", 0))

    def test_the_live_model_list_supersedes_the_curated_fallback(self):
        """Asserts `live: true`, not merely that the list is non-empty.

        list_models falls back to CURATED_MODELS on any auth or network error (ai.py:253),
        so a non-empty list proves nothing -- this test passed against a deliberately invalid
        key until that was noticed. `live` is the only field that distinguishes a real fetch
        from the fallback, which is the whole claim #123 asks about.
        """
        res = self.client.get("/api/v1/ai/models", headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text[:300])
        body = res.json()
        self.assertTrue(body.get("live"),
                        f"fell back to the curated list rather than fetching: {body.get('error')}")
        self.assertTrue(body.get("models"))


if __name__ == "__main__":
    unittest.main()
