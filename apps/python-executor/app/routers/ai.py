"""AI + provider-template endpoints — workflow provider library, AI config/usage/budget, model
listing, connection test, and NL→draft rule/policy generation + decision explanation. Extracted
verbatim from app/main.py. Stable helpers imported by value from app.main; direct storage calls use
main.storage live."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import main
from ..logic import find_by_id
from ..main import ensure_exists, validate_policy_steps, validate_rule_tree

router = APIRouter()


# ── Provider templates (workflow API steps) ────────────────────────────────
@router.get("/api/v1/providers")
def list_provider_templates(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Built-in provider action templates for workflow API steps."""
    from ..providers import list_providers

    return list_providers(category)


@router.get("/api/v1/providers/{provider_id}")
def get_provider_template(provider_id: str) -> Dict[str, Any]:
    from ..providers import get_provider

    return ensure_exists(get_provider(provider_id), "provider", provider_id)


# ── AI config / usage / budget ─────────────────────────────────────────────
class AIConfigRequest(BaseModel):
    default_provider: Optional[str] = None
    anthropic: Optional[Dict[str, Any]] = None  # {"model": "...", "key": "sk-..."} ("__CLEAR__" to remove)
    openai: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None  # admin master switch (AI still needs a key to be "on")


class AITestRequest(BaseModel):
    provider: Optional[str] = None


class AIGenerateRuleRequest(BaseModel):
    prompt: str
    provider: Optional[str] = None


class AIBudgetRequest(BaseModel):
    monthly_budget_usd: float = 0.0


class AIGeneratePolicyRequest(BaseModel):
    prompt: str
    provider: Optional[str] = None


class AIExplainRequest(BaseModel):
    decision_id: str
    provider: Optional[str] = None


class AIPredictorRequest(BaseModel):
    definition: str
    provider: Optional[str] = None


class AIExperimentAnalysisRequest(BaseModel):
    experiment_id: str
    provider: Optional[str] = None


class AIRejectionAnalysisRequest(BaseModel):
    policy_id: Optional[str] = None  # None = across all policies
    limit: int = 500
    provider: Optional[str] = None


@router.get("/api/v1/ai/config")
def get_ai_config() -> Dict[str, Any]:
    """Masked view — reports which providers are configured, never returns keys."""
    return main.storage.get_ai_config_masked()


@router.put("/api/v1/ai/config")
def put_ai_config(request: AIConfigRequest) -> Dict[str, Any]:
    return main.storage.set_ai_config(request.model_dump(exclude_none=True))


@router.get("/api/v1/ai/usage")
def get_ai_usage() -> Dict[str, Any]:
    """Per-workspace AI token + estimated-cost accounting, plus the budget cap.
    Costs are estimates (BYO-key: the customer is billed by their provider)."""
    return main.storage.get_ai_usage()


@router.put("/api/v1/ai/budget")
def set_ai_budget(request: AIBudgetRequest) -> Dict[str, Any]:
    """Set a monthly estimated-cost cap; AI generation is blocked once exceeded
    (0 = no cap)."""
    if request.monthly_budget_usd < 0:
        raise HTTPException(status_code=422, detail="Budget must be >= 0.")
    return main.storage.set_ai_budget(request.monthly_budget_usd)


@router.post("/api/v1/ai/usage/reset")
def reset_ai_usage() -> Dict[str, Any]:
    """Reset the accumulated usage counters (e.g. at the start of a billing month)."""
    main.storage.reset_ai_usage()
    return main.storage.get_ai_usage()


def _enforce_ai_budget() -> None:
    """Block an AI generation call when the workspace is over its estimated-cost cap."""
    usage = main.storage.get_ai_usage()
    if usage.get("over_budget"):
        raise HTTPException(
            status_code=402,
            detail="AI budget of ${0:.2f} reached (estimated spend ${1:.2f}). Raise the budget in Settings or reset usage.".format(
                usage.get("budget_usd", 0), usage.get("cost_usd", 0)),
        )


@router.get("/api/v1/ai/models")
async def list_ai_models(provider: str = Query(default="anthropic")) -> Dict[str, Any]:
    """Selectable models for a provider. Live-fetches from the provider's /models
    API when a key is configured (so new model launches appear automatically),
    else returns the curated list."""
    from ..ai import list_models

    creds = main.storage.get_ai_credentials(provider)
    api_key = creds.get("api_key") if creds else None
    return await list_models(provider, api_key)


