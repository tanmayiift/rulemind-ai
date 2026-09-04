"""Runtime + config endpoints — the /decide hot path, test/simulation console, concurrent batch
(JSON + JSONL), async workflow callback + loop debug, deploy board, config export/import, settings,
bootstrap, and the live decision SSE stream. The last feature slice extracted from app/main.py.

Stable helpers/models/constants imported by value from app.main (they internally read the live
main.storage); direct storage calls use main.storage live. Sibling handlers (test_rule,
execute_policy_endpoint) are imported from their routers so batch simulation can delegate to them."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import decision_bus, main
from ..executor import ExecutionContext, PolicyExecutor
from ..logic import export_bundle, json_dumps
from ..main import (
    ALLOWED_NODE_TYPES,
    ALLOWED_OPERATORS,
    BatchSimulationRequest,
    DecideRequest,
    DeployPromoteRequest,
    ImportRequest,
    SettingsRequest,
    TestPayloadRequest,
    active_tenant_id,
    current_connectors,
    ensure_exists,
    payload_map,
    promote_entity,
    record_error,
    test_policy_entity,
    test_variable_entity,
    workflow_executor,
)
from ..storage import _parse_client_datetime
from .. import env

router = APIRouter()

# Max payload lines accepted per JSONL simulation upload (config so limits move without code).
SIMULATION_JSONL_MAX = env.env_int("SIMULATION_JSONL_MAX", 50000)


# ── Test / simulation console ──────────────────────────────────────────────
@router.post("/api/v1/test/variables")
def batch_test_variables(request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    payloads = payload_map(request.payload)
    results = []
    pass_count = 0
    for variable in main.storage.list_variables():
        outcome = test_variable_entity(variable, payloads)
        result = {
            "id": outcome["variable"]["id"],
            "name": outcome["variable"]["name"],
            "status": outcome["variable"]["status"],
            "category": outcome["variable"]["category"],
            "source_id": outcome["variable"]["source_id"],
            "source_icon": current_connectors().get(outcome["variable"]["source_id"], {}).get("icon"),
            "computed_value": outcome["result"]["value"],
            "latency_ms": outcome["result"]["latency_ms"],
            "passed": outcome["result"]["passed"],
            "badge": "PASS" if outcome["result"]["passed"] else "SIM",
            "error": outcome["result"]["error"],
        }
        pass_count += 1 if result["passed"] else 0
        results.append(result)
    return {"results": results, "summary": "{0}/{1} passed".format(pass_count, len(results))}


@router.post("/api/v1/test/batch")
def batch_simulation(request: BatchSimulationRequest) -> Dict[str, Any]:
    from .policies import execute_policy_endpoint
    from .rules import test_rule

    rows = []
    for index, payload in enumerate(request.payloads):
        if request.targetType == "variables":
            result = batch_test_variables(TestPayloadRequest(payload=payload))
        elif request.targetType == "rule":
            if not request.targetId:
                raise HTTPException(status_code=422, detail="targetId is required for rule batch simulation.")
            result = test_rule(request.targetId, TestPayloadRequest(payload=payload))
        elif request.targetType == "policy":
            if not request.resolved_target_id:
                raise HTTPException(status_code=422, detail="targetId is required for policy batch simulation.")
            result = execute_policy_endpoint(request.resolved_target_id, TestPayloadRequest(payload=payload))
        elif request.targetType == "decide":
            if not request.targetId:
                raise HTTPException(status_code=422, detail="targetId is required for decision batch simulation.")
            # policy_id (not policyId) is valid: DecideRequest sets populate_by_name=True
            # (main.py:269); verified at runtime. mypy reads only the alias because the
            # pydantic plugin is not enabled.
            result = decide(DecideRequest(policy_id=request.targetId, payload=payload))  # type: ignore[call-arg]
        else:
            raise HTTPException(status_code=422, detail="Unsupported batch targetType.")
        rows.append({"index": index, "payload": payload, "result": result})
    return {"targetType": request.targetType, "targetId": request.resolved_target_id, "rows": rows, "count": len(rows)}


def _enforce_quota() -> None:
    """Refuse a decision when the workspace is over its monthly allowance.

    No-op unless QUOTA_ENFORCE=1 -- see app/quota.py for why enforcement is opt-in while
    metering is always on.

    402, not 429. 429 means "you are going too fast" and every well-behaved HTTP client
    responds by backing off and RETRYING, which is exactly wrong here: no amount of waiting
    restores the allowance before the period rolls over, so the retry storm is pure waste and
    it makes a billing event indistinguishable from a burst in the logs. 402 Payment Required
    says the only thing that will fix this is a plan change.

    WHERE THIS IS AND IS NOT APPLIED, and why:
      * /api/v1/decide -- gated. It writes a Decision row, so it is the billable unit.
      * /api/v1/decide/batch and .../batch/jsonl -- NOT gated. Both run in simulation mode and
        log no decision (see their docstrings), so they are not billable and rejecting them
        would charge a customer's quota against what-if runs they were never billed for.
      * POST /sdk/v1/decisions -- metered but NOT gated, deliberately. Those decisions were
        already taken on the device; refusing the upload does not prevent the usage, it only
        destroys the audit record and strands the device's outbox in a retry loop it can never
        drain. Over-quota device volume is a billing conversation, not a data-loss event.
    """
    from .. import quota

    if not quota.enforcement_enabled():
        return
    tenant_id = active_tenant_id()
    tenant = main.storage.get_tenant(tenant_id)
    start, end = quota.period_bounds()
    used = main.storage.get_usage_total(quota.METRIC_DECISIONS, tenant_id=tenant_id, start=start, end=end)
    status = quota.evaluate(tenant, used)
    if status["exceeded"]:
        raise HTTPException(status_code=402, detail={
            "error": "quota_exceeded",
            "message": "Monthly decision quota exhausted for this workspace.",
            **{k: status[k] for k in ("plan", "limit", "used", "period_start", "period_end")},
        })


@router.post("/api/v1/decide")
def decide(request: DecideRequest) -> Dict[str, Any]:
    """Run a policy decision.

    `payload` accepts two INTENTIONALLY DISTINCT shapes:
      * flat fields — `{"bureau_score": 800}` — a per-field OVERRIDE applied on top of each source's
        sample; unspecified fields keep their sample value (handy for quick what-if testing).
      * a source-keyed dict — `{"loan": {"bureau_score": 800}}` — the AUTHORITATIVE, complete payload
        for that source; fields you omit are absent, not defaulted (use this for real integrations).
    So a partial flat and a partial nested of the same field can decide differently; send the full
    source payload under its key when you want an exact, reproducible decision.
    """
    _enforce_quota()
    policy = ensure_exists(main.storage.get_policy(request.policy_id), "policy", request.policy_id)
    # Scalable hot path: serve pure-compute policies from the cached bundle via the stateless
    # core (Rust when available), bypassing the heavy PolicyExecutor. fast_path_eligible is the
    # single authority on when this is safe (shape + no running experiment) so the two paths
    # can't drift.
    if os.getenv("FAST_DECIDE", "0") == "1":
        from ..fast_decide import fast_decide, fast_path_eligible

        if fast_path_eligible(main.storage, policy, active_tenant_id()):
            decision = fast_decide(main.storage, policy, request.payload or {}, active_tenant_id(), user_id=request.user_id)
            if decision["outcome"] == "reject":
                record_error("decisions", "decide", "Decision outcome rejected.", "policy", request.policy_id, {})
            return decision
    # source="api" so the executor logs this as a production decision (exactly one
    # Decision row); the fast path above logs source="api_fast", also once.
    outcome = test_policy_entity(policy, request.payload, source="api", user_id=request.user_id)
    if outcome["result"]["outcome"] == "reject":
        record_error("decisions", "decide", "Decision outcome rejected.", "policy", request.policy_id, {"trace": outcome["result"].get("trace", [])})
    return {
        "policy_id": policy["id"],
        # The id of the Decision row this call just wrote — lets a caller fetch
        # /decisions/{id}/reason-codes or deep-link the case without a second lookup.
        "decision_id": outcome["result"].get("decision_id"),
        "outcome": outcome["result"]["outcome"],
        "score": outcome["result"].get("scorecard_result", {}).get("score") if outcome["result"].get("scorecard_result") else None,
        "variables": outcome["values"],
        # A variable that failed to compute is None in "variables" above, and None is also a
        # perfectly legitimate value -- so without this map an integrator cannot tell a real
        # null from a bureau call that timed out. Empty on a healthy decision.
        "variable_errors": outcome.get("variable_errors") or {},
        "rule_results": [item for item in outcome["result"]["trace"] if item.get("step", {}).get("type") == "rule"],
        "scorecard_result": outcome["result"].get("scorecard_result"),
        "trace": outcome["result"]["trace"],
        "latency_ms": outcome["latency_ms"],
    }


class ActionTestRequest(BaseModel):
    action: Dict[str, Any]
    context: Optional[Dict[str, Any]] = None


@router.post("/api/v1/test/action")
def test_action(request: ActionTestRequest) -> Dict[str, Any]:
    """Postman-style console: resolve an `action` step's templates against a sample
    context and send it server-side (no browser CORS). Only console-supplied secrets
    are resolved — stored tenant secrets are never injected here, so nothing sensitive
    leaks back to the client."""
    import httpx

    from ..templates import resolve_template

    action = request.action or {}
    ctx = request.context or {}
    view = {
        "payload": ctx.get("payload", {}) or {},
        "variables": ctx.get("variables", {}) or {},
        "secrets": ctx.get("secrets", {}) or {},
        "computed": ctx.get("computed", {}) or {},
        "outcome": ctx.get("outcome", "review"),
        "execution_id": ctx.get("execution_id", "console-test"),
    }
    url = resolve_template(action.get("url", ""), view)
    method = str(action.get("method", "GET")).upper()
    headers = resolve_template(action.get("headers", {}) or {}, view)
    body = resolve_template(action.get("bodyTemplate", action.get("body", {})) or {}, view)
    timeout = min(max(int(action.get("timeoutMs", 5000)), 100), 15000) / 1000

    if not isinstance(url, str) or not url:
        raise HTTPException(status_code=422, detail="Action url is required.")

    # Same SSRF guard as the real action path (executor), not the old substring blocklist: resolve
    # + range-check the host and pin the vetted IP. follow_redirects stays False so a 302 to an
    # internal target can't bypass the check.
    from ..egress import EgressBlocked, build_pinned_request

    try:
        pinned = build_pinned_request(url)
    except EgressBlocked as blocked:
        raise HTTPException(status_code=422, detail="Blocked by egress policy: {0}".format(blocked)) from blocked

    request_headers = dict(headers or {})
    request_headers["Host"] = pinned.host_header
    extensions = {"sni_hostname": pinned.sni_hostname}
    connect_url = pinned.url

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            if method == "GET":
                resp = client.get(connect_url, headers=request_headers, extensions=extensions)
            elif method == "PUT":
                resp = client.put(connect_url, json=body, headers=request_headers, extensions=extensions)
            elif method == "PATCH":
                resp = client.patch(connect_url, json=body, headers=request_headers, extensions=extensions)
            elif method == "DELETE":
                resp = client.request("DELETE", connect_url, headers=request_headers, extensions=extensions)
            else:
                resp = client.post(connect_url, json=body, headers=request_headers, extensions=extensions)
        latency = (time.perf_counter() - started) * 1000
        return {
            "ok": True,
            "status": resp.status_code,
            "success": 200 <= resp.status_code < 300,
            "latencyMs": round(latency, 1),
            "resolvedUrl": url,
            "resolvedMethod": method,
            "responseHeaders": dict(resp.headers),
            "body": resp.text[:20480],
        }
    except Exception as error:  # network / timeout / DNS
        latency = (time.perf_counter() - started) * 1000
        return {"ok": False, "error": str(error), "latencyMs": round(latency, 1), "resolvedUrl": url, "resolvedMethod": method}


# ── Async workflow callback + loop debug ───────────────────────────────────
class WorkflowCallbackRequest(BaseModel):
    step_id: str
    data: Dict[str, Any] = Field(default_factory=dict)


@router.post("/api/v1/workflows/{execution_id}/callback")
def workflow_callback(execution_id: str, request: WorkflowCallbackRequest) -> Dict[str, Any]:
    """Resume a durably-paused async workflow step with its provider's result.

    An async `action` step fires its request, pauses the execution, and waits for
    the provider to POST back here; the execution then continues from that step.
    """
    import copy

    tenant_id = active_tenant_id()
    execution = ensure_exists(main.storage.get_workflow_execution(execution_id, tenant_id=tenant_id), "workflow_execution", execution_id)
    ctx = ExecutionContext.from_dict(execution["context"])
    if ctx.status != "paused":
        raise HTTPException(status_code=409, detail="Execution is not awaiting a callback (status: {0}).".format(ctx.status))
    ctx.callbacks[request.step_id] = copy.deepcopy(request.data)
    ctx.current_step_index = int(ctx.paused_at_step or 0)  # re-enter the async step to consume the callback
    ctx.status = "running"
    policy = ensure_exists(main.storage.get_policy(ctx.policy_id, tenant_id=tenant_id), "policy", ctx.policy_id)
    result = asyncio.run(
        workflow_executor().execute(policy=policy, payload=ctx.payload, tenant_id=tenant_id, resume_from=ctx, source="callback")
    )
    return {
        "execution_id": execution_id,
        "status": result.status,
        "outcome": result.outcome if result.outcome != "pending" else (policy.get("defaultOutcome") or "review"),
        "trace": result.step_trace,
    }


class LoopDebugRequest(BaseModel):
    """Evaluate a single loop step against a payload and return its per-iteration
    trace, without persisting a decision."""
    over: Optional[Any] = None
    items: Optional[List[Any]] = None
    as_: Optional[str] = Field(default=None, alias="as")
    indexAs: Optional[str] = None
    steps: List[Dict[str, Any]] = []
    maxIterations: int = 1000
    payload: Dict[str, Any] = {}
    variable_values: Optional[Dict[str, Any]] = None

    model_config = {"populate_by_name": True}


@router.post("/api/v1/workflows/loop-debug")
def workflow_loop_debug(request: LoopDebugRequest) -> Dict[str, Any]:
    config: Dict[str, Any] = {"steps": request.steps, "maxIterations": request.maxIterations}
    if request.over is not None:
        config["over"] = request.over
    if request.items is not None:
        config["items"] = request.items
    if request.as_:
        config["as"] = request.as_
    if request.indexAs:
        config["indexAs"] = request.indexAs
    loop_step = {"type": "loop", "id": "debug_loop", "config": config}
    return asyncio.run(workflow_executor().debug_loop(
        loop_step, request.payload, active_tenant_id(), variable_values=request.variable_values,
    ))


# ── Concurrent batch simulation (JSON + JSONL) ─────────────────────────────
def _run_decision_batch(policy: Dict[str, Any], payloads: List[Dict[str, Any]], tenant_id: str) -> Dict[str, Any]:
    """Backtest a policy over many payloads, concurrently and in *simulation* mode (no decision
    logged, review gates don't pause). Fast path for pure-compute policies, full executor
    otherwise. Shared by the JSON and JSONL batch endpoints."""
    from concurrent.futures import ThreadPoolExecutor

    from ..fast_decide import fast_decide, is_fast_servable

    use_fast = is_fast_servable(policy)
    workers = min(env.env_int("SIM_MAX_WORKERS", 32), max(4, (os.cpu_count() or 4) * 4))
    default_outcome = policy.get("defaultOutcome") or "review"

    def _one(item: tuple) -> tuple:
        index, payload = item
        try:
            if use_fast:
                d = fast_decide(main.storage, policy, payload or {}, tenant_id, log=False)
                return index, {"index": index, "result": {"policy_id": policy["id"], "outcome": d["outcome"], "latency_ms": d.get("latency_ms")}}
            ctx = asyncio.run(PolicyExecutor(main.storage).execute(
                policy=policy, payload=payload or {}, tenant_id=tenant_id, source="simulation", simulate=True))
            outcome = ctx.outcome if ctx.outcome != "pending" else default_outcome
            return index, {"index": index, "result": {"policy_id": policy["id"], "outcome": outcome, "latency_ms": ctx.total_latency_ms}}
        except Exception as exc:  # a bad case must not sink the batch
            return index, {"index": index, "result": {"policy_id": policy["id"], "outcome": "error", "error": str(exc)}}

    rows: List[Optional[Dict[str, Any]]] = [None] * len(payloads)
    started = time.perf_counter()
    if payloads:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, row in pool.map(_one, enumerate(payloads)):
                rows[index] = row
    elapsed = time.perf_counter() - started
    tps = round(len(payloads) / elapsed) if elapsed > 0 and payloads else None
    return {
        "targetType": "decide", "targetId": policy["id"], "rows": rows, "count": len(rows),
        "performance": {
            "server_ms": round(elapsed * 1000, 1),
            "throughput_tps": tps,
            "avg_ms": round(elapsed * 1000 / len(payloads), 3) if payloads else None,
            "path": "fast" if use_fast else "full_executor",
            "workers": workers,
        },
    }


@router.post("/api/v1/decide/batch")
def batch_decide(request: BatchSimulationRequest) -> Dict[str, Any]:
    """Backtest a policy over many cases. Runs concurrently and in *simulation*
    mode — no decision is logged and review gates don't pause — so thousands of
    what-if cases execute without DB-write contention. Uses the fast (cached-bundle
    / Rust) path for pure-compute policies, the full executor otherwise. Returns a
    `performance` block with real server-side throughput."""
    target_id = request.resolved_target_id
    if not target_id:
        raise HTTPException(
            status_code=422,
            detail="Supply the policy to run: policyId (as /decide takes) or targetId.",
        )
    # Resolve tenant + policy in the request thread (worker threads have no context).
    tenant_id = active_tenant_id()
    policy = ensure_exists(main.storage.get_policy(target_id, tenant_id=tenant_id), "policy", target_id)
    return _run_decision_batch(policy, request.payloads or [], tenant_id)


@router.post("/api/v1/decide/batch/jsonl")
async def batch_decide_jsonl(request: Request) -> Response:
    """Backtest a policy over payloads supplied as **JSONL** — one JSON object per line — and,
    when asked, stream the results back as JSONL. This is the import/export path for large
    what-if runs: a client uploads a `.jsonl` of payloads (`?targetId=<policy>`), each line is
    parsed (blank lines skipped, an unparseable line becomes an `error` row so one bad line
    never sinks the batch), and results come back as JSON (default) or JSONL (`?format=jsonl`,
    downloadable). Reuses the same concurrent simulation core as /decide/batch."""
    target_id = request.query_params.get("targetId")
    if not target_id:
        raise HTTPException(status_code=422, detail="targetId query parameter is required.")
    tenant_id = active_tenant_id(request)
    policy = ensure_exists(main.storage.get_policy(target_id, tenant_id=tenant_id), "policy", target_id)

    body = (await request.body()).decode("utf-8", errors="replace")
    payloads: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    for line_no, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
            payloads.append(parsed if isinstance(parsed, dict) else {"_value": parsed})
        except json.JSONDecodeError as exc:
            parse_errors.append({"line": line_no, "error": str(exc)})
        if len(payloads) > SIMULATION_JSONL_MAX:
            raise HTTPException(status_code=413, detail="JSONL exceeds {0} payloads.".format(SIMULATION_JSONL_MAX))

    result = _run_decision_batch(policy, payloads, tenant_id)
    result["parseErrors"] = parse_errors

    if request.query_params.get("format") == "jsonl":
        lines = [json_dumps(row) for row in result["rows"] if row is not None]
        return Response(
            content="\n".join(lines) + ("\n" if lines else ""),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="simulation-results.jsonl"'},
        )
    return Response(content=json_dumps(result), media_type="application/json")


# ── Deploy board ───────────────────────────────────────────────────────────
@router.get("/api/v1/deploy/status")
def deploy_status() -> Dict[str, Any]:
    return {
        "dev": {
            "variables": main.storage.list_variables(status="dev"),
            "rules": main.storage.list_rules(status="dev"),
            "scorecards": main.storage.list_scorecards(status="dev"),
            "policies": main.storage.list_policies(status="dev"),
        },
        "uat": {
            "variables": main.storage.list_variables(status="uat"),
            "rules": main.storage.list_rules(status="uat"),
            "scorecards": main.storage.list_scorecards(status="uat"),
            "policies": main.storage.list_policies(status="uat"),
        },
        "prod": {
            "variables": main.storage.list_variables(status="prod"),
            "rules": main.storage.list_rules(status="prod"),
            "scorecards": main.storage.list_scorecards(status="prod"),
            "policies": main.storage.list_policies(status="prod"),
        },
    }


@router.post("/api/v1/deploy/promote")
def deploy_promote(request: DeployPromoteRequest) -> Dict[str, Any]:
    promoted = []
    for item in request.items:
        promoted.append(promote_entity(item.entity_type, item.entity_id, item.promoted_by, item.reason))
    return {"promoted": promoted, "history": main.storage.list_promotions()[: len(promoted)]}


# ── Config export / import ─────────────────────────────────────────────────
@router.get("/api/v1/export")
def export_config(format: str = Query(default="json")) -> PlainTextResponse:
    config = main.storage.export_config()
    content = export_bundle(config, format)
    extension = {"python": "py", "csv": "csv"}.get(format, format)
    media_type = {
        "json": "application/json",
        "yaml": "application/x-yaml",
        "python": "text/x-python",
        "csv": "text/csv",
    }.get(format, "text/plain")
    return PlainTextResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": 'attachment; filename="rulemind-export.{0}"'.format(extension)},
    )


def _validate_import_entity(entity_type: str, entity: Dict[str, Any], existing_connectors: List[str], existing_variables: List[str]) -> Dict[str, Any]:
    """Validate a single entity and return a validation report."""
    issues: List[str] = []
    entity_id = entity.get("id", entity.get("name", "unknown"))

    if entity_type == "variable":
        if not entity.get("code"):
            issues.append("Missing 'code' field")
        else:
            import ast as _ast
            try:
                _ast.parse(entity["code"])
            except SyntaxError as exc:
                issues.append(f"Python syntax error: {exc}")
        if entity.get("source_id") and entity["source_id"] not in existing_connectors:
            issues.append(f"Referenced connector '{entity['source_id']}' not found")

    elif entity_type == "rule":
        nodes = entity.get("nodes", [])
        tree = entity.get("tree")
        if not nodes and not tree:
            issues.append("Rule has no nodes and no tree")
        for node in nodes:
            node_type = node.get("type", "")
            if node_type not in ALLOWED_NODE_TYPES:
                issues.append(f"Invalid node type: '{node_type}'")
            if node_type == "condition":
                operator = node.get("operator", "")
                if operator and operator not in ALLOWED_OPERATORS:
                    issues.append(f"Unsupported operator '{operator}' on node '{node.get('id', '?')}'")
                if node.get("variable") and node["variable"] not in existing_variables:
                    issues.append(f"Referenced variable '{node['variable']}' not found")
        has_outcome = any(n.get("type") in {"approve", "review", "reject"} for n in nodes)
        if nodes and not has_outcome and not tree:
            issues.append("Rule has no outcome node (approve/review/reject)")
        if tree:
            _validate_tree_depth(tree, issues, depth=0, max_depth=10)

    elif entity_type == "scorecard":
        if not entity.get("bins"):
            issues.append("Scorecard has no bins")
        for b in entity.get("bins", []):
            if b.get("variable_id") and b["variable_id"] not in existing_variables:
                issues.append(f"Referenced variable '{b['variable_id']}' not found in scorecard bin")
            for r in b.get("ranges", []):
                try:
                    if float(r.get("min", 0)) > float(r.get("max", 0)):
                        issues.append(f"Invalid range: min ({r.get('min')}) > max ({r.get('max')}) in bin '{b.get('variable_id')}'")
                except (TypeError, ValueError):
                    issues.append(f"Non-numeric range values in bin '{b.get('variable_id')}'")

    elif entity_type == "policy":
        if not entity.get("steps"):
            issues.append("Policy has no steps")
        valid_step_types = {"connector", "rule", "scorecard", "decision_table", "outcome", "action", "review_gate", "transform", "model", "branch", "loop", "workflow", "monitor"}
        for step in entity.get("steps", []):
            st = step.get("type", "")
            if st not in valid_step_types:
                issues.append(f"Invalid step type: '{st}'")

    return {
        "id": entity_id,
        "valid": len(issues) == 0,
        "issues": issues,
    }


def _validate_tree_depth(tree: Dict[str, Any], issues: List[str], depth: int, max_depth: int) -> None:
    if depth > max_depth:
        issues.append(f"Tree depth exceeds maximum ({max_depth})")
        return
    # Import payloads are user-supplied: a `tree` that is a string/list/None must be
    # reported as an invalid entity, not raise AttributeError into a 500. The whole
    # point of /import/validate is to survive malformed input and describe it.
    if not isinstance(tree, dict):
        issues.append(f"Tree must be an object, got {type(tree).__name__}")
        return
    children = tree.get("children") or []
    if not isinstance(children, list):
        issues.append(f"Tree 'children' must be a list, got {type(children).__name__}")
        children = []
    for child in children:
        _validate_tree_depth(child, issues, depth + 1, max_depth)
    child_node = tree.get("child")
    if isinstance(child_node, dict):
        _validate_tree_depth(child_node, issues, depth + 1, max_depth)


@router.post("/api/v1/import/validate")
def validate_import(request: ImportRequest) -> Dict[str, Any]:
    """Validate an import payload and return a detailed report of which entities are valid/invalid."""
    data = request.model_dump()
    connector_ids = {c.get("id") for c in data.get("connectors", []) if c.get("id")}
    connector_ids.update(c["id"] for c in main.storage.list_connectors())
    variable_ids = {v.get("id") for v in data.get("variables", []) if v.get("id")}
    variable_ids.update(v["id"] for v in main.storage.list_variables())

    report: Dict[str, Any] = {}
    total, valid_count, invalid_count = 0, 0, 0

    for entity_type in ("connectors", "variables", "rules", "scorecards", "policies"):
        singular = entity_type.rstrip("s") if entity_type != "policies" else "policy"
        entity_reports = []
        for entity in data.get(entity_type, []):
            r = _validate_import_entity(singular, entity, list(connector_ids), list(variable_ids))
            entity_reports.append(r)
            total += 1
            if r["valid"]:
                valid_count += 1
            else:
                invalid_count += 1
        report[entity_type] = entity_reports

    report["summary"] = {"total": total, "valid": valid_count, "invalid": invalid_count}
    return report


@router.post("/api/v1/import")
def import_config(request: ImportRequest) -> Dict[str, Any]:
    """Import configuration with validation report. All entities are imported; invalid ones are flagged."""
    validation = validate_import(request)
    try:
        main.storage.replace_all(request.model_dump())
        return {
            "imported": True,
            "counts": {key: len(value) if isinstance(value, list) else 1 for key, value in request.model_dump().items()},
            "validation": validation,
        }
    except Exception as error:
        record_error("imports", "replace_all", str(error), None, None, {"keys": sorted(request.model_dump().keys())})
        raise


# ── Settings + bootstrap ───────────────────────────────────────────────────
@router.get("/api/v1/settings")
def get_settings() -> Dict[str, Any]:
    return main.storage.get_settings()


@router.put("/api/v1/settings")
def update_settings(request: SettingsRequest) -> Dict[str, Any]:
    return main.storage.update_settings(request.model_dump())


@router.get("/api/v1/bootstrap")
def bootstrap() -> Dict[str, Any]:
    return {
        "connectors": main.storage.list_connectors(),
        "variables": main.storage.list_variables(),
        "rules": main.storage.list_rules(),
        "scorecards": main.storage.list_scorecards(),
        "policies": main.storage.list_policies(),
        "settings": main.storage.get_settings(),
        "promotions": main.storage.list_promotions(),
    }


# ── Live decision stream (SSE) ─────────────────────────────────────────────
def _sse_decision_frame(row: Dict[str, Any]) -> str:
    """Render one decision as a compact Server-Sent Event frame."""
    data = {
        "id": row.get("id"),
        "policy_id": row.get("policy_id"),
        "outcome": row.get("outcome"),
        "source": row.get("source"),
        "latency_ms": row.get("latency_ms"),
        "experiment_variant": row.get("experiment_variant"),
        "created_at": row.get("created_at"),
    }
    return "id: {0}\nevent: decision\ndata: {1}\n\n".format(row.get("id"), json_dumps(data))


@router.get("/api/v1/decisions/stream")
async def stream_decisions(request: Request) -> StreamingResponse:
    """Live decision feed as Server-Sent Events (text/event-stream).

    Opens with a small backlog (the most recent decisions, oldest-first), then long-polls for
    new rows and pushes each as a `decision` event, advancing a monotonic `created_at` cursor so
    nothing is re-sent. A client resumes after a drop by passing `?after=<ISO-8601>` (or the
    standard `Last-Event-ID` is echoed as the row id). Heartbeat comments keep proxies from
    closing an idle connection; the stream self-closes after a bounded lifetime so clients
    reconnect (EventSource does this automatically) instead of holding a worker forever.
    `?once=1` emits the current backlog and closes — handy for a one-shot catch-up or testing.
    """
    # Resolve the tenant eagerly, while the request context is live: the middleware resets its
    # context vars once this coroutine returns the StreamingResponse, before the body streams.
    tenant_id = active_tenant_id(request)
    after_param = request.query_params.get("after")
    once = request.query_params.get("once") in ("1", "true", "yes")
    poll_seconds = env.env_float("DECISION_STREAM_POLL_SECONDS", 1.0)
    max_seconds = env.env_float("DECISION_STREAM_MAX_SECONDS", 3600)
    backlog_size = env.env_int("DECISION_STREAM_BACKLOG", 25)

    def _emit(row: Dict[str, Any]):
        # Advance the cursor to this row's full-precision timestamp, then frame the clean row.
        raw = row.pop("_created_at_raw", None)
        return raw, _sse_decision_frame(row)

    async def event_gen():
        cursor = _parse_client_datetime(after_param) if after_param else None
        backlog = await asyncio.to_thread(main.storage.decisions_after, tenant_id, cursor, backlog_size)
        yield ": connected\n\n"
        for row in backlog:
            raw, frame = _emit(row)
            cursor = raw or cursor
            yield frame
        if once:
            return
        started = time.monotonic()
        # Live tail: prefer Redis pub/sub (no DB polling, cross-replica). Falls back to DB polling
        # when Redis is unavailable (single-replica dev) so the feature works with no infra.
        if decision_bus.has_redis():
            async for message in decision_bus.subscribe_decisions(tenant_id):
                if await request.is_disconnected() or time.monotonic() - started >= max_seconds:
                    break
                if message:
                    yield _sse_decision_frame(message)
                else:
                    yield ": ping\n\n"
            return
        while time.monotonic() - started < max_seconds:
            if await request.is_disconnected():
                break
            rows = await asyncio.to_thread(main.storage.decisions_after, tenant_id, cursor, 200)
            if rows:
                for row in rows:
                    raw, frame = _emit(row)
                    cursor = raw or cursor
                    yield frame
            else:
                yield ": ping\n\n"
            await asyncio.sleep(poll_seconds)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
        },
    )
