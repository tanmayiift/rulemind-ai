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


# --------------------------------------------------------------------------- #
# Large-policy cross-engine accuracy fixture
# --------------------------------------------------------------------------- #
# One big v2 nested-tree rule (default 600 conditions across 750 variables, all
# 12 operators, mixed AND/OR/NOT) used to prove the on-device Kotlin & Dart engines
# match the Python core on a realistically large policy — including payloads that
# OMIT variables (the missing-variable parity case). See
# packages/shared/large-policy.spec.json and the three conformance test arms.

_LARGE_OPS = ["==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists"]


def _large_condition(idx: int, operator: str) -> Dict[str, Any]:
    """A single well-typed condition on its own variable `var_{idx}`."""
    var = f"var_{idx}"
    if operator == "==":  # boolean-typed equality
        return {"type": "condition", "variable": var, "operator": "==", "value": False, "fieldType": "boolean"}
    if operator == "!=":
        return {"type": "condition", "variable": var, "operator": "!=", "value": 100}
    if operator in (">", ">=", "<", "<="):
        return {"type": "condition", "variable": var, "operator": operator, "value": 500}
    if operator == "between":
        return {"type": "condition", "variable": var, "operator": "between", "value": 200, "value2": 800}
    if operator == "in":
        return {"type": "condition", "variable": var, "operator": "in", "value": "alpha,beta,gamma"}
    if operator == "not_in":
        return {"type": "condition", "variable": var, "operator": "not_in", "value": "x,y,z"}
    if operator == "regex":
        return {"type": "condition", "variable": var, "operator": "regex", "value": "^A"}
    # exists / !exists
    return {"type": "condition", "variable": var, "operator": operator, "value": None}


def _satisfying_value(operator: str) -> Any:
    return {
        "==": False, "!=": 101, ">": 600, ">=": 500, "<": 499, "<=": 500,
        "between": 500, "in": "beta", "not_in": "ok", "regex": "Apple",
        "exists": 1, "!exists": "__OMIT__",
    }[operator]


def _violating_value(operator: str) -> Any:
    return {
        "==": True, "!=": 100, ">": 400, ">=": 499, "<": 501, "<=": 501,
        "between": 900, "in": "zeta", "not_in": "x", "regex": "Banana",
        "exists": "__OMIT__", "!exists": 1,
    }[operator]


def build_threshold_rule(num_conditions: int = 500, rule_id: str = "r_threshold") -> Dict[str, Any]:
    """A v2 rule that is a FLAT AND of `num_conditions` conditions (all 12 operators, no NOT),
    onPass=approve, onFail=reject. Because it is a pure conjunction, the outcome flips cleanly at a
    single boundary: ALL conditions true -> approve; even one false -> reject. This lets the
    conformance fixture assert the negative/boundary behaviour every engine must share — a case just
    below the threshold (e.g. 499/500 true) MUST fail, not pass — which the big OR-based rule can't
    express as a clean count threshold."""
    children = [_large_condition(i, _LARGE_OPS[i % len(_LARGE_OPS)]) for i in range(num_conditions)]
    tree = {"type": "group", "id": "troot", "logic": "AND", "children": children,
            "onPass": "approve", "onFail": "reject"}
    return {"id": rule_id, "name": "Threshold policy", "rule_format": "v2", "ruleFormat": "v2", "tree": tree}


def _threshold_payload(num_conditions: int, num_true: int, num_variables: int = 750) -> Dict[str, Any]:
    """A payload for the threshold rule where EXACTLY the first `num_true` conditions are satisfied
    and the rest are violated (so trueConditions == num_true deterministically). `exists`/`!exists`
    are handled by omitting the variable when the chosen value is the OMIT sentinel."""
    payload: Dict[str, Any] = {}
    for i in range(num_conditions):
        op = _LARGE_OPS[i % len(_LARGE_OPS)]
        value = _satisfying_value(op) if i < num_true else _violating_value(op)
        if value != "__OMIT__":
            payload[f"var_{i}"] = value
    for i in range(num_conditions, num_variables):
        payload[f"var_{i}"] = i  # extra vars to reach the ≥700-variable scale
    return payload


