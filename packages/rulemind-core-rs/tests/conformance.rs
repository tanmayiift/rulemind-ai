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
    let approve = compiled.decide_from_payload(&json!({"amount": 3000, "income": 10000}));
    assert_eq!(approve, "approve");
    // 8000 / 10000 = 0.8 -> condition fails -> onFail -> reject
    let reject = compiled.decide_from_payload(&json!({"amount": 8000, "income": 10000}));
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
        if compiled.decide_from_payload(&payload) == "approve" {
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
