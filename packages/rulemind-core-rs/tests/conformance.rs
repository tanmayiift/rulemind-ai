//! Cross-engine conformance for the Rust decide core — the same shared specs Python, Kotlin
//! and Dart assert against. Proves the standalone Rust service produces identical decisions,
//! so the hot path can't silently drift from the source of truth.

use rulemind_core_rs::{variables, CompiledBundle};
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;

fn shared_spec(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("shared")
        .join(name);
    let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {:?}: {}", path, e));
    serde_json::from_str(&text).unwrap()
}

#[test]
fn operators_conformance() {
    let spec = shared_spec("operators.spec.json");
    let cases = spec["cases"].as_array().expect("cases");
    let mut checked = 0;
    for case in cases {
        let actual = &case["actual"];
        let operator = case["operator"].as_str().unwrap();
        let value = case.get("value").unwrap_or(&Value::Null);
        let value2 = case.get("value2").unwrap_or(&Value::Null);
        let field_type = case.get("fieldType").and_then(Value::as_str);
        let expected = case["expected"].as_bool().unwrap();
        let got = rulemind_core_rs::compare(actual, operator, value, value2, field_type);
        assert_eq!(
            got,
            expected,
            "case {}: {} {} {} (fieldType {:?})",
            case["name"], actual, operator, value, field_type
        );
        checked += 1;
    }
    assert_eq!(checked, cases.len());
    assert!(checked >= 30, "expected the full operator matrix");
}

#[test]
fn variable_vm_computes_a_ratio_then_decides() {
    // A compiled variable: ratio = amount / income, then a rule approves when ratio < 0.4.
    let bundle = json!({
        "variables": [
            {
                "id": "ratio",
                "source_id": "custom",
                "instructions": [
                    { "op": "get", "target": "a", "path": "amount" },
                    { "op": "get", "target": "b", "path": "income" },
                    { "op": "divide", "target": "r", "left": "a", "right": "b" },
                    { "op": "return", "source": "r" }
                ]
            }
        ],
        "rules": {
            "rule_dti": {
                "id": "rule_dti",
                "tree": {
                    "type": "condition", "variable": "ratio", "operator": "<", "value": 0.4,
                    "onPass": "approve", "onFail": "reject"
                }
            }
        },
        "policy": {
            "id": "p", "steps": [ { "type": "rule", "ref_id": "rule_dti" } ]
        }
    });
    let compiled = CompiledBundle::from_json(&bundle.to_string()).unwrap();

    // 3000 / 10000 = 0.3 < 0.4 -> onPass -> approve
    let approve = compiled.decide_from_payload(&json!({"amount": 3000, "income": 10000})).unwrap();
    assert_eq!(approve, "approve");
    // 8000 / 10000 = 0.8 -> condition fails -> onFail -> reject
    let reject = compiled.decide_from_payload(&json!({"amount": 8000, "income": 10000})).unwrap();
    assert_eq!(reject, "reject");
}

#[test]
fn decide_throughput_far_exceeds_1000_tps() {
    use std::time::Instant;
    let bundle = json!({
        "variables": [ { "id": "ratio", "source_id": "custom", "instructions": [
            { "op": "get", "target": "a", "path": "amount" },
            { "op": "get", "target": "b", "path": "income" },
            { "op": "divide", "target": "r", "left": "a", "right": "b" },
            { "op": "return", "source": "r" } ] } ],
        "rules": { "rule_dti": { "id": "rule_dti", "tree": {
            "type": "condition", "variable": "ratio", "operator": "<", "value": 0.4,
            "onPass": "approve", "onFail": "reject" } } },
        "policy": { "id": "p", "steps": [ { "type": "rule", "ref_id": "rule_dti" } ] }
    });
    let compiled = CompiledBundle::from_json(&bundle.to_string()).unwrap();
    let payload = json!({ "amount": 3000, "income": 10000 });
    let n = 100_000u32;
    let start = Instant::now();
    let mut sink = 0u64;
    for _ in 0..n {
        if compiled.decide_from_payload(&payload).unwrap() == "approve" {
            sink += 1;
        }
    }
    let elapsed = start.elapsed();
    let tps = n as f64 / elapsed.as_secs_f64();
    eprintln!(
        "decide throughput: {:.0}/sec single-thread ({:.0} ns/decision), approvals={}",
        tps,
        elapsed.as_nanos() as f64 / n as f64,
        sink
    );
    // The target is 1000+ TPS; a single core clears that by orders of magnitude.
    assert!(tps > 20_000.0, "single-thread decide throughput {tps:.0}/sec is unexpectedly low");
}

#[test]
fn variable_vm_missing_field_is_null_not_zero() {
    // Parity with Python compare(None, ...) == false: a missing field must not silently become 0.
    let variable = json!({
        "id": "v", "source_id": "custom",
        "instructions": [ { "op": "get", "target": "x", "path": "missing" }, { "op": "return", "source": "x" } ]
    });
    let out = variables::evaluate_variable(&variable, &json!({"present": 5}), &json!({}));
    assert!(out.is_null(), "missing field resolves to null, got {out}");
}

// ---------------------------------------------------------------------------
// LARGE-policy conformance — the Rust arm.
//
// packages/shared/large-policy.spec.json records, per case, the outcome AND the number of
// conditions that evaluated true, as produced by the Python engine (the source of truth). Until
// this engine emitted a per-condition trace it could only check the outcome, so the 135 + 5
// count assertions the Kotlin and Dart arms make had no Rust counterpart. They do now.
// Mirrors apps/python-executor/tests/test_large_policy_conformance.py and
// packages/sdk-android/.../LargePolicyConformanceTest.kt.
// ---------------------------------------------------------------------------

fn large_policy_spec() -> Value {
    shared_spec("large-policy.spec.json")
}

