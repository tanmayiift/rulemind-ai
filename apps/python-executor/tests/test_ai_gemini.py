"""Gemini provider: registration, request shaping, response parsing, pricing."""
import unittest
from unittest.mock import patch

from app import ai


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self):
        return self._payload


class _Client:
    """Captures the outgoing request so the body shape can be asserted."""
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _Client.calls.append({"url": url, "headers": headers or {}, "body": json or {}})
        return _Resp(200, {
            "candidates": [{"content": {"parts": [{"text": "hello "}, {"text": "world"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 5},
        })


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class RegistrationTests(unittest.TestCase):
    def test_gemini_is_a_supported_provider(self):
        self.assertIn("gemini", ai.SUPPORTED_PROVIDERS)
        self.assertIn("gemini", ai._PROVIDERS)
        self.assertIn("gemini", ai.CURATED_MODELS)
        self.assertIn("gemini", ai._LIVE_FETCHERS)
        self.assertIn("gemini", ai._PRICING)

    def test_default_model_is_in_the_curated_list(self):
        self.assertIn(ai.DEFAULT_MODELS["gemini"], ai.CURATED_MODELS["gemini"])


class RequestShapeTests(unittest.TestCase):
    def setUp(self):
        _Client.calls = []

    def _call(self, model="gemini-2.5-pro", system="sys", temperature=0.2):
        with patch.object(ai.httpx, "AsyncClient", _Client):
            return _run(ai._call_gemini("KEY", model, system, "user prompt", 900, temperature))

    def test_key_travels_in_a_header_not_the_query_string(self):
        self._call()
        call = _Client.calls[0]
        self.assertEqual(call["headers"].get("x-goog-api-key"), "KEY")
        self.assertNotIn("key=", call["url"])

    def test_system_prompt_uses_system_instruction(self):
        self._call(system="be terse")
        body = _Client.calls[0]["body"]
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "be terse")
        # ...and is not smuggled in as a "system" role message
        roles = [c.get("role") for c in body["contents"]]
        self.assertNotIn("system", roles)

    def test_no_system_instruction_when_no_system_prompt(self):
        self._call(system="")
        self.assertNotIn("systemInstruction", _Client.calls[0]["body"])

    def test_sampling_settings_live_under_generation_config(self):
        self._call(temperature=0.7)
        gen = _Client.calls[0]["body"]["generationConfig"]
        self.assertEqual(gen["temperature"], 0.7)
        self.assertEqual(gen["maxOutputTokens"], 900)

    def test_model_id_is_normalised_to_the_models_prefix(self):
        self._call(model="gemini-2.5-flash")
        self.assertTrue(_Client.calls[0]["url"].endswith("models/gemini-2.5-flash:generateContent"))
        _Client.calls = []
        self._call(model="models/gemini-2.5-flash")
        self.assertTrue(_Client.calls[0]["url"].endswith("models/gemini-2.5-flash:generateContent"))
        self.assertNotIn("models/models/", _Client.calls[0]["url"])


class ResponseTests(unittest.TestCase):
    def test_concatenates_parts_and_reports_usage(self):
        with patch.object(ai.httpx, "AsyncClient", _Client):
            text, usage = _run(ai._call_gemini("K", "gemini-2.5-pro", "s", "u", 100, 0.2))
        self.assertEqual(text, "hello world")
        self.assertEqual(usage, {"input_tokens": 11, "output_tokens": 5})

    def test_a_blocked_response_explains_itself(self):
        class Blocked(_Client):
            async def post(self, url, headers=None, json=None):
                return _Resp(200, {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]})
        with patch.object(ai.httpx, "AsyncClient", Blocked):
            with self.assertRaises(ai.AIError) as ctx:
                _run(ai._call_gemini("K", "gemini-2.5-pro", "s", "u", 100, 0.2))
        self.assertIn("SAFETY", str(ctx.exception))

    def test_http_error_is_surfaced(self):
        class Failing(_Client):
            async def post(self, url, headers=None, json=None):
                return _Resp(429, {}, "quota exceeded")
        with patch.object(ai.httpx, "AsyncClient", Failing):
            with self.assertRaises(ai.AIError) as ctx:
                _run(ai._call_gemini("K", "gemini-2.5-pro", "s", "u", 100, 0.2))
        self.assertIn("429", str(ctx.exception))


class PricingTests(unittest.TestCase):
    def test_rates_resolve_longest_key_first(self):
        # "gemini-1.5-flash-8b" contains "gemini-1.5-flash"; the specific rate must win.
        self.assertAlmostEqual(ai.estimate_cost("gemini", "gemini-1.5-flash-8b", 1_000_000, 0), 0.0375, places=6)
        self.assertAlmostEqual(ai.estimate_cost("gemini", "gemini-1.5-flash", 1_000_000, 0), 0.075, places=6)
        self.assertAlmostEqual(ai.estimate_cost("gemini", "gemini-2.5-flash-lite", 1_000_000, 0), 0.10, places=6)
        self.assertAlmostEqual(ai.estimate_cost("gemini", "gemini-2.5-flash", 1_000_000, 0), 0.30, places=6)
        self.assertAlmostEqual(ai.estimate_cost("gemini", "gemini-2.5-pro", 0, 1_000_000), 10.0, places=6)

    def test_unknown_gemini_model_is_never_free(self):
        self.assertGreater(ai.estimate_cost("gemini", "gemini-9-experimental", 1000, 1000), 0)


class ModelListTests(unittest.TestCase):
    def test_curated_list_used_without_a_key(self):
        out = _run(ai.list_models("gemini", None))
        self.assertEqual(out["models"], ai.CURATED_MODELS["gemini"])
        self.assertFalse(out["live"])

    def test_live_list_drops_models_that_cannot_generate(self):
        class Listing(_Client):
            async def get(self, url, headers=None):
                return _Resp(200, {"models": [
                    {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                ]})
            def raise_for_status(self): return None
        class R(_Resp):
            def raise_for_status(self): return None
        async def fake_get(self, url, headers=None):
            return R(200, {"models": [
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            ]})
        with patch.object(_Client, "get", fake_get, create=True), patch.object(ai.httpx, "AsyncClient", _Client):
            ids = _run(ai._fetch_live_models_gemini("KEY"))
        self.assertIn("gemini-2.5-pro", ids)
        self.assertNotIn("text-embedding-004", ids)


if __name__ == "__main__":
    unittest.main()
