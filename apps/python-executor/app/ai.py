"""AI Copilot layer — BYO-key, server-side, provider-agnostic (OpenAI + Anthropic).

Design points:
- The client's API key is never handled by the browser or logged; it is passed in
  from encrypted per-tenant settings and used only for the outbound LLM call.
- A cheap, LOCAL scope guardrail runs BEFORE any paid call: an off-topic question
  (unrelated to the tenant's decisioning domain) is refused without spending a token.
- Every generated artifact is a DRAFT — callers must still validate (MECE) and
  test-gate before promotion. Nothing here deploys anything.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

# Latest, most capable defaults; overridable via ai_config.model.
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-4o"}

# ---- scope guardrail (token-saving) ------------------------------------------

# Domain vocabulary — a question must touch the decisioning domain (or a named
# tenant entity) to be worth a paid LLM call.
_DOMAIN_TERMS = {
    "rule", "rules", "policy", "policies", "variable", "variables", "scorecard",
    "scorecards", "connector", "connectors", "decision", "decisions", "simulation",
    "simulate", "backtest", "approve", "approval", "reject", "rejection", "rejected",
    "review", "outcome", "credit", "risk", "loan", "lending", "underwriting", "fraud",
    "bureau", "score", "threshold", "condition", "operator", "workflow", "predictor",
    "feature", "reason", "adverse", "gate", "mece", "cutoff", "dti", "kyc", "income",
    "delinquenc", "utilization", "band", "weight", "woe", "model", "eligibility",
    "limit", "pricing", "segment", "cohort", "champion", "challenger", "experiment",
}
_GREETINGS = {"hi", "hello", "hey", "thanks", "thank you", "ok", "okay"}


def is_in_scope(text: str, entity_names: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Cheap, local, no-API check. Returns (in_scope, reason). Off-topic questions
    are refused here so no token is spent on them."""
    t = (text or "").lower().strip()
    if not t:
        return False, "empty"
    if len(t.split()) <= 2 and t in _GREETINGS:
        return False, "greeting"
    words = set(re.findall(r"[a-z_]+", t))
    if words & _DOMAIN_TERMS:
        return True, "domain-term"
    for name in entity_names or []:
        if name and name.lower() in t:
            return True, "entity-name"
    return False, "out-of-scope"


OUT_OF_SCOPE_MESSAGE = (
    "I can only help with your decisioning workspace — rules, scorecards, variables, "
    "policies, simulations, and decisions. Ask me something in that scope."
)


# ---- provider calls (server-side) --------------------------------------------

class AIError(RuntimeError):
    pass


# Newer models reject the `temperature` param outright (a 400, not a warning) because they
# manage sampling internally: the Claude 5 family (claude-<tier>-5…) and OpenAI's o-series
# reasoning models. We omit it for those, and additionally retry once without it if any other
# model turns out to reject it — so a future model launch never hard-fails every AI call.
_TEMPERATURE_FREE_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-5(?:$|[-.\d])")


def _model_omits_temperature(model: Optional[str]) -> bool:
    m = (model or "").lower()
    return bool(_TEMPERATURE_FREE_RE.search(m) or re.match(r"o[1-9]", m))


def _temperature_rejected(resp: Any, body: Dict[str, Any]) -> bool:
    return resp.status_code == 400 and "temperature" in body and "temperature" in resp.text.lower()


