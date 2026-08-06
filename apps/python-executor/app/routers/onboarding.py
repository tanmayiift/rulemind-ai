"""Self-serve onboarding endpoints — signup, status, activation checklist, load-samples, verify,
AI opt-in, request-prod. Extracted verbatim from app/main.py. Models imported by value from
app.main; direct storage calls use main.storage live."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from .. import main
from ..main import OnboardingAIRequest, OnboardingSignupRequest, active_tenant_id, maybe_compile_bundle

router = APIRouter()


def _onboarding_view(tenant_id: str) -> Dict[str, Any]:
    ob = main.storage.get_onboarding(tenant_id=tenant_id)
    ai_cfg = main.storage.get_ai_config_masked(tenant_id=tenant_id)
    ai_configured = any(p.get("configured") for p in ai_cfg.get("providers", {}).values())
    decision_count = main.storage.count_decisions(tenant_id=tenant_id)
    steps = {
        "org": bool(ob.get("org")),
        "dev_key": bool(ob.get("dev_key_issued")),
        "verified": bool(ob.get("verified")) or decision_count > 0,
        "ai": bool(ob.get("ai_choice_made")) or ai_configured,
        "prod_key": bool(ob.get("prod_key_issued")),
    }
    return {
        "onboarding": ob,
        "ai_configured": ai_configured,
        "decision_count": decision_count,
        "steps": steps,
        "complete": all(steps[k] for k in ("org", "dev_key", "verified", "ai", "prod_key")),
    }


@router.post("/api/v1/onboarding/signup")
def onboarding_signup(request: OnboardingSignupRequest) -> Dict[str, Any]:
    """Public: create a workspace + issue a DEV key (shown once). Prod key comes later,
    after the client verifies their integration. (In production, gate this behind email
    verification / CAPTCHA — it is intentionally open here for self-serve onboarding.)"""
    tenant = main.storage.create_tenant(name=request.company, plan=request.plan,
                                        config={"onboarding": {"org": {"company": request.company, "contact_email": request.contact_email, "use_case": request.use_case}}})
    main.storage.seed_sample_inventory(tenant["id"])  # so they can try a decision immediately
    key = main.storage.generate_api_key_for_tenant(tenant["id"], environment="dev", label="Onboarding dev key")
    main.storage.update_onboarding({"dev_key_issued": True}, tenant_id=tenant["id"])
    return {"tenant_id": tenant["id"], "company": request.company,
            "api_key": key["plaintext"], "environment": "dev",
            "status": _onboarding_view(tenant["id"])}


@router.get("/api/v1/onboarding/status")
def onboarding_status() -> Dict[str, Any]:
    return _onboarding_view(active_tenant_id())


@router.get("/api/v1/onboarding/activation")
def onboarding_activation() -> Dict[str, Any]:
    """Build-first activation checklist with live completion — connect a source →
    create a variable → author a rule → assemble a policy → run a decision. Powers
    the guided onboarding for a clean (unseeded) workspace."""
    tid = active_tenant_id()
    counts = {
        "connectors": len(main.storage.list_connectors(tenant_id=tid)),
        "variables": len(main.storage.list_variables(tenant_id=tid)),
        "rules": len(main.storage.list_rules(tenant_id=tid)),
        "policies": len(main.storage.list_policies(tenant_id=tid)),
        "decisions": main.storage.count_decisions(tenant_id=tid),
    }
    steps = [
        {"key": "connector", "label": "Connect a data source", "href": "/connectors",
         "hint": "Add the payload your decisions read from (bureau, bank, KYC, or a custom API).", "count": counts["connectors"], "done": counts["connectors"] > 0},
        {"key": "variable", "label": "Create a variable", "href": "/variables",
         "hint": "Compute a feature from that payload (e.g. a score or ratio) in sandboxed Python.", "count": counts["variables"], "done": counts["variables"] > 0},
        {"key": "rule", "label": "Author a rule", "href": "/rules",
         "hint": "Gate on your variables — approve / review / reject.", "count": counts["rules"], "done": counts["rules"] > 0},
        {"key": "policy", "label": "Assemble a policy", "href": "/workflow-builder",
         "hint": "Chain connectors, variables, rules and scorecards into a decision flow.", "count": counts["policies"], "done": counts["policies"] > 0},
        {"key": "decision", "label": "Run your first decision", "href": "/simulation",
         "hint": "Send a payload through the policy and inspect the full trace.", "count": counts["decisions"], "done": counts["decisions"] > 0},
    ]
    completed = sum(1 for s in steps if s["done"])
    return {"steps": steps, "completed": completed, "total": len(steps),
            "activated": completed == len(steps), "has_data": counts["connectors"] > 0 or counts["policies"] > 0}


@router.post("/api/v1/onboarding/load-samples")
def onboarding_load_samples() -> Dict[str, Any]:
    """Load the sample lending inventory into this workspace so the user can explore
    before building their own (the fresh-clone default is a clean workspace)."""
    main.storage.seed_sample_inventory(active_tenant_id())
    maybe_compile_bundle(active_tenant_id())
    return onboarding_activation()


@router.post("/api/v1/onboarding/verify")
def onboarding_verify() -> Dict[str, Any]:
    """Mark the integration verified once the workspace has made its first decision."""
    tenant_id = active_tenant_id()
    if main.storage.count_decisions(tenant_id=tenant_id) < 1:
        raise HTTPException(status_code=409, detail="Make at least one decision (POST /api/v1/decide) with your dev key first.")
    main.storage.update_onboarding({"verified": True}, tenant_id=tenant_id)
    return _onboarding_view(tenant_id)


@router.post("/api/v1/onboarding/ai")
def onboarding_ai(request: OnboardingAIRequest) -> Dict[str, Any]:
    """Record the AI opt-in decision. AI features stay hidden unless a key is set
    (via /ai/config); this just marks the choice so onboarding can proceed."""
    tenant_id = active_tenant_id()
    main.storage.update_onboarding({"ai_choice_made": True, "ai_opted_in": bool(request.opted_in)}, tenant_id=tenant_id)
    return _onboarding_view(tenant_id)


@router.post("/api/v1/onboarding/request-prod")
def onboarding_request_prod() -> Dict[str, Any]:
    """Issue the PROD key — only after the dev integration is verified."""
    tenant_id = active_tenant_id()
    view = _onboarding_view(tenant_id)
    if not view["steps"]["verified"]:
        raise HTTPException(status_code=409, detail="Verify your dev integration before requesting a production key.")
    if main.storage.get_onboarding(tenant_id=tenant_id).get("prod_key_issued"):
        raise HTTPException(status_code=409, detail="A production key was already issued for this workspace.")
    key = main.storage.generate_api_key_for_tenant(tenant_id, environment="prod", label="Production key")
    main.storage.update_onboarding({"prod_key_issued": True, "completed": True}, tenant_id=tenant_id)
    return {"api_key": key["plaintext"], "environment": "prod", "status": _onboarding_view(tenant_id)}