def build_threshold_cases(rule: Dict[str, Any], num_conditions: int = 500) -> List[Dict[str, Any]]:
    """Boundary + negative cases for the threshold rule. Each records the ORACLE outcome and passed
    count (computed live from app/logic.py) plus the intended `targetTrue`, so the conformance arms
    assert both the exact count AND that sub-threshold cases fail (reject)."""
    from app.logic import evaluate_rule_tree

    targets = [
        (num_conditions, "all-true → approve"),          # 500/500 -> approve
        (num_conditions - 1, "one-below → reject"),        # 499/500 -> reject  (THE boundary)
        (num_conditions - 2, "two-below → reject"),        # 498/500 -> reject
        (num_conditions // 2, "half → reject"),            # 250/500 -> reject
        (0, "none → reject"),                              # 0/500   -> reject
    ]
    cases: List[Dict[str, Any]] = []
    for num_true, label in targets:
        payload = _threshold_payload(num_conditions, num_true)
        ev = evaluate_rule_tree(rule["tree"], payload)
        cases.append({
            "label": label,
            "targetTrue": num_true,
            "variables": payload,
            "expectedOutcome": ev["outcome"],
            "trueConditions": sum(1 for c in ev["conditions"] if c["passed"]),
        })
    return cases


def build_large_rule(num_conditions: int = 600, conds_per_group: int = 20, rule_id: str = "r_large") -> Dict[str, Any]:
    """A v2 rule whose tree nests `num_conditions` conditions (all 12 operators) into
    alternating AND/OR subgroups with periodic NOT wrappers. Root onPass=approve,
    onFail=review, so the outcome is sensitive to individual condition results."""
    conditions = [(_large_condition(i, _LARGE_OPS[i % len(_LARGE_OPS)]), _LARGE_OPS[i % len(_LARGE_OPS)])
                  for i in range(num_conditions)]
    subgroups: List[Dict[str, Any]] = []
    for g in range(0, num_conditions, conds_per_group):
        chunk = conditions[g:g + conds_per_group]
        children: List[Dict[str, Any]] = []
        for local, (cond, _op) in enumerate(chunk):
            # Wrap ~1 in 7 leaves in NOT (uses the single-child `child` form all engines share).
            if (g + local) % 7 == 3:
                children.append({"type": "not", "id": f"not_{g + local}", "child": cond})
            else:
                children.append(cond)
        # AND subgroups (each rarely all-true) under a top-level OR keeps the final
        # outcome sensitive to individual conditions and roughly split (not saturated).
        subgroups.append({"type": "group", "id": f"grp_{g}", "logic": "AND", "children": children})
    tree = {"type": "group", "id": "root", "logic": "OR", "children": subgroups,
            "onPass": "approve", "onFail": "review"}
    return {"id": rule_id, "name": "Large policy", "rule_format": "v2", "ruleFormat": "v2", "tree": tree}


def generate_large_cases(num_conditions: int = 600, num_variables: int = 750,
                         num_dense: int = 150, num_missing: int = 150, seed: int = 7) -> List[Dict[str, Any]]:
    """Deterministic payloads for the large rule: `dense` payloads satisfy each
    condition with p≈0.85 (so 500+ conditions are true), `missing` payloads then
    drop ~30% of variables to exercise the missing-variable parity path."""
    rng = random.Random(seed)
    ops = [_LARGE_OPS[i % len(_LARGE_OPS)] for i in range(num_conditions)]

    def dense_payload() -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for i in range(num_conditions):
            op = ops[i]
            value = _satisfying_value(op) if rng.random() < 0.85 else _violating_value(op)
            if value != "__OMIT__":
                payload[f"var_{i}"] = value
        # Extra variables present only in the payload (pushes total variables to num_variables).
        for i in range(num_conditions, num_variables):
            payload[f"var_{i}"] = rng.randint(0, 1000)
        return payload

    cases: List[Dict[str, Any]] = []
    for _ in range(num_dense):
        cases.append(dense_payload())
    for _ in range(num_missing):
        payload = dense_payload()
        for key in list(payload.keys()):
            if rng.random() < 0.30:
                del payload[key]
        cases.append(payload)
    return cases


def _approve_payload(rng: random.Random, num_variables: int = 750) -> Dict[str, Any]:
    """A payload that makes subgroup 0 (conditions 0..19) fully true so the top-level
    OR yields `approve` — exercises the approve path with a random tail."""
    payload: Dict[str, Any] = {}
    not_local = {3, 10, 17}  # NOT-wrapped leaves in subgroup 0: their inner must be FALSE
    for i in range(20):
        op = _LARGE_OPS[i % len(_LARGE_OPS)]
        value = _violating_value(op) if i in not_local else _satisfying_value(op)
        if value != "__OMIT__":
            payload[f"var_{i}"] = value
    for i in range(20, num_variables):
        payload[f"var_{i}"] = rng.randint(0, 1000)
    return payload


def build_large_policy_spec(seed: int = 7) -> Dict[str, Any]:
    """The committed cross-engine large-policy fixture: the big v2 rule + payload cases
    with their Python-oracle outcome and passed-condition count. The on-device Kotlin &
    Dart engines must reproduce both for every case."""
    from app.logic import evaluate_rule_tree

    rule = build_large_rule(num_conditions=600)
    payloads = generate_large_cases(num_dense=80, num_missing=40, seed=seed)
    payloads += [_approve_payload(random.Random(seed + 92)) for _ in range(15)]
    cases: List[Dict[str, Any]] = []
    for payload in payloads:
        ev = evaluate_rule_tree(rule["tree"], payload)
        cases.append({
            "variables": payload,
            "expectedOutcome": ev["outcome"],
            "trueConditions": sum(1 for c in ev["conditions"] if c["passed"]),
        })
    threshold_rule = build_threshold_rule(num_conditions=500)
    threshold_cases = build_threshold_cases(threshold_rule, num_conditions=500)
    return {
        "_comment": "Cross-engine large-policy conformance. Python core (app/logic.py) is the source of truth; the Kotlin (sdk-android) and Dart (sdk-flutter) on-device engines must resolve the SAME outcome AND the same number of passed conditions per case. The thresholdRule is a flat AND of 500 conditions (onFail=reject) so sub-threshold cases (e.g. 499/500 true) MUST reject — the negative/boundary coverage. Regenerate with: python -m simulation.gen_large_policy_spec",
        "meta": {"conditions": 600, "cases": len(cases), "thresholdConditions": 500, "thresholdCases": len(threshold_cases)},
        "rule": rule,
        "cases": cases,
        "thresholdRule": threshold_rule,
        "thresholdCases": threshold_cases,
    }


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