/// The spec's `conditionTrace` encoding: one character per condition in EVALUATION order,
/// '1' if that condition evaluated true. Per `meta.traceEncoding` the bit is the RAW condition
/// result — a condition under a `not` records what the CONDITION returned, not the negated
/// value — which is exactly what the engine's `conditions[]` entries carry.
fn trace_bits(trace: &rulemind_core_rs::RuleTrace) -> String {
    trace
        .conditions
        .iter()
        .map(|c| {
            if c["passed"].as_bool().unwrap_or(false) {
                '1'
            } else {
                '0'
            }
        })
        .collect()
}

/// The variable each condition read, in evaluation order — checked against the spec's
/// `conditionOrder` so a walk-order difference is reported as such instead of showing up as an
/// inscrutable bitstring mismatch.
fn trace_order(trace: &rulemind_core_rs::RuleTrace) -> Vec<&str> {
    trace
        .conditions
        .iter()
        .map(|c| c["variable_id"].as_str().unwrap_or(""))
        .collect()
}

fn spec_order(list: &Value) -> Vec<&str> {
    list.as_array()
        .expect("conditionOrder")
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect()
}

#[test]
fn large_policy_every_case_reproduces_outcome_and_true_count() {
    let spec = large_policy_spec();
    let tree = &spec["rule"]["tree"];
    let cases = spec["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 135, "the committed corpus is 135 cases");
    for (i, case) in cases.iter().enumerate() {
        let variables = &case["variables"];
        let trace = rulemind_core_rs::evaluate_tree_traced(tree, variables)
            .unwrap_or_else(|e| panic!("case {i}: {e}"));
        assert_eq!(
            trace.outcome,
            case["expectedOutcome"].as_str().unwrap(),
            "case {i} outcome"
        );
        assert_eq!(
            trace.passed_conditions() as u64,
            case["trueConditions"].as_u64().unwrap(),
            "case {i} passed-condition count"
        );
        assert_eq!(
            trace.conditions.len(),
            spec["meta"]["conditions"].as_u64().unwrap() as usize,
            "case {i} must visit every condition (no short-circuiting)"
        );
        // The full per-condition trace, not just its popcount: every condition must land on the
        // same true/false as the Python core, in the same evaluation order.
        assert_eq!(
            trace_bits(&trace),
            case["conditionTrace"].as_str().unwrap(),
            "case {i} per-condition trace"
        );
    }
}

/// The engine's condition walk order must be the spec's, so the bitstrings above are being
/// compared position-for-position and not accidentally agreeing on a permutation.
#[test]
fn large_policy_condition_walk_order_matches_the_spec() {
    let spec = large_policy_spec();
    for (rule_key, order_key) in [
        ("rule", "conditionOrder"),
        ("thresholdRule", "thresholdConditionOrder"),
    ] {
        let case = if rule_key == "rule" {
            &spec["cases"][0]
        } else {
            &spec["thresholdCases"][0]
        };
        let trace =
            rulemind_core_rs::evaluate_tree_traced(&spec[rule_key]["tree"], &case["variables"])
                .unwrap();
        assert_eq!(
            trace_order(&trace),
            spec_order(&spec[order_key]),
            "{rule_key}: depth-first, children-in-declaration-order walk"
        );
    }
}

#[test]
fn large_policy_threshold_boundary_and_negatives() {
    let spec = large_policy_spec();
    let tree = &spec["thresholdRule"]["tree"];
    let n = spec["meta"]["thresholdConditions"].as_u64().unwrap();
    let mut by_target: std::collections::HashMap<u64, String> = std::collections::HashMap::new();
    for (i, case) in spec["thresholdCases"].as_array().unwrap().iter().enumerate() {
        let label = case["label"].as_str().unwrap_or("");
        let trace = rulemind_core_rs::evaluate_tree_traced(tree, &case["variables"]).unwrap();
        assert_eq!(
            trace.outcome,
            case["expectedOutcome"].as_str().unwrap(),
            "threshold case {i} ({label}) outcome"
        );
        assert_eq!(
            trace.passed_conditions() as u64,
            case["trueConditions"].as_u64().unwrap(),
            "threshold case {i} ({label}) count"
        );
        assert_eq!(
            trace.passed_conditions() as u64,
            case["targetTrue"].as_u64().unwrap(),
            "threshold case {i} exact target count"
        );
        assert_eq!(
            trace_bits(&trace),
            case["conditionTrace"].as_str().unwrap(),
            "threshold case {i} ({label}) per-condition trace"
        );
        by_target.insert(case["targetTrue"].as_u64().unwrap(), trace.outcome);
    }
    assert_eq!(by_target[&n], "approve", "all-true must approve");
    assert_eq!(by_target[&(n - 1)], "reject", "one-below-threshold MUST reject");
    assert_eq!(by_target[&0], "reject", "none-true must reject");
}

#[test]
fn large_policy_trace_covers_all_twelve_operators() {
    // The corpus is only meaningful if the trace actually walks every operator; assert the same
    // 12-operator coverage the Python arm does.
    let spec = large_policy_spec();
    let mut ops = std::collections::BTreeSet::new();
    fn walk(node: &Value, ops: &mut std::collections::BTreeSet<String>) {
        if node.get("type").and_then(Value::as_str) == Some("condition") {
            ops.insert(node["operator"].as_str().unwrap_or("").to_string());
        }
        if let Some(children) = node.get("children").and_then(Value::as_array) {
            for child in children {
                walk(child, ops);
            }
        }
        if let Some(child) = node.get("child") {
            walk(child, ops);
        }
    }
    walk(&spec["rule"]["tree"], &mut ops);
    let expected: std::collections::BTreeSet<String> = [
        "==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(ops, expected);
}
