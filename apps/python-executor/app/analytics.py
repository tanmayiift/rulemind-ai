from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Union

from .champion_challenger import analyze_champion_challenger
from .runtime import cache_get_json, cache_set_json
from .storage import Storage


def _percentile(values: List[Union[int, float]], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def decision_analytics(storage: Storage, tenant_id: str) -> Dict[str, Any]:
    cache_key = "analytics:{0}:decisions".format(tenant_id)
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    decisions = storage.list_decisions(tenant_id=tenant_id)
    total = len(decisions)
    outcomes = Counter(item.get("outcome", "unknown") for item in decisions)
    by_policy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_source = Counter(item.get("source", "api") for item in decisions)
    timeseries: dict[str, Counter] = defaultdict(Counter)

    for decision in decisions:
        policy_id = decision.get("policy_id") or "unknown"
        by_policy[policy_id].append(decision)
        timestamp = str(decision.get("created_at", ""))[:13] + ":00:00Z"
        timeseries[timestamp][decision.get("outcome", "unknown")] += 1
        timeseries[timestamp]["total"] += 1

    result = {
        "summary": {
            "total": total,
            "approved": outcomes.get("approve", 0),
            "reviewed": outcomes.get("review", 0),
            "rejected": outcomes.get("reject", 0),
            "approvalRate": round((outcomes.get("approve", 0) / total) * 100, 2) if total else 0,
        },
        "timeseries": [
            {
                "timestamp": timestamp,
                "total": counts.get("total", 0),
                "approved": counts.get("approve", 0),
                "reviewed": counts.get("review", 0),
                "rejected": counts.get("reject", 0),
            }
            for timestamp, counts in sorted(timeseries.items())
        ],
        "byPolicy": [
            {
                "policyId": policy_id,
                "total": len(items),
                "approvalRate": round((sum(1 for item in items if item.get("outcome") == "approve") / len(items)) * 100, 2) if items else 0,
                "avgLatencyMs": round(sum(item.get("latency_ms", 0) for item in items) / len(items), 2) if items else 0,
            }
            for policy_id, items in by_policy.items()
        ],
        "bySource": [{"source": source, "total": count} for source, count in by_source.items()],
    }
    cache_set_json(cache_key, result, ttl_seconds=300)
    return result


def latency_analytics(storage: Storage, tenant_id: str) -> Dict[str, Any]:
    cache_key = "analytics:{0}:latency".format(tenant_id)
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    decisions = storage.list_decisions(tenant_id=tenant_id)
    latencies = [int(item.get("latency_ms", 0)) for item in decisions]
    by_source: dict[str, list[int]] = defaultdict(list)
    for decision in decisions:
        source = str(decision.get("source", "api"))
        bucket = "edge" if source == "sdk_edge" else "server"
        by_source[bucket].append(int(decision.get("latency_ms", 0)))
    result = {
        "p50": round(_percentile(latencies, 0.50), 2),
        "p90": round(_percentile(latencies, 0.90), 2),
        "p95": round(_percentile(latencies, 0.95), 2),
        "p99": round(_percentile(latencies, 0.99), 2),
        "byMode": {
            mode: {"p50": round(_percentile(values, 0.50), 2), "p95": round(_percentile(values, 0.95), 2)}
            for mode, values in by_source.items()
        },
    }
    cache_set_json(cache_key, result, ttl_seconds=300)
    return result


def sdk_analytics(storage: Storage, tenant_id: str) -> Dict[str, Any]:
    cache_key = "analytics:{0}:sdk".format(tenant_id)
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    events = storage.list_sdk_events(tenant_id=tenant_id)
    version_counts = Counter()
    sync_latencies: List[float] = []
    sync_success = 0
    sync_total = 0
    for event in events:
        payload = event.get("payload", {})
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        sdk_version = data.get("sdkVersion") or payload.get("sdkVersion")
        if sdk_version:
            version_counts[str(sdk_version)] += 1
        if payload.get("type") == "bundle.synced":
            sync_total += 1
            sync_latencies.append(float(data.get("latencyMs", 0)))
            if not data.get("error"):
                sync_success += 1
    result = {
        "activeSdkVersions": [{"version": version, "count": count} for version, count in version_counts.items()],
        "bundleSyncHealth": {
            "successRate": round((sync_success / sync_total) * 100, 2) if sync_total else 0,
            "avgLatencyMs": round(sum(sync_latencies) / len(sync_latencies), 2) if sync_latencies else 0,
        },
        "eventIngestionRate": len(events),
    }
    cache_set_json(cache_key, result, ttl_seconds=300)
    return result


def experiment_analytics(storage: Storage, tenant_id: str, experiment_id: str) -> Dict[str, Any]:
    cache_key = "analytics:{0}:experiment:{1}".format(tenant_id, experiment_id)
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    experiment = storage.get_experiment(experiment_id, tenant_id=tenant_id)
    if not experiment:
        raise ValueError("Experiment not found")
    decisions = [item for item in storage.list_decisions(tenant_id=tenant_id) if item.get("experiment_variant")]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        grouped[str(decision.get("experiment_variant"))].append(decision)

    variant_rows = []
    variant_stats: dict[str, dict[str, Any]] = {}
    for variant in experiment.get("variants", []):
        items = grouped.get(variant.get("id"), [])
        users = len(items)
        approved = sum(1 for item in items if item.get("outcome") == "approve")
        rejected = sum(1 for item in items if item.get("outcome") == "reject")
        reviewed = sum(1 for item in items if item.get("outcome") == "review")
        avg_latency = round(sum(int(item.get("latency_ms", 0)) for item in items) / users, 2) if users else 0
        row = {
            "id": variant.get("id"),
            "role": variant.get("role"),
            "users": users,
            "approved": approved,
            "rejected": rejected,
            "reviewed": reviewed,
            "approvalRate": round((approved / users) * 100, 2) if users else 0,
            "rejectRate": round((rejected / users) * 100, 2) if users else 0,
            "avgLatencyMs": avg_latency,
        }
        variant_rows.append(row)
        variant_stats[str(variant.get("id"))] = row

    significance = {"pValue": 1.0, "significant": False}
    if len(variant_rows) >= 2:
        control, treatment = variant_rows[0], variant_rows[1]
        n1 = max(control["users"], 1)
        n2 = max(treatment["users"], 1)
        p1 = control["approved"] / n1
        p2 = treatment["approved"] / n2
        pooled = (control["approved"] + treatment["approved"]) / (n1 + n2)
        denominator = math.sqrt(max(pooled * (1 - pooled) * ((1 / n1) + (1 / n2)), 1e-9))
        z_score = (p2 - p1) / denominator if denominator else 0
        p_value = math.erfc(abs(z_score) / math.sqrt(2))
        significance = {"pValue": round(p_value, 6), "significant": p_value < 0.05}

    champion_challenger = analyze_champion_challenger(experiment.get("variants", []), variant_stats)

    result = {
        "experiment": {key: experiment[key] for key in ["id", "name", "status"]},
        "variants": variant_rows,
        "significance": significance,
        "championChallenger": champion_challenger,
    }
    cache_set_json(cache_key, result, ttl_seconds=300)
    return result


def rejection_drivers(decisions: List[Dict[str, Any]], focus_outcomes=("reject", "review")) -> Dict[str, Any]:
    """Rank the conditions/predictors that most often FAIL on non-approved decisions.

    Pure compute over decision traces — exact and cheap (no LLM). For each rule
    condition in each decision, count failures on decisions whose outcome is in
    `focus_outcomes`, plus total evaluations (for a fail rate). Answers "which
    factor/predictor drives the most rejections".
    """
    focus = set(focus_outcomes)
    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"variable_name": "", "operator": "", "threshold": None, "fail_count": 0, "seen": 0, "example_value": None}
    )
    analyzed = 0
    focus_count = 0
    outcome_mix: Dict[str, int] = defaultdict(int)

    for d in decisions:
        analyzed += 1
        outcome = d.get("outcome")
        outcome_mix[str(outcome)] += 1
        focused = outcome in focus
        if focused:
            focus_count += 1
        for step in d.get("trace") or []:
            if (step.get("step") or {}).get("type") != "rule":
                continue
            for c in ((step.get("result") or {}).get("conditions") or []):
                vid = c.get("variable_id") or c.get("variable_name")
                if not vid:
                    continue
                slot = agg[vid]
                slot["variable_name"] = c.get("variable_name") or vid
                slot["operator"] = c.get("operator", "")
                slot["threshold"] = c.get("threshold")
                slot["seen"] += 1
                if focused and c.get("passed") is False:
                    slot["fail_count"] += 1
                    slot["example_value"] = c.get("value")

    drivers: List[Dict[str, Any]] = []
    for vid, s in agg.items():
        seen = s["seen"] or 0
        drivers.append({
            "variable_id": vid,
            "variable_name": s["variable_name"],
            "operator": s["operator"],
            "threshold": s["threshold"],
            "fail_count": s["fail_count"],
            "seen": seen,
            "fail_rate": round(s["fail_count"] / seen, 4) if seen else 0.0,
            "share_of_focus": round(s["fail_count"] / focus_count, 4) if focus_count else 0.0,
            "example_value": s["example_value"],
        })
    drivers.sort(key=lambda x: (x["fail_count"], x["fail_rate"]), reverse=True)

    return {
        "analyzed": analyzed,
        "focus_outcomes": list(focus_outcomes),
        "focus_count": focus_count,
        "outcome_mix": dict(outcome_mix),
        "drivers": drivers,
    }
