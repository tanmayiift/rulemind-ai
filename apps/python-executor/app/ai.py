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


def _call_anthropic(api_key: str, model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      "system": system, "messages": [{"role": "user", "content": user}]},
            )
    except Exception as e:  # network
        raise AIError(f"Anthropic request failed: {e}")
    if resp.status_code != 200:
        raise AIError(f"Anthropic error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    parts = data.get("content") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


def _call_openai(api_key: str, model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            )
    except Exception as e:
        raise AIError(f"OpenAI request failed: {e}")
    if resp.status_code != 200:
        raise AIError(f"OpenAI error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


# Indirection so tests can inject a mock without a network / real key.
_PROVIDERS = {"anthropic": _call_anthropic, "openai": _call_openai}


def complete(provider: str, api_key: str, system: str, user: str, model: Optional[str] = None,
             max_tokens: int = 1500, temperature: float = 0.2) -> str:
    provider = (provider or "anthropic").lower()
    fn = _PROVIDERS.get(provider)
    if not fn:
        raise AIError(f"Unsupported provider: {provider}")
    if not api_key:
        raise AIError("No API key configured for this provider.")
    return fn(api_key, model or DEFAULT_MODELS[provider], system, user, max_tokens, temperature)


def test_connection(provider: str, api_key: str, model: Optional[str] = None) -> Dict[str, Any]:
    """A tiny call to confirm the key works. ~1 token out."""
    try:
        out = complete(provider, api_key, "You are a connectivity probe.", "Reply with the single word: ok",
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


def generate_rule(provider: str, api_key: str, prompt: str, variables: List[Dict[str, Any]], model: Optional[str] = None) -> Dict[str, Any]:
    var_lines = "\n".join(f"- {v.get('id')} ({v.get('name','')})" for v in variables[:120])
    user = f"Available variables:\n{var_lines}\n\nRequirement:\n{prompt}\n\nReturn the JSON now."
    raw = complete(provider, api_key, _RULE_SYSTEM, user, model=model, max_tokens=1500, temperature=0.1)
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


def generate_policy(provider: str, api_key: str, prompt: str, connectors: List[Dict[str, Any]],
                    rules: List[Dict[str, Any]], scorecards: List[Dict[str, Any]], model: Optional[str] = None) -> Dict[str, Any]:
    def ids(items):
        return "\n".join(f"- {i.get('id')} ({i.get('name','')})" for i in items[:80]) or "  (none)"
    user = (f"Connectors:\n{ids(connectors)}\n\nRules:\n{ids(rules)}\n\nScorecards:\n{ids(scorecards)}\n\n"
            f"Requirement:\n{prompt}\n\nReturn the JSON now.")
    raw = complete(provider, api_key, _POLICY_SYSTEM, user, model=model, max_tokens=1800, temperature=0.1)
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


def explain_decision(provider: str, api_key: str, decision: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
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
    raw = complete(provider, api_key, _EXPLAIN_SYSTEM, user, model=model, max_tokens=800, temperature=0.2)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or "summary" not in obj:
        raise AIError("Model output was not a valid explanation.")
    obj.setdefault("reason_codes", [])
    return obj
