//! RuleMind decision eval-core in Rust.
//!
//! Pure logic operates on `serde_json::Value` and has **no** Python/PyO3
//! dependency, so it compiles to native and to WASM (`--no-default-features`).
//! The optional `python` feature adds a PyO3 extension module. Operator
//! semantics are the same contract every other engine follows —
//! `packages/shared/operators.spec.json` — so this is the 5th conforming engine.

use regex::Regex;
use serde_json::Value;

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        _ => v.to_string(),
    }
}

/// Coerce a JSON value to f64 (numbers, or numeric strings).
pub fn to_number(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Coerce to bool: real bool, non-zero number, or "true"/"1"/"yes".
pub fn to_bool(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Value::String(s) => matches!(s.trim().to_lowercase().as_str(), "true" | "1" | "yes"),
        _ => false,
    }
}

fn loose_equal(a: &Value, b: &Value) -> bool {
    if a == b {
        return true;
    }
    if let (Some(x), Some(y)) = (to_number(a), to_number(b)) {
        return x == y;
    }
    value_to_string(a) == value_to_string(b)
}

fn option_list(expected: &Value) -> Vec<Value> {
    match expected {
        Value::Array(a) => a.clone(),
        Value::Null => Vec::new(),
        _ => value_to_string(expected)
            .split(',')
            .map(|s| Value::String(s.trim().to_string()))
            .filter(|v| !value_to_string(v).is_empty())
            .collect(),
    }
}

/// Evaluate a single condition. Mirrors packages/shared/operators.spec.json.
pub fn compare(
    actual: &Value,
    operator: &str,
    expected: &Value,
    expected2: &Value,
    field_type: Option<&str>,
) -> bool {
    match operator {
        "exists" => !actual.is_null() && !value_to_string(actual).is_empty(),
        "!exists" => actual.is_null() || value_to_string(actual).is_empty(),
        "in" | "not_in" => {
            let matched = option_list(expected).iter().any(|o| loose_equal(actual, o));
            if operator == "in" {
                matched
            } else {
                !matched
            }
        }
        "regex" => {
            if actual.is_null() {
                return false;
            }
            match Regex::new(&value_to_string(expected)) {
                Ok(re) => re.is_match(&value_to_string(actual)),
                Err(_) => false,
            }
        }
        _ => {
            if field_type == Some("boolean") && (operator == "==" || operator == "!=") {
                let matched = to_bool(actual) == to_bool(expected);
                return if operator == "==" { matched } else { !matched };
            }
            if matches!(operator, ">=" | "<=" | ">" | "<" | "between") {
                let a = match to_number(actual) {
                    Some(x) => x,
                    None => return false,
                };
                let e = match to_number(expected) {
                    Some(x) => x,
                    None => return false,
                };
                return match operator {
                    ">=" => a >= e,
                    "<=" => a <= e,
                    ">" => a > e,
                    "<" => a < e,
                    "between" => match to_number(expected2) {
                        Some(u) => a >= e && a <= u,
                        None => false,
                    },
                    _ => false,
                };
            }
            match operator {
                "==" => loose_equal(actual, expected),
                "!=" => !loose_equal(actual, expected),
                _ => false,
            }
        }
    }
}

/// Evaluate a v2 rule tree node against a map of variable values.
/// Returns `true` when the node passes.
pub fn evaluate_node(node: &Value, variables: &Value) -> bool {
    match node.get("type").and_then(Value::as_str) {
        Some("condition") => {
            let var = node.get("variable").and_then(Value::as_str).unwrap_or("");
            let actual = variables.get(var).cloned().unwrap_or(Value::Null);
            let operator = node.get("operator").and_then(Value::as_str).unwrap_or("==");
            let expected = node.get("value").cloned().unwrap_or(Value::Null);
            let expected2 = node.get("value2").cloned().unwrap_or(Value::Null);
            let field_type = node.get("fieldType").and_then(Value::as_str);
            compare(&actual, operator, &expected, &expected2, field_type)
        }
        Some("not") => {
            let child = node.get("child");
            match child {
                Some(c) => !evaluate_node(c, variables),
                None => false,
            }
        }
        _ => {
            let logic = node
                .get("logic")
                .and_then(Value::as_str)
                .unwrap_or("AND")
                .to_uppercase();
            let empty = Vec::new();
            let children = node.get("children").and_then(Value::as_array).unwrap_or(&empty);
            if logic == "OR" {
                children.iter().any(|c| evaluate_node(c, variables))
            } else {
                children.iter().all(|c| evaluate_node(c, variables))
            }
        }
    }
}

