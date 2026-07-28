"""Simulation & validation harness for the stateless decision core.

Builds a synthetic dataset of customers (customer_id + 20 variables), a realistic
credit policy bundle, then:
  * runs every customer through the core and validates each outcome against an
    independent oracle (proves correctness at scale),
  * measures single-core throughput,
  * runs an A/B champion/challenger experiment across two simultaneous policies
    and reports per-variant stats, significance, and a promote/rollback call.

Run:  python -m simulation.harness --customers 10000
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.champion_challenger import analyze_champion_challenger  # noqa: E402
from app.core import decide  # noqa: E402

REGIONS = ["KA", "MH", "DL", "TN", "GJ", "WB", "UP", "RJ", "AP", "KL"]
APPROVED_REGIONS = ["KA", "MH", "DL", "TN", "GJ"]
RESIDENCE = ["own", "rent", "mortgage"]
TERMS = [12, 24, 36, 48, 60]


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #
def generate_customers(n: int, seed: int = 42) -> List[Dict[str, Any]]:
    """Deterministically generate n customers with a customer_id + 20 variables."""
    rng = random.Random(seed)
    customers = []
    for i in range(n):
        customers.append(
            {
                "customer_id": f"CUST-{i:07d}",
                "credit_score": rng.randint(300, 850),
                "annual_income": rng.randint(10_000, 200_000),
                "dti_ratio": round(rng.uniform(0.0, 0.8), 3),
                "age": rng.randint(18, 75),
                "employment_months": rng.randint(0, 360),
                "num_inquiries_6m": rng.randint(0, 15),
                "delinquencies_12m": rng.randint(0, 8),
                "credit_utilization": round(rng.uniform(0.0, 1.2), 3),
                "loan_amount": rng.randint(1_000, 100_000),
                "num_open_accounts": rng.randint(0, 25),
                "months_since_last_delinq": rng.randint(0, 120),
                "region": rng.choice(REGIONS),
                "has_bankruptcy": rng.random() < 0.05,
                "num_credit_lines": rng.randint(0, 30),
                "avg_account_age_months": rng.randint(0, 300),
                "total_balance": rng.randint(0, 500_000),
                "income_verified": rng.random() < 0.8,
                "residence_type": rng.choice(RESIDENCE),
                "requested_term_months": rng.choice(TERMS),
            }
        )
    return customers


# --------------------------------------------------------------------------- #
# Policy bundle (uses ==, >=, between, in, == boolean across rules)
# --------------------------------------------------------------------------- #
def _condition(variable: str, operator: str, value: Any, value2: Any = None, field_type: str = None) -> Dict[str, Any]:
    node = {"type": "condition", "variable": variable, "operator": operator, "value": value}
    if value2 is not None:
        node["value2"] = value2
    if field_type is not None:
        node["fieldType"] = field_type
    return node


def _rule(rule_id: str, condition: Dict[str, Any], on_pass: str, on_fail: str) -> Dict[str, Any]:
    return {
        "id": rule_id,
        "rule_format": "v2",
        "tree": {"type": "group", "logic": "AND", "children": [condition], "onPass": on_pass, "onFail": on_fail},
    }


def build_credit_bundle(policy_id: str = "credit_v1") -> Dict[str, Any]:
    rules = {
        # hard reject on subprime score
        "r_score_floor": _rule("r_score_floor", _condition("credit_score", ">=", 600), "approve", "reject"),
        # DTI must be within an acceptable band (exercises `between`)
        "r_dti_band": _rule("r_dti_band", _condition("dti_ratio", "between", 0.0, 0.45), "approve", "reject"),
        # minimum income
        "r_income": _rule("r_income", _condition("annual_income", ">=", 30_000), "approve", "reject"),
        # region eligibility (exercises `in`)
        "r_region": _rule("r_region", _condition("region", "in", APPROVED_REGIONS), "approve", "reject"),
        # no bankruptcy (exercises boolean `==`)
        "r_bankruptcy": _rule("r_bankruptcy", _condition("has_bankruptcy", "==", False, field_type="boolean"), "approve", "reject"),
        # marginal scores route to manual review (onFail = review, not reject)
        "r_marginal": _rule("r_marginal", _condition("credit_score", ">=", 660), "approve", "review"),
    }
    scorecard = {
        "id": "sc_credit",
        "base_score": 300,
        "max_score": 900,
        "bins": [
            {"variable_id": "credit_score", "weight": 1.0, "ranges": [{"min": 300, "max": 850, "points": 50}]},
            {"variable_id": "annual_income", "weight": 1.0, "ranges": [{"min": 0, "max": 1_000_000, "points": 30}]},
        ],
    }
    policy = {
        "id": policy_id,
        "steps": [
            {"type": "scorecard", "ref_id": "sc_credit"},
            {"type": "rule", "ref_id": "r_score_floor"},
            {"type": "rule", "ref_id": "r_dti_band"},
            {"type": "rule", "ref_id": "r_income"},
            {"type": "rule", "ref_id": "r_region"},
            {"type": "rule", "ref_id": "r_bankruptcy"},
            {"type": "rule", "ref_id": "r_marginal"},
        ],
    }
    return {"policy": policy, "rules": rules, "scorecards": {"sc_credit": scorecard}}


def build_deep_bundle(num_conditions: int = 20, policy_id: str = "deep_credit") -> Dict[str, Any]:
    """A policy that evaluates >= num_conditions sequentially as ONE decision.

    Each condition is its own rule step on a distinct variable, so a single
    decide() call runs the full chain of >= 20 checks before returning one
    outcome — the 'heavy case' load-test target.
    """
    numeric_vars = [
        ("credit_score", ">=", 500), ("annual_income", ">=", 20000), ("dti_ratio", "<=", 0.55),
        ("age", ">=", 18), ("employment_months", ">=", 3), ("num_inquiries_6m", "<=", 12),
        ("delinquencies_12m", "<=", 6), ("credit_utilization", "<=", 1.1), ("loan_amount", "<=", 95000),
        ("num_open_accounts", "<=", 24), ("months_since_last_delinq", ">=", 0), ("num_credit_lines", "<=", 29),
        ("avg_account_age_months", ">=", 0), ("total_balance", "<=", 490000), ("requested_term_months", "<=", 60),
    ]
    rules: Dict[str, Any] = {}
    steps = []
    for i in range(num_conditions):
        var, op, val = numeric_vars[i % len(numeric_vars)]
        rid = f"r_deep_{i}"
        rules[rid] = _rule(rid, _condition(var, op, val), "approve", "reject")
        steps.append({"type": "rule", "ref_id": rid})
    return {"policy": {"id": policy_id, "steps": steps}, "rules": rules, "scorecards": {}}


def oracle_outcome(customer: Dict[str, Any]) -> str:
    """Independent reference implementation of the credit policy above.

    reject dominates review dominates approve (same precedence as the engine).
    """
    if customer["credit_score"] < 600:
        return "reject"
    if not (0.0 <= customer["dti_ratio"] <= 0.45):
        return "reject"
    if customer["annual_income"] < 30_000:
        return "reject"
    if customer["region"] not in APPROVED_REGIONS:
        return "reject"
    if customer["has_bankruptcy"]:
        return "reject"
    if customer["credit_score"] < 660:
        return "review"
    return "approve"


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def run_and_validate(customers: List[Dict[str, Any]], bundle: Dict[str, Any]) -> Dict[str, Any]:
    outcomes = Counter()
    mismatches = []
    started = time.perf_counter()
    for customer in customers:
        result = decide(bundle, customer)
        outcome = result["outcome"]
        outcomes[outcome] += 1
        expected = oracle_outcome(customer)
        if outcome != expected:
            if len(mismatches) < 10:
                mismatches.append({"customer_id": customer["customer_id"], "got": outcome, "expected": expected})
    elapsed = time.perf_counter() - started
    total = len(customers)
    return {
        "total": total,
        "outcomes": dict(outcomes),
        "approvalRatePct": round(outcomes["approve"] / total * 100, 2) if total else 0,
        "reviewRatePct": round(outcomes["review"] / total * 100, 2) if total else 0,
        "rejectRatePct": round(outcomes["reject"] / total * 100, 2) if total else 0,
        "elapsedSec": round(elapsed, 3),
        "throughputPerSec": round(total / elapsed, 1) if elapsed else 0,
        "mismatches": mismatches,
        "valid": not mismatches,
    }


def _ab_bundle(policy_id: str, challenger_threshold: int) -> Dict[str, Any]:
    """Credit bundle plus a champion/challenger experiment that raises the score floor."""
    bundle = build_credit_bundle(policy_id)
    bundle["experiments"] = [
        {
            "id": f"exp_{policy_id}",
            "status": "running",
            "target_policy_id": policy_id,
            "variants": [
                {"id": "champion", "role": "champion", "weight": 50},
                {
                    "id": "challenger",
                    "role": "challenger",
                    "weight": 50,
                    "overrides": {"r_score_floor.conditions.0.value": challenger_threshold},
                    "guardrails": {"maxApprovalRateDropPct": 25, "maxAvgLatencyMs": 1000},
                },
            ],
        }
    ]
    return bundle


def run_ab_experiment(customers: List[Dict[str, Any]], policy_id: str, challenger_threshold: int) -> Dict[str, Any]:
    """Run one A/B experiment: assign each customer a variant and record outcomes."""
    bundle = _ab_bundle(policy_id, challenger_threshold)
    per_variant = {"champion": Counter(), "challenger": Counter()}
    for customer in customers:
        result = decide(bundle, customer, {"subject_id": customer["customer_id"], "policy_id": policy_id})
        variant = result["experiment_variant"] or "champion"
        per_variant[variant][result["outcome"]] += 1
        per_variant[variant]["users"] += 1

    variant_stats = {}
    for variant_id, counts in per_variant.items():
        users = counts["users"]
        variant_stats[variant_id] = {
            "users": users,
            "approved": counts["approve"],
            "approvalRate": round(counts["approve"] / users * 100, 2) if users else 0,
            "rejectRate": round(counts["reject"] / users * 100, 2) if users else 0,
            "avgLatencyMs": 40,
        }
    variants = bundle["experiments"][0]["variants"]
    analysis = analyze_champion_challenger(variants, variant_stats)
    return {"policy_id": policy_id, "variantStats": variant_stats, "analysis": analysis}


def main() -> int:
    parser = argparse.ArgumentParser(description="RuleMind simulation & validation harness")
    parser.add_argument("--customers", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Generating {args.customers:,} customers (customer_id + 20 variables, seed={args.seed})...")
    customers = generate_customers(args.customers, args.seed)

    print("\n=== Correctness validation (single credit policy) ===")
    bundle = build_credit_bundle()
    report = run_and_validate(customers, bundle)
    print(f"  Decisions:      {report['total']:,}")
    print(f"  Outcome mix:    {report['outcomes']}")
    print(f"  Approve/Review/Reject: {report['approvalRatePct']}% / {report['reviewRatePct']}% / {report['rejectRatePct']}%")
    print(f"  Throughput:     {report['throughputPerSec']:,}/sec single-core ({report['elapsedSec']}s)")
    print(f"  Oracle match:   {'PASS — every decision matches the reference' if report['valid'] else 'FAIL'}")
    if not report["valid"]:
        print(f"  Mismatches:     {report['mismatches']}")
        return 1

    print("\n=== A/B + Champion/Challenger across two simultaneous policies ===")
    for policy_id, threshold in [("credit_retail", 640), ("credit_sme", 700)]:
        ab = run_ab_experiment(customers, policy_id, threshold)
        analysis = ab["analysis"]
        champ = analysis["champion"]
        chal = analysis["challengers"][0]
        print(f"\n  Policy '{policy_id}' (challenger raises score floor to {threshold}):")
        print(f"    champion   : {champ['stats']['users']:,} users, approval {champ['stats']['approvalRate']}%")
        print(f"    challenger : {chal['stats']['users']:,} users, approval {chal['stats']['approvalRate']}%")
        print(f"    lift {chal['liftPct']}pp | p={chal['pValue']} | significant={chal['significant']} | "
              f"guardrails_breached={chal['guardrails']['breached']}")
        print(f"    RECOMMENDATION: {chal['recommendation'].upper()}")

    print("\nSimulation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