@router.post("/api/v1/ai/test")
async def ai_test(request: AITestRequest) -> Dict[str, Any]:
    from ..ai import test_connection

    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No API key configured for that provider.")
    return await test_connection(creds["provider"], creds["api_key"], creds.get("model"))


# ── AI generation (NL → draft; always validated + test-gated before promotion) ──
@router.post("/api/v1/ai/generate-rule")
async def ai_generate_rule(request: AIGenerateRuleRequest) -> Dict[str, Any]:
    """NL → draft rule tree. Guardrails: an out-of-scope prompt is refused LOCALLY
    (no token spent), and the result is a DRAFT that still passes MECE/test-gating
    before it can be promoted — nothing is saved or deployed here."""
    from ..ai import AIError, OUT_OF_SCOPE_MESSAGE, generate_rule, is_in_scope

    variables = main.storage.list_variables()
    names = [v.get("name", "") for v in variables] + [v.get("id", "") for v in variables]
    in_scope, reason = is_in_scope(request.prompt, names)
    if not in_scope:
        return {"in_scope": False, "reason": reason, "message": OUT_OF_SCOPE_MESSAGE}

    _enforce_ai_budget()
    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No AI provider configured — add a key in AI settings.")
    try:
        draft = await generate_rule(creds["provider"], creds["api_key"], request.prompt, variables, model=creds.get("model"))
    except AIError as error:
        raise HTTPException(status_code=502, detail=str(error))

    # Validate the generated tree as a draft (not saved).
    valid, validation_error = True, None
    try:
        if isinstance(draft.get("tree"), dict):
            validate_rule_tree(draft["tree"])
        else:
            valid, validation_error = False, "Model did not return a tree."
    except HTTPException as error:
        valid, validation_error = False, error.detail
    return {"in_scope": True, "provider": creds["provider"], "draft": draft, "valid": valid, "validation_error": validation_error}


@router.post("/api/v1/ai/generate-policy")
async def ai_generate_policy(request: AIGeneratePolicyRequest) -> Dict[str, Any]:
    """NL → draft policy steps (draft only; still validated + test-gated before promotion)."""
    from ..ai import AIError, OUT_OF_SCOPE_MESSAGE, generate_policy, is_in_scope

    connectors = main.storage.list_connectors()
    rules = main.storage.list_rules()
    scorecards = main.storage.list_scorecards()
    names = [x.get("name", "") for x in connectors + rules + scorecards]
    in_scope, reason = is_in_scope(request.prompt, names)
    if not in_scope:
        return {"in_scope": False, "reason": reason, "message": OUT_OF_SCOPE_MESSAGE}
    _enforce_ai_budget()
    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No AI provider configured — add a key in AI settings.")
    try:
        draft = await generate_policy(creds["provider"], creds["api_key"], request.prompt, connectors, rules, scorecards, model=creds.get("model"))
    except AIError as error:
        raise HTTPException(status_code=502, detail=str(error))
    valid, validation_error = True, None
    try:
        validate_policy_steps(draft.get("steps") or [])
    except HTTPException as error:
        valid, validation_error = False, error.detail
    return {"in_scope": True, "provider": creds["provider"], "draft": draft, "valid": valid, "validation_error": validation_error}


