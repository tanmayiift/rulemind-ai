from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Union

from .champion_challenger import analyze_champion_challenger
from .runtime import cache_get_json, cache_set_json
from .storage import Storage
from . import env


def _analytics_window_start() -> Optional[datetime]:
    """Start of the dashboard analytics window (naive UTC). Bounds the scanned decision set so
    the dashboards stay fast at scale; configurable via ANALYTICS_WINDOW_DAYS (default 90).
    Set to 0 to scan all history."""
    days = env.env_int("ANALYTICS_WINDOW_DAYS", 90)
    if days <= 0:
        return None
    return datetime.utcnow() - timedelta(days=days)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a device-supplied number, treating an uncoercible one as `default`.

    Telemetry fields arrive as whatever JSON a client sent. A dashboard read must not be the
    thing that discovers that -- dropping one nonsense measurement is right, and 500ing the
    whole workspace's SDK-health page over it is not."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _percentile(values: Sequence[Union[int, float]], percentile: float) -> float:
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
    # Full windowed set (column-projected, paginated) — not the 200-row recency cap.
    decisions = storage.decision_facts(tenant_id=tenant_id, since=_analytics_window_start())
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
    decisions = storage.decision_facts(tenant_id=tenant_id, since=_analytics_window_start())
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
    version_counts: "Counter[str]" = Counter()
    sync_latencies: List[float] = []
    sync_success = 0
    sync_total = 0
    for event in events:
        # Every field here is DEVICE-SUPPLIED and unvalidated: /sdk/v1/events declares
        # `events: List[Dict[str, Any]]` and storage persists the dict verbatim, so `data` may
        # be a string and `latencyMs` may be "fast". Reading those shapes optimistically made
        # one misbehaving device a stored poison pill -- the row stays in sdk_events, so every
        # later GET /api/v1/analytics/sdk for that workspace 500s, not just the request that
        # ingested it. The guard was half-applied before: `payload` was isinstance-checked for
        # `data` and then `.get` was called on it unconditionally two lines later.
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        sdk_version = data.get("sdkVersion") or payload.get("sdkVersion")
        if sdk_version:
            version_counts[str(sdk_version)] += 1
        if payload.get("type") == "bundle.synced":
            sync_total += 1
            sync_latencies.append(_as_float(data.get("latencyMs")))
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
    # Exact per-variant aggregates over ALL matching decisions, computed in SQL (GROUP BY) —
    # not a 200-row recency sample — so lift + significance hold at real traffic.
    rollup = storage.experiment_variant_rollup(tenant_id, experiment_id)

    variant_rows = []
    variant_stats: dict[str, dict[str, Any]] = {}
    for variant in experiment.get("variants", []):
        stats = rollup.get(str(variant.get("id")), {})
        users = int(stats.get("users", 0))
        approved = int(stats.get("approved", 0))
        rejected = int(stats.get("rejected", 0))
        reviewed = int(stats.get("reviewed", 0))
        avg_latency = round(stats.get("latency_sum", 0) / users, 2) if users else 0
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
        # BY ROLE, not by position. This was `variant_rows[0], variant_rows[1]`, which is wrong
        # in two ways at once: with three or more variants the single headline p-value silently
        # described one arbitrary pair while appearing to describe the experiment, and if the
        # champion was not listed first then "control" was not the champion at all -- the sign of
        # the effect flips and nothing says so.
        #
        # The response now names the pair it measured, so a screenshot of the number is
        # self-describing and a reader with four variants can see that it covers two of them.
        # Pick the control FIRST, then a treatment that is not it. The obvious phrasing --
        # "first row whose role is not champion, and which is not the champion" -- is wrong when
        # no variant declares a role at all: `champion` is None, `row is not None` excludes
        # nothing, and control and treatment end up being THE SAME ROW. p1 == p2, z == 0, and
        # every experiment reports "not significant". Variants without roles are not exotic;
        # test_platform's z-test fixture uses plain {"id": "control"} / {"id": "treatment"}, and
        # that suite is what caught this.
        control = next(
            (row for row in variant_rows if row.get("role") == "champion"), variant_rows[0])
        treatment = next(
            (row for row in variant_rows
             if row is not control and row.get("role") == "challenger"), None)
        if treatment is None:
            # No declared challenger: fall back to the next variant in order, which is the
            # pre-existing positional behaviour and the right answer for a role-less experiment.
            treatment = next(row for row in variant_rows if row is not control)
        n1 = max(control["users"], 1)
        n2 = max(treatment["users"], 1)
        p1 = control["approved"] / n1
        p2 = treatment["approved"] / n2
        pooled = (control["approved"] + treatment["approved"]) / (n1 + n2)
        denominator = math.sqrt(max(pooled * (1 - pooled) * ((1 / n1) + (1 / n2)), 1e-9))
        z_score = (p2 - p1) / denominator if denominator else 0
        p_value = math.erfc(abs(z_score) / math.sqrt(2))
        significance = {
            "pValue": round(p_value, 6),
            "significant": p_value < 0.05,
            # Which two arms the number is about. Without these a 4-variant experiment shows one
            # p-value that looks like a verdict on the whole test.
            "control": control["id"],
            "treatment": treatment["id"],
            "comparedVariants": 2,
            "totalVariants": len(variant_rows),
        }

    champion_challenger = analyze_champion_challenger(experiment.get("variants", []), variant_stats)

    result = {
        "experiment": {key: experiment[key] for key in ["id", "name", "status"]},
        "variants": variant_rows,
        "significance": significance,
        "championChallenger": champion_challenger,
    }
    # 30s, not 300. This is a LIVE TRAFFIC rollup, and the page's own hint tells the reader to
    # "run traffic through the target policy with a userId to populate these" -- so at five
    # minutes a user follows that instruction, sees zeros, and runs more traffic, for five
    # minutes. Observed during the sweep: 300 decisions were sent and the page kept showing 0
    # assigned users until the TTL expired.
    #
    # The underlying query is an exact SQL GROUP BY rather than a scan, so the cache is here to
    # absorb repeated polling of one page, which 30s does just as well.
    cache_set_json(cache_key, result, ttl_seconds=30)
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