async def _call_anthropic(api_key: str, model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system,
                            "messages": [{"role": "user", "content": user}]}
    if not _model_omits_temperature(model):
        body["temperature"] = temperature
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=body)
            if _temperature_rejected(resp, body):
                body.pop("temperature", None)
                resp = await client.post(url, headers=headers, json=body)
    except Exception as e:  # network
        raise AIError(f"Anthropic request failed: {e}")
    if resp.status_code != 200:
        raise AIError(f"Anthropic error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    parts = data.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    usage = data.get("usage") or {}
    return text, {"input_tokens": int(usage.get("input_tokens", 0)), "output_tokens": int(usage.get("output_tokens", 0))}


async def _call_openai(api_key: str, model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    body: Dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if not _model_omits_temperature(model):
        body["temperature"] = temperature
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=body)
            if _temperature_rejected(resp, body):
                body.pop("temperature", None)
                resp = await client.post(url, headers=headers, json=body)
    except Exception as e:
        raise AIError(f"OpenAI request failed: {e}")
    if resp.status_code != 200:
        raise AIError(f"OpenAI error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    usage = data.get("usage") or {}
    return text, {"input_tokens": int(usage.get("prompt_tokens", 0)), "output_tokens": int(usage.get("completion_tokens", 0))}


# Indirection so tests can inject a mock without a network / real key. Providers are
# async so a slow LLM never blocks a Starlette threadpool thread (the endpoints are
# async and await these directly).
_PROVIDERS = {"anthropic": _call_anthropic, "openai": _call_openai}


# Curated fallback lists — shown when no key is set, or the live /models call
# fails. Kept current with releases; the live fetch below supersedes these when a
# key is configured, so newly launched models appear without a code change.
CURATED_MODELS = {
    "anthropic": ["claude-opus-4-1", "claude-sonnet-4-5", "claude-sonnet-5", "claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o3-mini", "o1"],
}


async def _fetch_live_models_anthropic(api_key: str) -> List[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get("https://api.anthropic.com/v1/models",
                                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", []) if m.get("id")]


async def _fetch_live_models_openai(api_key: str) -> List[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {api_key}"})
    resp.raise_for_status()
    # Keep only chat-capable GPT/o-series models; drop embeddings/tts/whisper/etc.
    ids = [m["id"] for m in resp.json().get("data", []) if m.get("id")]
    return sorted(m for m in ids if (m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"))
                  and not any(x in m for x in ("audio", "realtime", "transcribe", "tts", "embedding", "moderation", "search")))


_LIVE_FETCHERS = {"anthropic": _fetch_live_models_anthropic, "openai": _fetch_live_models_openai}


async def list_models(provider: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Return the selectable models for a provider. Live-fetches from the provider's
    /models API when a key is available (so new launches show up automatically),
    falling back to the curated list on any error or when no key is set."""
    provider = (provider or "anthropic").lower()
    curated = CURATED_MODELS.get(provider, [])
    default = DEFAULT_MODELS.get(provider, curated[0] if curated else "")
    if not api_key:
        return {"provider": provider, "models": curated, "default": default, "live": False}
    try:
        live = await _LIVE_FETCHERS[provider](api_key)
        # Merge: keep curated ordering first (recommended), then any extra live ids.
        merged = list(dict.fromkeys([*curated, *live])) if live else curated
        return {"provider": provider, "models": merged, "default": default, "live": bool(live)}
    except Exception as exc:  # network / auth / rate-limit -> curated fallback
        return {"provider": provider, "models": curated, "default": default, "live": False, "error": str(exc)[:200]}


# Approximate list prices in USD per 1M tokens (input, output), matched by model-name
# substring. The customer pays their provider directly (BYO-key); this is an ESTIMATE
# for visibility + budget guardrails, not a bill. Unknown models fall back to a
# mid-range default so cost is never silently zero.
_PRICING = {
    "anthropic": [
        ("opus", (15.0, 75.0)), ("haiku", (0.80, 4.0)), ("sonnet", (3.0, 15.0)),
    ],
    "openai": [
        ("gpt-4o-mini", (0.15, 0.60)), ("gpt-4o", (2.50, 10.0)), ("o3-mini", (1.10, 4.40)),
        ("o1", (15.0, 60.0)), ("o3", (10.0, 40.0)), ("gpt-4", (10.0, 30.0)),
    ],
}
_DEFAULT_RATE = (3.0, 15.0)


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Rough USD estimate for a call, matched by model-name substring."""
    table = _PRICING.get((provider or "").lower(), [])
    model_l = (model or "").lower()
    in_rate, out_rate = next((rates for key, rates in table if key in model_l), _DEFAULT_RATE)
    return round((input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate, 6)


# The host registers a recorder so ai.py stays storage-decoupled; complete() calls it
# with (provider, model, usage) after every successful completion.
_USAGE_RECORDER: Optional[Any] = None


def set_usage_recorder(fn: Any) -> None:
    global _USAGE_RECORDER
    _USAGE_RECORDER = fn


async def complete(provider: str, api_key: str, system: str, user: str, model: Optional[str] = None,
                   max_tokens: int = 1500, temperature: float = 0.2) -> str:
    provider = (provider or "anthropic").lower()
    fn = _PROVIDERS.get(provider)
    if not fn:
        raise AIError(f"Unsupported provider: {provider}")
    if not api_key:
        raise AIError("No API key configured for this provider.")
    resolved_model = model or DEFAULT_MODELS[provider]
    result = await fn(api_key, resolved_model, system, user, max_tokens, temperature)
    # Providers return (text, usage); legacy/mocked ones may return a bare string.
    if isinstance(result, tuple):
        text, usage = result
    else:
        text, usage = result, None
    if usage and _USAGE_RECORDER is not None:
        try:
            _USAGE_RECORDER(provider, resolved_model, usage)
        except Exception:  # pragma: no cover - usage accounting must never fail a call
            pass
    return text


async def test_connection(provider: str, api_key: str, model: Optional[str] = None) -> Dict[str, Any]:
    """A tiny call to confirm the key works. ~1 token out."""
    try:
        out = await complete(provider, api_key, "You are a connectivity probe.", "Reply with the single word: ok",
                             model=model, max_tokens=5, temperature=0)
        return {"ok": True, "provider": provider, "model": model or DEFAULT_MODELS[provider], "sample": out[:40]}
    except AIError as e:
        return {"ok": False, "error": str(e)}


# ---- JSON extraction ---------------------------------------------------------

def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an LLM response (handles ``` fences)."""
    if not text:
        raise AIError("Empty response from the model.")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = min([i for i in (candidate.find("{"), candidate.find("[")) if i != -1] or [-1])
    if start == -1:
        raise AIError("Model did not return JSON.")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(candidate)):
        c = candidate[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(candidate[start:i + 1])
    return json.loads(candidate[start:])


# ---- feature: NL -> rule tree -------------------------------------------------

_RULE_SYSTEM = """You are RuleMind's policy copilot. You translate a plain-English
credit/risk decisioning requirement into a RuleMind rule TREE (JSON), using ONLY the
variables provided. Output STRICT JSON, no prose.

Tree schema:
- group node: {"type":"group","logic":"AND"|"OR","children":[...],"onPass":"approve"|"review"|"reject"|"pass","onFail":"approve"|"review"|"reject"}
- condition node: {"type":"condition","variable":"<one of the given variable ids>","operator":"=="|"!="|">"|">="|"<"|"<="|"between"|"in"|"not_in"|"regex"|"exists"|"!exists","value":<number|string>,"value2":<number, only for between>}
Rules:
- Use ONLY variable ids from the provided list. Never invent variables.
- Prefer a single top-level group. Nest at most 3 levels.
- Return: {"name": "<short name>", "tree": <group node>}
"""


async def generate_rule(provider: str, api_key: str, prompt: str, variables: List[Dict[str, Any]], model: Optional[str] = None) -> Dict[str, Any]:
    var_lines = "\n".join(f"- {v.get('id')} ({v.get('name','')})" for v in variables[:120])
    user = f"Available variables:\n{var_lines}\n\nRequirement:\n{prompt}\n\nReturn the JSON now."
    raw = await complete(provider, api_key, _RULE_SYSTEM, user, model=model, max_tokens=1500, temperature=0.1)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or "tree" not in obj:
        raise AIError("Model output was not a valid rule object.")
    return obj


# ---- feature: NL -> policy steps ---------------------------------------------

_POLICY_SYSTEM = """You are RuleMind's policy copilot. Translate a plain-English
decisioning requirement into a RuleMind POLICY: an ordered list of steps, using ONLY
the connector/rule/scorecard ids provided. Output STRICT JSON, no prose.

Each step: {"id":"s1","type":"connector"|"rule"|"scorecard"|"outcome","ref_id":"<id, omit for outcome>","label":"<short>","outcome":"approve"|"review"|"reject" (only for type outcome)}
Rules:
- Use ONLY ids from the provided lists. Never invent ids.
- Typical order: pull connector(s) → evaluate rule(s)/scorecard(s) → end with an outcome step.
- Return: {"name":"<short>","steps":[...]}
"""


async def generate_policy(provider: str, api_key: str, prompt: str, connectors: List[Dict[str, Any]],
                    rules: List[Dict[str, Any]], scorecards: List[Dict[str, Any]], model: Optional[str] = None) -> Dict[str, Any]:
    def ids(items):
        return "\n".join(f"- {i.get('id')} ({i.get('name','')})" for i in items[:80]) or "  (none)"
    user = (f"Connectors:\n{ids(connectors)}\n\nRules:\n{ids(rules)}\n\nScorecards:\n{ids(scorecards)}\n\n"
            f"Requirement:\n{prompt}\n\nReturn the JSON now.")
    raw = await complete(provider, api_key, _POLICY_SYSTEM, user, model=model, max_tokens=1800, temperature=0.1)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list):
        raise AIError("Model output was not a valid policy object.")
    return obj


# ---- feature: explain a decision (plain English + reason codes) --------------

_EXPLAIN_SYSTEM = """You are RuleMind's decision explainer for a credit/risk team.
Given a decision's outcome and the conditions that were evaluated (each with pass/fail,
the variable, operator, threshold, and actual value), explain in plain English WHY the
outcome happened. Then list concise adverse-action style reason codes for any FAILED
conditions (the drivers of a decline/review). Be factual, reference the numbers, and do
NOT invent conditions. Output STRICT JSON: {"summary":"<2-4 sentences>","reason_codes":["<short reason>", ...]}"""


async def explain_decision(provider: str, api_key: str, decision: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    lines: List[str] = [f"Outcome: {decision.get('outcome')}"]
    for step in decision.get("trace") or []:
        s = step.get("step") or {}
        if s.get("type") == "rule":
            for c in ((step.get("result") or {}).get("conditions") or []):
                status = "PASS" if c.get("passed") else "FAIL"
                lines.append(f"[{status}] {c.get('variable_name')} {c.get('operator')} {c.get('threshold')} (actual={c.get('value')})")
        elif s.get("type") == "scorecard":
            res = step.get("result") or {}
            if "score" in res:
                lines.append(f"Scorecard {s.get('label') or s.get('ref_id')}: score={res.get('score')}")
    user = "Decision evidence:\n" + "\n".join(lines[:120]) + "\n\nReturn the JSON now."
    raw = await complete(provider, api_key, _EXPLAIN_SYSTEM, user, model=model, max_tokens=800, temperature=0.2)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or "summary" not in obj:
        raise AIError("Model output was not a valid explanation.")
    obj.setdefault("reason_codes", [])
    return obj


# ---- feature: NL/structured definition -> a scorecard "predictor" ------------

_PREDICTOR_SYSTEM = """You are RuleMind's predictor copilot. Turn a plain-English predictor definition
into a RuleMind SCORECARD (a points-based predictor over existing variables). Output STRICT JSON, no
prose. Use ONLY the provided variable ids.

Shape: {"name":"<short>","base_score":<int>,"max_score":<int>,"bins":[
  {"variable_id":"<id>","weight":1.0,"ranges":[{"min":<num>,"max":<num>,"points":<int>}, ...]}, ...]}
Rules:
- Higher points = lower risk (or as the definition specifies). Cover the plausible value range per
  variable with non-overlapping ranges. Never invent variable ids. Keep it to <= 8 bins.
"""


async def generate_predictor(provider: str, api_key: str, definition: str, variables: List[Dict[str, Any]],
                             model: Optional[str] = None) -> Dict[str, Any]:
    var_lines = "\n".join(f"- {v.get('id')} ({v.get('name','')}, {v.get('category','')})" for v in variables[:120])
    user = f"Available variables:\n{var_lines}\n\nPredictor definition:\n{definition}\n\nReturn the JSON now."
    raw = await complete(provider, api_key, _PREDICTOR_SYSTEM, user, model=model, max_tokens=1800, temperature=0.1)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("bins"), list) or not obj["bins"]:
        raise AIError("Model output was not a valid predictor/scorecard object.")
    obj.setdefault("name", "AI predictor")
    obj.setdefault("base_score", 300)
    obj.setdefault("max_score", 900)
    return obj


# ---- feature: analyze a champion/challenger experiment -----------------------

_EXPERIMENT_SYSTEM = """You are RuleMind's experimentation analyst. Given a champion/challenger
experiment's per-variant results (traffic, decision counts, outcome mix, avg score/latency), give a
crisp read and a decision. Be quantitative and reference the numbers; never invent data. Output STRICT
JSON: {"summary":"<3-5 sentences>","recommendation":"promote"|"hold"|"rollback","winning_variant":
"<variant id or ''>","rationale":"<why, referencing the metrics>","cautions":["<short>", ...]}"""


async def analyze_experiment(provider: str, api_key: str, experiment: Dict[str, Any], results: Dict[str, Any],
                             model: Optional[str] = None) -> Dict[str, Any]:
    import json as _json
    user = (f"Experiment: {experiment.get('name')} (hash_key={experiment.get('hash_key')}, "
            f"status={experiment.get('status')})\nVariants: {_json.dumps(experiment.get('variants', []))[:1500]}\n\n"
            f"Results:\n{_json.dumps(results)[:3500]}\n\nReturn the JSON now.")
    raw = await complete(provider, api_key, _EXPERIMENT_SYSTEM, user, model=model, max_tokens=900, temperature=0.2)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or "recommendation" not in obj:
        raise AIError("Model output was not a valid experiment analysis.")
    obj.setdefault("summary", "")
    obj.setdefault("cautions", [])
    return obj


# ---- feature: explain WHY rejections went up in a policy ---------------------

_REJECTION_SYSTEM = """You are RuleMind's decision-quality analyst. Given the top drivers of DECLINE/
REVIEW outcomes for a policy (each: variable, operator, threshold, how often it failed, and the recent
vs baseline rejection rate), explain in plain English the MAIN reasons rejections changed, ranked by
impact, and suggest concrete, safe next steps. Be factual and reference the numbers; do not invent
drivers. Output STRICT JSON: {"summary":"<3-5 sentences>","top_reasons":[{"driver":"<short>","impact":
"<why it matters + the number>"}, ...],"recommendations":["<short, actionable>", ...]}"""


async def analyze_rejections(provider: str, api_key: str, policy_name: str, drivers: Any,
                             model: Optional[str] = None) -> Dict[str, Any]:
    import json as _json
    user = (f"Policy: {policy_name}\n\nRejection drivers (most impactful first):\n"
            f"{_json.dumps(drivers)[:3500]}\n\nReturn the JSON now.")
    raw = await complete(provider, api_key, _REJECTION_SYSTEM, user, model=model, max_tokens=900, temperature=0.2)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or "top_reasons" not in obj:
        raise AIError("Model output was not a valid rejection analysis.")
    obj.setdefault("summary", "")
    obj.setdefault("recommendations", [])
    return obj
