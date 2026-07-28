"""Champion/Challenger analysis — pure, DB-free helpers.

Extends A/B experiments with explicit champion vs. challenger roles, a traffic
ramp schedule, guardrail metrics, and an automated promote / rollback / hold
recommendation. All functions are pure so they can be unit-tested without a DB
and reused by the analytics layer and any future scheduler.

Experiment shape (stored in the free-form `variants` JSON — no migration):
    variants: [
      {"id": "champion",  "role": "champion",  "weight": 90},
      {"id": "challenger","role": "challenger","weight": 10,
        "overrides": {...},
        "ramp": [{"day": 0, "weight": 10}, {"day": 3, "weight": 25},
                 {"day": 7, "weight": 50}],
        "guardrails": {"minApprovalRate": 60, "maxRejectRate": 40,
                        "maxAvgLatencyMs": 150, "maxApprovalRateDropPct": 5}}
    ]
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def identify_roles(variants: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (champion, [challengers]).

    Champion = the variant with role 'champion'; falls back to the first variant.
    Everything else is a challenger.
    """
    if not variants:
        return None, []
    champion = next((v for v in variants if str(v.get("role", "")).lower() == "champion"), None)
    if champion is None:
        champion = variants[0]
    challengers = [v for v in variants if v is not champion]
    return champion, challengers


def current_ramp_weight(variant: Dict[str, Any], elapsed_days: float) -> Optional[float]:
    """Resolve the challenger's current traffic weight from its ramp schedule.

    Returns the weight of the most recent ramp step whose `day` <= elapsed_days,
    or None when no ramp is defined (caller should use the static `weight`).
    """
    ramp = variant.get("ramp")
    if not isinstance(ramp, list) or not ramp:
        return None
    applicable = [step for step in ramp if float(step.get("day", 0)) <= elapsed_days]
    if not applicable:
        return float(ramp[0].get("weight", variant.get("weight", 0)))
    latest = max(applicable, key=lambda step: float(step.get("day", 0)))
    return float(latest.get("weight", variant.get("weight", 0)))


def two_proportion_pvalue(approved1: int, total1: int, approved2: int, total2: int) -> float:
    """Two-sided p-value for the difference in two approval proportions."""
    n1 = max(total1, 1)
    n2 = max(total2, 1)
    p1 = approved1 / n1
    p2 = approved2 / n2
    pooled = (approved1 + approved2) / (n1 + n2)
    denominator = math.sqrt(max(pooled * (1 - pooled) * ((1 / n1) + (1 / n2)), 1e-9))
    z_score = (p2 - p1) / denominator if denominator else 0.0
    return math.erfc(abs(z_score) / math.sqrt(2))


def evaluate_guardrails(challenger_stats: Dict[str, Any], champion_stats: Dict[str, Any], guardrails: Dict[str, Any]) -> Dict[str, Any]:
    """Check a challenger's metrics against its guardrails.

    Returns {"breached": bool, "breaches": [str, ...]}.
    """
    breaches: List[str] = []
    if not guardrails:
        return {"breached": False, "breaches": breaches}

    approval = float(challenger_stats.get("approvalRate", 0))
    reject = float(challenger_stats.get("rejectRate", 0))
    latency = float(challenger_stats.get("avgLatencyMs", 0))
    champion_approval = float(champion_stats.get("approvalRate", 0))

    if "minApprovalRate" in guardrails and approval < float(guardrails["minApprovalRate"]):
        breaches.append(f"approval rate {approval}% below floor {guardrails['minApprovalRate']}%")
    if "maxRejectRate" in guardrails and reject > float(guardrails["maxRejectRate"]):
        breaches.append(f"reject rate {reject}% above ceiling {guardrails['maxRejectRate']}%")
    if "maxAvgLatencyMs" in guardrails and latency > float(guardrails["maxAvgLatencyMs"]):
        breaches.append(f"avg latency {latency}ms above ceiling {guardrails['maxAvgLatencyMs']}ms")
    if "maxApprovalRateDropPct" in guardrails:
        drop = champion_approval - approval
        if drop > float(guardrails["maxApprovalRateDropPct"]):
            breaches.append(
                f"approval rate dropped {round(drop, 2)}pp vs champion (max {guardrails['maxApprovalRateDropPct']}pp)"
            )
    return {"breached": bool(breaches), "breaches": breaches}


def recommend_action(
    significant: bool,
    lift: float,
    guardrail_result: Dict[str, Any],
    min_sample: int,
    challenger_users: int,
) -> str:
    """Recommend promote / rollback / hold for a challenger.

    - rollback  : a guardrail is breached (safety first).
    - hold      : not enough traffic yet, or not statistically significant.
    - promote   : significant, positive lift, no guardrail breach.
    """
    if guardrail_result.get("breached"):
        return "rollback"
    if challenger_users < min_sample or not significant:
        return "hold"
    if lift > 0:
        return "promote"
    return "rollback" if lift < 0 else "hold"


def analyze_champion_challenger(
    variants: List[Dict[str, Any]],
    variant_stats: Dict[str, Dict[str, Any]],
    min_sample: int = 100,
) -> Dict[str, Any]:
    """Full champion/challenger analysis from precomputed per-variant stats.

    variant_stats: {variant_id: {"users","approved","approvalRate","rejectRate",
                                   "avgLatencyMs"}}
    """
    champion, challengers = identify_roles(variants)
    if champion is None:
        return {"champion": None, "challengers": []}

    champion_stats = variant_stats.get(champion.get("id"), {})
    champion_approval = float(champion_stats.get("approvalRate", 0))

    challenger_rows = []
    for challenger in challengers:
        stats = variant_stats.get(challenger.get("id"), {})
        lift = round(float(stats.get("approvalRate", 0)) - champion_approval, 2)
        p_value = two_proportion_pvalue(
            int(champion_stats.get("approved", 0)),
            int(champion_stats.get("users", 0)),
            int(stats.get("approved", 0)),
            int(stats.get("users", 0)),
        )
        significant = p_value < 0.05
        guardrail_result = evaluate_guardrails(stats, champion_stats, challenger.get("guardrails", {}))
        recommendation = recommend_action(
            significant, lift, guardrail_result, min_sample, int(stats.get("users", 0))
        )
        challenger_rows.append(
            {
                "id": challenger.get("id"),
                "stats": stats,
                "liftPct": lift,
                "pValue": round(p_value, 6),
                "significant": significant,
                "guardrails": guardrail_result,
                "recommendation": recommendation,
            }
        )

    return {
        "champion": {"id": champion.get("id"), "stats": champion_stats},
        "challengers": challenger_rows,
    }