/// Evaluate a full rule tree; returns the resolved outcome string.
pub fn evaluate_tree(tree: &Value, variables: &Value) -> String {
    let passed = evaluate_node(tree, variables);
    let key = if passed { "onPass" } else { "onFail" };
    tree.get(key)
        .and_then(Value::as_str)
        .unwrap_or(if passed { "approve" } else { "reject" })
        .to_string()
}

/// Outcome precedence merge — mirrors the Python executor.
pub fn merge_outcome(current: &str, candidate: &str) -> String {
    fn rank(o: &str) -> i32 {
        match o {
            "pending" => 0,
            "pass" => 1,
            "approve" => 2,
            "review" => 3,
            "reject" => 4,
            _ => 0,
        }
    }
    if rank(candidate) > rank(current) {
        candidate.to_string()
    } else if rank(candidate) < rank(current) {
        current.to_string()
    } else if current == "pass" && candidate == "approve" {
        candidate.to_string()
    } else {
        current.to_string()
    }
}

use std::collections::HashMap;

/// A bundle parsed **once** and evaluated against many payloads — this is the
/// hot-path interface where Rust actually pays off (no per-decision JSON parse,
/// only the payload crosses the boundary).
pub struct CompiledBundle {
    policy: Value,
    rules: HashMap<String, Value>,
}

impl CompiledBundle {
    pub fn from_json(bundle_json: &str) -> Result<Self, serde_json::Error> {
        let bundle: Value = serde_json::from_str(bundle_json)?;
        let policy = bundle.get("policy").cloned().unwrap_or(Value::Null);
        let mut rules = HashMap::new();
        if let Some(map) = bundle.get("rules").and_then(Value::as_object) {
            for (key, value) in map {
                rules.insert(key.clone(), value.clone());
            }
        }
        Ok(Self { policy, rules })
    }

