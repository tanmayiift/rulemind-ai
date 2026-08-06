"""Per-workspace SLOs + outcome-drift detection.

Each workspace can declare a service-level objective for its decisioning — a latency
ceiling (p95), an error-rate ceiling, and optional approval-rate bounds — plus a
guard on **outcome drift**: how far the recent outcome mix has moved from a trailing
baseline. ``evaluate_slo`` scores the live decision stream against that objective and
returns the breaches; the scheduler evaluates it on an interval, records a durable
``slo_breach`` audit event, and emits OTel metrics so an on-call backend can alert.

Use-case agnostic: outcomes are the engine's generic verdicts (approve / review /
reject / error / …), not anything domain-specific.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .storage import Storage

logger = logging.getLogger("rulemind.slo")

# Outcomes that count as a hard failure of the decision path (fail-closed gate/variable
# errors surface here). Everything else is a normal verdict.
ERROR_OUTCOMES = {"error"}

# Prometheus gauges — scraped on /metrics, so Grafana/Alertmanager (or any OTLP backend
# via the collector) can alert on a breach without any bespoke alerting code here. Defined
# once, in this module, to avoid duplicate-registration across the app.
try:  # pragma: no cover - exercised indirectly; import guard for envs without the dep
    from prometheus_client import Gauge

    SLO_P95_LATENCY = Gauge("rulemind_slo_p95_latency_ms", "Recent-window p95 decision latency (ms)", ["tenant"])
    SLO_ERROR_RATE = Gauge("rulemind_slo_error_rate_pct", "Recent-window decision error rate (%)", ["tenant"])
    SLO_DRIFT_DISTANCE = Gauge("rulemind_slo_outcome_drift", "Outcome-mix drift vs baseline (total-variation distance)", ["tenant"])
    SLO_HEALTHY = Gauge("rulemind_slo_healthy", "1 when the workspace SLO is met, 0 when breached", ["tenant"])
    SLO_BREACHES = Gauge("rulemind_slo_active_breaches", "Active SLO breaches by type (1 active, 0 clear)", ["tenant", "type"])
    _BREACH_TYPES = ("latency_p95", "error_rate", "approval_rate_low", "approval_rate_high", "outcome_drift")
except Exception:  # pragma: no cover - metrics are best-effort
    Gauge = None  # type: ignore
    SLO_P95_LATENCY = SLO_ERROR_RATE = SLO_DRIFT_DISTANCE = SLO_HEALTHY = SLO_BREACHES = None  # type: ignore
    _BREACH_TYPES = ()


def record_prometheus(tenant_id: str, report: Dict[str, Any]) -> None:
    """Publish an SLO evaluation to the Prometheus gauges. Best-effort and never raises."""
    if SLO_HEALTHY is None:
        return
    try:
        metrics = report.get("metrics", {})
        SLO_P95_LATENCY.labels(tenant=tenant_id).set(float(metrics.get("p95_latency_ms", 0) or 0))
        SLO_ERROR_RATE.labels(tenant=tenant_id).set(float(metrics.get("error_rate_pct", 0) or 0))
        SLO_DRIFT_DISTANCE.labels(tenant=tenant_id).set(float(report.get("drift", {}).get("distance", 0) or 0))
        SLO_HEALTHY.labels(tenant=tenant_id).set(1 if report.get("healthy") else 0)
        active = {b.get("type") for b in report.get("breaches", [])}
        for breach_type in _BREACH_TYPES:
            SLO_BREACHES.labels(tenant=tenant_id, type=breach_type).set(1 if breach_type in active else 0)
    except Exception:  # pragma: no cover - metrics must never break the caller
        logger.debug("SLO metric publish failed for tenant %s", tenant_id, exc_info=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def default_slo_config() -> Dict[str, Any]:
    """Platform-wide SLO defaults (env-overridable). A workspace's stored config is
    merged over these, so an empty/omitted config still yields a sensible objective."""
    return {
        "enabled": True,
        # Latency ceiling for the hot decision path (matches the <100ms design target).
        "latency_p95_ms": _env_float("SLO_LATENCY_P95_MS", 100.0),
        # Share of decisions that may end in a hard error before it's a breach.
        "error_rate_pct": _env_float("SLO_ERROR_RATE_PCT", 1.0),
        # Optional approval-rate guardrails (null disables). Catches a policy change or
        # data-drift that suddenly approves/rejects far more than usual.
        "min_approval_rate_pct": None,
        "max_approval_rate_pct": None,
        # Outcome-drift guard: total-variation distance between the recent window's
        # outcome distribution and the trailing baseline. 0 = identical, 1 = disjoint.
        "drift_threshold": _env_float("SLO_DRIFT_THRESHOLD", 0.2),
        # Minimum recent volume before drift / rates are trusted (avoids noise alerts).
        "min_sample": _env_int("SLO_MIN_SAMPLE", 50),
        "recent_hours": _env_int("SLO_RECENT_HOURS", 24),
        "baseline_days": _env_int("SLO_BASELINE_DAYS", 7),
    }


def _coerce_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = default_slo_config()
    if not isinstance(raw, dict):
        return cfg
    for key in ("enabled",):
        if key in raw:
            cfg[key] = bool(raw[key])
    for key in ("latency_p95_ms", "error_rate_pct", "drift_threshold"):
        if raw.get(key) is not None:
            try:
                cfg[key] = float(raw[key])
            except (TypeError, ValueError):
                pass
    for key in ("min_sample", "recent_hours", "baseline_days"):
        if raw.get(key) is not None:
            try:
                cfg[key] = max(1, int(raw[key]))
            except (TypeError, ValueError):
                pass
    for key in ("min_approval_rate_pct", "max_approval_rate_pct"):
        if key in raw:
            val = raw[key]
            if val is None or val == "":
                cfg[key] = None
            else:
                try:
                    cfg[key] = float(val)
                except (TypeError, ValueError):
                    cfg[key] = None
    return cfg


def tenant_slo_config(storage: Storage, tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """The effective SLO config for a workspace — stored overrides merged over defaults."""
    settings = storage.get_settings(tenant_id=tenant_id)
    raw = (settings.get("engine_config", {}) or {}).get("slo")
    return _coerce_config(raw)


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _distribution(outcomes: List[str]) -> Dict[str, float]:
    total = len(outcomes)
    if not total:
        return {}
    counts = Counter(outcomes)
    return {key: value / total for key, value in counts.items()}


def total_variation_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Half the L1 distance between two outcome distributions — 0 (identical) … 1 (disjoint)."""
    keys = set(a) | set(b)
    return round(0.5 * sum(abs(a.get(key, 0.0) - b.get(key, 0.0)) for key in keys), 4)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "").split("+")[0]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def evaluate_slo(
    storage: Storage,
    tenant_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Score the live decision stream for a workspace against its SLO.

    Returns metrics for the recent window, the outcome-drift measurement vs the trailing
    baseline, and a list of breaches (empty when healthy)."""
    cfg = config or tenant_slo_config(storage, tenant_id)
    now = now or datetime.utcnow()
    recent_start = now - timedelta(hours=cfg["recent_hours"])
    baseline_start = now - timedelta(days=cfg["baseline_days"])

    # One scan over the baseline window; split into recent vs baseline (baseline excludes recent).
    rows = storage.decision_facts(tenant_id=tenant_id, since=baseline_start)
    recent: List[Dict[str, Any]] = []
    baseline: List[Dict[str, Any]] = []
    for row in rows:
        ts = _parse_ts(row.get("created_at"))
        if ts is not None and ts >= recent_start:
            recent.append(row)
        else:
            baseline.append(row)

    recent_outcomes = [str(r.get("outcome", "unknown")) for r in recent]
    latencies = [float(r.get("latency_ms") or 0) for r in recent]
    total = len(recent)
    errors = sum(1 for o in recent_outcomes if o in ERROR_OUTCOMES)
    approvals = sum(1 for o in recent_outcomes if o == "approve")
    p95 = round(_percentile(latencies, 0.95), 2)
    error_rate = round((errors / total) * 100, 2) if total else 0.0
    approval_rate = round((approvals / total) * 100, 2) if total else 0.0

    recent_dist = _distribution(recent_outcomes)
    baseline_dist = _distribution([str(r.get("outcome", "unknown")) for r in baseline])
    drift_distance = total_variation_distance(recent_dist, baseline_dist)
    drift_measurable = len(baseline) >= cfg["min_sample"] and total >= cfg["min_sample"]

    breaches: List[Dict[str, Any]] = []
    if cfg["enabled"] and total:
        if p95 > cfg["latency_p95_ms"]:
            breaches.append({
                "type": "latency_p95", "observed": p95, "threshold": cfg["latency_p95_ms"],
                "severity": "critical", "message": "p95 latency {0}ms over {1}ms objective".format(p95, cfg["latency_p95_ms"]),
            })
        if total >= cfg["min_sample"] and error_rate > cfg["error_rate_pct"]:
            breaches.append({
                "type": "error_rate", "observed": error_rate, "threshold": cfg["error_rate_pct"],
                "severity": "critical", "message": "error rate {0}% over {1}% objective".format(error_rate, cfg["error_rate_pct"]),
            })
        if total >= cfg["min_sample"]:
            lo = cfg.get("min_approval_rate_pct")
            hi = cfg.get("max_approval_rate_pct")
            if lo is not None and approval_rate < lo:
                breaches.append({
                    "type": "approval_rate_low", "observed": approval_rate, "threshold": lo,
                    "severity": "warning", "message": "approval rate {0}% under {1}% floor".format(approval_rate, lo),
                })
            if hi is not None and approval_rate > hi:
                breaches.append({
                    "type": "approval_rate_high", "observed": approval_rate, "threshold": hi,
                    "severity": "warning", "message": "approval rate {0}% over {1}% ceiling".format(approval_rate, hi),
                })
        if drift_measurable and drift_distance > cfg["drift_threshold"]:
            breaches.append({
                "type": "outcome_drift", "observed": drift_distance, "threshold": cfg["drift_threshold"],
                "severity": "warning", "message": "outcome mix drifted {0} vs baseline (limit {1})".format(drift_distance, cfg["drift_threshold"]),
            })

    return {
        "enabled": bool(cfg["enabled"]),
        "healthy": len(breaches) == 0,
        "window": {
            "recent_hours": cfg["recent_hours"],
            "baseline_days": cfg["baseline_days"],
            "recent_start": recent_start.isoformat() + "Z",
            "evaluated_at": now.isoformat() + "Z",
        },
        "metrics": {
            "sample": total,
            "p95_latency_ms": p95,
            "error_rate_pct": error_rate,
            "approval_rate_pct": approval_rate,
        },
        "drift": {
            "distance": drift_distance,
            "threshold": cfg["drift_threshold"],
            "measurable": drift_measurable,
            "recent": {k: round(v, 4) for k, v in recent_dist.items()},
            "baseline": {k: round(v, 4) for k, v in baseline_dist.items()},
        },
        "objective": {
            "latency_p95_ms": cfg["latency_p95_ms"],
            "error_rate_pct": cfg["error_rate_pct"],
            "drift_threshold": cfg["drift_threshold"],
            "min_approval_rate_pct": cfg.get("min_approval_rate_pct"),
            "max_approval_rate_pct": cfg.get("max_approval_rate_pct"),
            "min_sample": cfg["min_sample"],
        },
        "breaches": breaches,
    }