@router.post("/api/v1/ai/explain-decision")
async def ai_explain_decision(request: AIExplainRequest) -> Dict[str, Any]:
    """Plain-English explanation + adverse-action reason codes for one decision."""
    from ..ai import AIError, explain_decision

    decision = find_by_id(main.storage.list_decisions(limit=1000), request.decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found.")
    _enforce_ai_budget()
    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No AI provider configured — add a key in AI settings.")
    try:
        result = await explain_decision(creds["provider"], creds["api_key"], decision, model=creds.get("model"))
    except AIError as error:
        raise HTTPException(status_code=502, detail=str(error))
    return {"decision_id": request.decision_id, "outcome": decision.get("outcome"), **result}


@router.post("/api/v1/ai/generate-predictor")
async def ai_generate_predictor(request: AIPredictorRequest) -> Dict[str, Any]:
    """Definition -> draft SCORECARD predictor over existing variables. Like generate-rule/policy:
    off-topic is refused locally (no token spent) and the result is a DRAFT — it is validated and
    must still be test-gated before it is saved/promoted. Nothing is created here."""
    from ..ai import AIError, OUT_OF_SCOPE_MESSAGE, generate_predictor, is_in_scope

    variables = main.storage.list_variables()
    names = [v.get("name", "") for v in variables] + [v.get("id", "") for v in variables]
    in_scope, reason = is_in_scope(request.definition, names)
    if not in_scope:
        return {"in_scope": False, "reason": reason, "message": OUT_OF_SCOPE_MESSAGE}
    if not variables:
        raise HTTPException(status_code=422, detail="No variables exist yet — create variables before generating a predictor.")
    _enforce_ai_budget()
    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No AI provider configured — add a key in AI settings.")
    try:
        draft = await generate_predictor(creds["provider"], creds["api_key"], request.definition, variables, model=creds.get("model"))
    except AIError as error:
        raise HTTPException(status_code=502, detail=str(error))

    # Draft-validate: every referenced variable id must exist (the model is told to use only real ids,
    # but we never trust that — an unknown id would produce a silently-broken scorecard).
    known = {v.get("id") for v in variables}
    unknown = sorted({b.get("variable_id") for b in draft.get("bins", []) if b.get("variable_id") not in known})
    valid = not unknown
    validation_error = None if valid else "References unknown variable id(s): {0}".format(", ".join(unknown))
    return {"in_scope": True, "provider": creds["provider"], "draft": draft, "valid": valid, "validation_error": validation_error}


@router.post("/api/v1/ai/analyze-experiment")
async def ai_analyze_experiment(request: AIExperimentAnalysisRequest, http_request: Request) -> Dict[str, Any]:
    """Read a champion/challenger experiment's live results and return a quantitative promote/hold/
    rollback recommendation. The metrics are computed server-side (no LLM); the LLM only interprets."""
    from ..ai import AIError, analyze_experiment
    from ..analytics import experiment_analytics

    experiment = main.storage.get_experiment(request.experiment_id, tenant_id=main.active_tenant_id(http_request))
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    try:
        results = experiment_analytics(main.storage, main.active_tenant_id(http_request), request.experiment_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _enforce_ai_budget()
    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No AI provider configured — add a key in AI settings.")
    try:
        analysis = await analyze_experiment(creds["provider"], creds["api_key"], experiment, results, model=creds.get("model"))
    except AIError as error:
        raise HTTPException(status_code=502, detail=str(error))
    return {"experiment_id": request.experiment_id, "provider": creds["provider"], "results": results, **analysis}


@router.post("/api/v1/ai/analyze-rejections")
async def ai_analyze_rejections(request: AIRejectionAnalysisRequest) -> Dict[str, Any]:
    """Explain WHY rejections changed for a policy. The drivers are computed server-side from the
    decision log (pure compute, same as /analytics/rejection-drivers); the LLM only interprets them."""
    from ..ai import AIError, analyze_rejections
    from ..analytics import rejection_drivers

    decisions = main.storage.list_decisions(limit=max(1, min(request.limit, 1000)))
    policy_name = "all policies"
    if request.policy_id:
        policy = main.storage.get_policy(request.policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="Policy not found.")
        policy_name = policy.get("name") or request.policy_id
        decisions = [d for d in decisions if d.get("policy_id") == request.policy_id]
    drivers = rejection_drivers(decisions)
    # focus_count = number of decline/review decisions. The drivers list is non-empty even when all
    # decisions approved (it lists every condition seen), so gate on focus_count — no declines means
    # there is nothing to explain, and we must not spend a token on it.
    if not drivers.get("focus_count"):
        return {"policy_id": request.policy_id, "policy_name": policy_name, "drivers": drivers,
                "summary": "No declines/reviews in the sampled decisions — nothing to analyze yet.",
                "top_reasons": [], "recommendations": []}
    _enforce_ai_budget()
    creds = main.storage.get_ai_credentials(request.provider)
    if not creds:
        raise HTTPException(status_code=422, detail="No AI provider configured — add a key in AI settings.")
    try:
        analysis = await analyze_rejections(creds["provider"], creds["api_key"], policy_name, drivers, model=creds.get("model"))
    except AIError as error:
        raise HTTPException(status_code=502, detail=str(error))
    return {"policy_id": request.policy_id, "policy_name": policy_name, "provider": creds["provider"], "drivers": drivers, **analysis}