    /// Run the policy's rule/outcome steps over a variable map; returns the outcome.
    pub fn decide(&self, variables: &Value) -> String {
        let mut outcome = String::from("pending");
        if let Some(steps) = self.policy.get("steps").and_then(Value::as_array) {
            for step in steps {
                let stype = step.get("type").and_then(Value::as_str).unwrap_or("");
                let ref_id = step
                    .get("ref_id")
                    .or_else(|| step.get("ref"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                match stype {
                    "rule" => {
                        if let Some(rule) = self.rules.get(ref_id) {
                            if let Some(tree) = rule.get("tree") {
                                let oc = evaluate_tree(tree, variables);
                                outcome = merge_outcome(&outcome, &oc);
                            }
                        }
                    }
                    "outcome" => {
                        let oc = step
                            .get("ref_id")
                            .and_then(Value::as_str)
                            .or_else(|| step.get("label").and_then(Value::as_str))
                            .unwrap_or("review");
                        outcome = merge_outcome(&outcome, oc);
                    }
                    _ => {}
                }
            }
        }
        if outcome == "pending" {
            String::from("review")
        } else {
            outcome
        }
    }
}

// --------------------------------------------------------------------------
// Python extension module (optional)
// --------------------------------------------------------------------------
#[cfg(feature = "python")]
mod python {
    use super::*;
    use pyo3::prelude::*;
    use pyo3::types::{PyAny, PyDict, PyList};

    fn py_to_value(obj: &Bound<'_, PyAny>) -> Value {
        if obj.is_none() {
            return Value::Null;
        }
        if let Ok(b) = obj.extract::<bool>() {
            return Value::Bool(b);
        }
        if let Ok(i) = obj.extract::<i64>() {
            return Value::from(i);
        }
        if let Ok(f) = obj.extract::<f64>() {
            return Value::from(f);
        }
        if let Ok(s) = obj.extract::<String>() {
            return Value::String(s);
        }
        if let Ok(dict) = obj.downcast::<PyDict>() {
            let mut map = serde_json::Map::new();
            for (key, value) in dict.iter() {
                let k = key
                    .extract::<String>()
                    .unwrap_or_else(|_| key.str().map(|s| s.to_string()).unwrap_or_default());
                map.insert(k, py_to_value(&value));
            }
            return Value::Object(map);
        }
        if let Ok(list) = obj.downcast::<PyList>() {
            return Value::Array(list.iter().map(|item| py_to_value(&item)).collect());
        }
        Value::Null
    }

    /// A bundle compiled once and reused across decisions — the fast hot path.
    #[pyclass]
    struct Bundle {
        inner: crate::CompiledBundle,
    }

    #[pymethods]
    impl Bundle {
        #[new]
        fn new(bundle_json: &str) -> PyResult<Self> {
            Ok(Self {
                inner: crate::CompiledBundle::from_json(bundle_json)
                    .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?,
            })
        }

        /// Decide over a payload dict (used directly as the variable map).
        fn decide(&self, payload: &Bound<'_, PyAny>) -> String {
            let vars = py_to_value(payload);
            self.inner.decide(&vars)
        }
    }

    #[pyfunction]
    #[pyo3(name = "compare", signature = (actual, operator, expected, expected2=None, field_type=None))]
    fn py_compare(
        actual: &Bound<'_, PyAny>,
        operator: &str,
        expected: &Bound<'_, PyAny>,
        expected2: Option<&Bound<'_, PyAny>>,
        field_type: Option<&str>,
    ) -> bool {
        let a = py_to_value(actual);
        let e = py_to_value(expected);
        let e2 = expected2.map(py_to_value).unwrap_or(Value::Null);
        crate::compare(&a, operator, &e, &e2, field_type)
    }

    #[pyfunction]
    #[pyo3(name = "evaluate_tree")]
    fn py_evaluate_tree(tree_json: &str, variables_json: &str) -> PyResult<String> {
        let tree: Value = serde_json::from_str(tree_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let vars: Value = serde_json::from_str(variables_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        Ok(crate::evaluate_tree(&tree, &vars))
    }

    #[pymodule]
    fn rulemind_core_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(py_compare, m)?)?;
        m.add_function(wrap_pyfunction!(py_evaluate_tree, m)?)?;
        m.add_class::<Bundle>()?;
        Ok(())
    }
}

// --------------------------------------------------------------------------
// WASM (wasm-bindgen) interface for edge / browser / on-device
// --------------------------------------------------------------------------
#[cfg(feature = "wasm")]
mod wasm_bindings {
    use super::*;
    use wasm_bindgen::prelude::*;

    /// Evaluate a single condition. Inputs are JSON strings.
    #[wasm_bindgen(js_name = compare)]
    pub fn compare_wasm(
        actual_json: &str,
        operator: &str,
        expected_json: &str,
        expected2_json: &str,
        field_type: Option<String>,
    ) -> bool {
        let a: Value = serde_json::from_str(actual_json).unwrap_or(Value::Null);
        let e: Value = serde_json::from_str(expected_json).unwrap_or(Value::Null);
        let e2: Value = serde_json::from_str(expected2_json).unwrap_or(Value::Null);
        crate::compare(&a, operator, &e, &e2, field_type.as_deref())
    }

    /// Evaluate a v2 rule tree; returns the outcome string.
    #[wasm_bindgen(js_name = evaluateTree)]
    pub fn evaluate_tree_wasm(tree_json: &str, variables_json: &str) -> String {
        let tree: Value = serde_json::from_str(tree_json).unwrap_or(Value::Null);
        let vars: Value = serde_json::from_str(variables_json).unwrap_or(Value::Null);
        crate::evaluate_tree(&tree, &vars)
    }

    /// Decide a compiled bundle against a payload; returns the outcome string.
    #[wasm_bindgen(js_name = decide)]
    pub fn decide_wasm(bundle_json: &str, payload_json: &str) -> String {
        match crate::CompiledBundle::from_json(bundle_json) {
            Ok(bundle) => {
                let vars: Value = serde_json::from_str(payload_json).unwrap_or(Value::Null);
                bundle.decide(&vars)
            }
            Err(_) => String::from("error"),
        }
    }
}

// --------------------------------------------------------------------------
// Native Rust tests
// --------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn operators() {
        assert!(compare(&json!(750), ">=", &json!(700), &Value::Null, None));
        assert!(compare(&json!(50000), "between", &json!(40000), &json!(60000), None));
        assert!(compare(&json!("KA"), "in", &json!(["KA", "MH"]), &Value::Null, None));
        assert!(compare(&json!("ABCDE1234F"), "regex", &json!("^[A-Z]{5}[0-9]{4}[A-Z]$"), &Value::Null, None));
        assert!(!compare(&json!(650), ">=", &json!(700), &Value::Null, None));
        assert!(compare(&json!("true"), "==", &json!(true), &Value::Null, Some("boolean")));
    }

    #[test]
    fn tree() {
        let tree = json!({
            "type": "group", "logic": "AND",
            "children": [{"type": "condition", "variable": "score", "operator": ">=", "value": 700}],
            "onPass": "approve", "onFail": "reject"
        });
        assert_eq!(evaluate_tree(&tree, &json!({"score": 750})), "approve");
        assert_eq!(evaluate_tree(&tree, &json!({"score": 650})), "reject");
    }
}
