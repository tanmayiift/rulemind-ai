//! RuleMind decision eval-core in Rust.
//!
//! Pure logic operates on `serde_json::Value` and has **no** Python/PyO3
//! dependency, so it compiles to native and to WASM (`--no-default-features`).
//! The optional `python` feature adds a PyO3 extension module. Operator
//! semantics are the same contract every other engine follows —
//! `packages/shared/operators.spec.json` — so this is the 5th conforming engine.

use regex::Regex;
use serde_json::{json, Value};

pub mod variables;

/// Max rule-tree nesting depth. Mirrors `app/logic.py::MAX_RULE_TREE_DEPTH`, the Kotlin
/// `RuleEvaluator.MAX_RULE_TREE_DEPTH` and the Dart `maxRuleTreeDepth`. Real trees are a
/// handful deep; without this an adversarial (author-supplied) tree recursed straight into a
/// native stack overflow, which in Rust **aborts the process** — in `rulemind-decide-service`
/// that killed the whole server rather than failing one request.
pub const MAX_RULE_TREE_DEPTH: usize = 200;

/// Max input length for the `regex` operator, in characters. Mirrors
/// `app/logic.py::_REGEX_INPUT_MAX`. Rust's `regex` crate is linear-time so ReDoS is
/// structurally impossible here, but an unbounded input still burns CPU proportional to its
/// size — and, more importantly, an over-long input must produce the *same* result on all five
/// engines. Python fails closed (returns `False`) without attempting the match; so do we.
pub const REGEX_INPUT_MAX: usize = 4096;

/// A rule-tree evaluation failure. Evaluation itself is total (a condition that cannot be
/// compared is simply false); the only way to fail is a structurally illegal tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EvalError {
    /// Nesting exceeded [`MAX_RULE_TREE_DEPTH`].
    DepthExceeded,
}

impl std::fmt::Display for EvalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            // Same wording as the Python/Kotlin/Dart engines raise.
            EvalError::DepthExceeded => write!(
                f,
                "Rule tree nesting exceeds the maximum depth ({MAX_RULE_TREE_DEPTH})"
            ),
        }
    }
}

impl std::error::Error for EvalError {}

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        _ => v.to_string(),
    }
}

/// Coerce a JSON value to f64 (numbers, or numeric strings). Non-finite (NaN / ±Infinity) → None,
/// so `"Infinity" >= threshold` can't pass a numeric gate and NaN comparisons stay false.
pub fn to_number(v: &Value) -> Option<f64> {
    let n = match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    };
    n.filter(|f| f.is_finite())
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
    // A JSON null only loosely-equals another null (handled by `a == b` above) — never "" or any
    // other value. value_to_string(Null) renders "", so without this guard `null == ""` would be
    // true here while the Python/TS/Kotlin/Dart cores all return false (they render null as a
    // non-empty token). Guard it so a missing field never equals an empty string on the Rust path.
    if a.is_null() || b.is_null() {
        return false;
    }
    if let (Some(x), Some(y)) = (to_number(a), to_number(b)) {
        return x == y;
    }
    value_to_string(a) == value_to_string(b)
}

// First-class date type — ISO date/date-time (UTC) to epoch seconds via a strict
// manual parse + integer civil-days math (Howard Hinnant), identical to the Python,
// TS, Kotlin and Dart engines so dates order and compare equal byte-for-byte.
const DAYS_IN_MONTH: [i64; 12] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

fn is_leap_year(y: i64) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = year - if month <= 2 { 1 } else { 0 };
    let era = (if y >= 0 { y } else { y - 399 }) / 400;
    let yoe = y - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + (day - 1);
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn date_to_epoch(v: &Value) -> Option<i64> {
    if v.is_boolean() || v.is_null() {
        return None;
    }
    let raw = value_to_string(v);
    let s = raw.trim();
    if s.is_empty() {
        return None;
    }
    let (date_part, time_part) = match s.find(['T', ' ']) {
        Some(i) => (&s[..i], Some(&s[i + 1..])),
        None => (s, None),
    };
    let dparts: Vec<&str> = date_part.split('-').collect();
    if dparts.len() != 3 || dparts[0].len() != 4 {
        return None;
    }
    let year: i64 = dparts[0].parse().ok()?;
    let month: i64 = dparts[1].parse().ok()?;
    let day: i64 = dparts[2].parse().ok()?;
    let (mut hour, mut minute, mut second) = (0i64, 0i64, 0i64);
    let mut offset_secs: i64 = 0; // timezone offset applied so +05:30 and Z resolve to the same UTC epoch
    if let Some(tp0) = time_part {
        // Peel off a trailing timezone: 'Z', or a signed ±HH:MM / ±HHMM.
        let (tp, tz): (&str, Option<&str>) = if let Some(z) = tp0.find('Z') {
            (&tp0[..z], None)
        } else if let Some(sign) = tp0.rfind(['+', '-']) {
            (&tp0[..sign], Some(&tp0[sign..]))
        } else {
            (tp0, None)
        };
        let tp = match tp.find('.') {
            Some(i) => &tp[..i],
            None => tp,
        };
        let tparts: Vec<&str> = tp.split(':').collect();
        if tparts.len() < 2 || tparts.len() > 3 {
            return None;
        }
        hour = tparts[0].parse().ok()?;
        minute = tparts[1].parse().ok()?;
        if tparts.len() == 3 {
            second = tparts[2].parse().ok()?;
        }
        if let Some(tz) = tz {
            let digits: String = tz.chars().filter(|c| c.is_ascii_digit()).collect();
            if digits.len() != 4 {
                return None;
            }
            let oh: i64 = digits[0..2].parse().ok()?;
            let om: i64 = digits[2..4].parse().ok()?;
            let magnitude = oh * 3600 + om * 60;
            offset_secs = if tz.starts_with('+') { -magnitude } else { magnitude };
        }
    }
    if !(1..=12).contains(&month) {
        return None;
    }
    let dim = if month == 2 && is_leap_year(year) {
        29
    } else {
        DAYS_IN_MONTH[(month - 1) as usize]
    };
    if !(1..=dim).contains(&day) {
        return None;
    }
    if !(0..=23).contains(&hour) || !(0..=59).contains(&minute) || !(0..=59).contains(&second) {
        return None;
    }
    Some(days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second + offset_secs)
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
            let text = value_to_string(actual);
            // Input cap, for parity with app/logic.py::_safe_regex_search. Python measures
            // `len(str(actual))` in CODE POINTS, so count chars, not bytes — a multi-byte
            // string must trip the cap at the same place on both engines. The comparison is
            // strictly `>`: exactly REGEX_INPUT_MAX characters still matches; one more fails
            // CLOSED (false) without the pattern ever being applied, so an over-long input is
            // never a match even when the pattern would have matched.
            if text.chars().count() > REGEX_INPUT_MAX {
                return false;
            }
            match Regex::new(&value_to_string(expected)) {
                Ok(re) => re.is_match(&text),
                Err(_) => false,
            }
        }
        _ => {
            // `fieldType` is matched case-INSENSITIVELY. Python (`(field_type or "").lower()`),
            // Kotlin (`.lowercase()`) and Dart (`.toLowerCase()`) all normalize it; Rust used a
            // bare `== Some("boolean")`, so a field declared `"Boolean"` or `"Date"` silently
            // skipped the typed comparison here and fell through to the generic path — a
            // different answer from three of the four other engines on the same policy.
            let declared = |name: &str| field_type.is_some_and(|t| t.eq_ignore_ascii_case(name));
            if declared("boolean") && (operator == "==" || operator == "!=") {
                let matched = to_bool(actual) == to_bool(expected);
                return if operator == "==" { matched } else { !matched };
            }
            if declared("date") {
                return match (date_to_epoch(actual), date_to_epoch(expected)) {
                    (Some(a), Some(e)) => match operator {
                        "==" => a == e,
                        "!=" => a != e,
                        ">=" => a >= e,
                        "<=" => a <= e,
                        ">" => a > e,
                        "<" => a < e,
                        "between" => match date_to_epoch(expected2) {
                            Some(u) => a >= e && a <= u,
                            None => false,
                        },
                        _ => false,
                    },
                    _ => false,
                };
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

/// A rule-tree evaluation together with its per-condition / per-group trace — the same shape
/// `app/logic.py::evaluate_rule_tree` returns and the Kotlin/Dart `RuleEvaluator`s emit, so the
/// Rust engine can participate in the shared trace-level conformance corpus (the passed-condition
/// COUNT in `packages/shared/large-policy.spec.json`) instead of only the final outcome.
#[derive(Debug, Clone, PartialEq)]
pub struct RuleTrace {
    /// One entry per condition node, in evaluation order.
    pub conditions: Vec<Value>,
    /// One entry per group / NOT node, appended after its children (post-order), as in Python.
    pub group_results: Vec<Value>,
    pub passed: bool,
    pub outcome: String,
    pub logic: String,
}

impl RuleTrace {
    /// How many conditions evaluated true — the quantity `large-policy.spec.json` records as
    /// `trueConditions` and every other engine asserts against.
    pub fn passed_conditions(&self) -> usize {
        self.conditions
            .iter()
            .filter(|c| c.get("passed").and_then(Value::as_bool).unwrap_or(false))
            .count()
    }

    /// The trace as the JSON object the other engines return.
    pub fn to_json(&self) -> Value {
        json!({
            "conditions": self.conditions,
            "groupResults": self.group_results,
            "passed": self.passed,
            "outcome": self.outcome,
            "logic": self.logic,
        })
    }
}

/// Accumulator for [`RuleTrace`]. The untraced path passes `None` so the hot path stays
/// allocation-free.
#[derive(Default)]
struct TraceSink {
    conditions: Vec<Value>,
    groups: Vec<Value>,
}

fn root_logic(tree: &Value) -> String {
    tree.get("logic")
        .and_then(Value::as_str)
        .unwrap_or("AND")
        .to_uppercase()
}

/// The one recursive evaluator. `inherited_logic` is the logic of the enclosing group (or "NOT"),
/// which is what Python records as a condition's `group` field.
fn eval_node(
    node: &Value,
    variables: &Value,
    inherited_logic: &str,
    depth: usize,
    mut trace: Option<&mut TraceSink>,
) -> Result<bool, EvalError> {
    // Depth guard, identical bound and behaviour to Python/Kotlin/Dart: past the limit this is
    // an ERROR, not a false condition. Without it a deep author-supplied tree overflowed the
    // native stack, which Rust turns into a process abort rather than a catchable exception.
    if depth > MAX_RULE_TREE_DEPTH {
        return Err(EvalError::DepthExceeded);
    }
    match node.get("type").and_then(Value::as_str) {
        Some("condition") => {
            let var = node.get("variable").and_then(Value::as_str).unwrap_or("");
            let actual = variables.get(var).cloned().unwrap_or(Value::Null);
            let operator = node.get("operator").and_then(Value::as_str).unwrap_or("==");
            let expected = node.get("value").cloned().unwrap_or(Value::Null);
            let expected2 = node.get("value2").cloned().unwrap_or(Value::Null);
            let field_type = node.get("fieldType").and_then(Value::as_str);
            let passed = compare(&actual, operator, &expected, &expected2, field_type);
            if let Some(sink) = trace.as_mut() {
                sink.conditions.push(json!({
                    "variable_id": var,
                    // No variable lookup on this engine, so the label falls back to the id —
                    // exactly what Python's variable_label() does with an empty lookup.
                    "variable_name": var,
                    "source_id": Value::Null,
                    "operator": operator,
                    "threshold": expected,
                    "value": actual,
                    "passed": passed,
                    "group": inherited_logic,
                }));
            }
            Ok(passed)
        }
        Some("not") => {
            let passed = match node.get("child") {
                Some(child) => !eval_node(
                    child,
                    variables,
                    "NOT",
                    depth + 1,
                    trace.as_deref_mut(),
                )?,
                None => false,
            };
            if let Some(sink) = trace.as_mut() {
                sink.groups.push(json!({
                    "id": node.get("id").and_then(Value::as_str).unwrap_or("not"),
                    "logic": "NOT",
                    "passed": passed,
                    "childCount": 1,
                }));
            }
            Ok(passed)
        }
        _ => {
            let logic = root_logic(node);
            let empty = Vec::new();
            let children = node.get("children").and_then(Value::as_array).unwrap_or(&empty);
            // Deliberately NOT `.any()` / `.all()`: those short-circuit, and Python/Kotlin/Dart
            // evaluate every child. Short-circuiting would under-count the passed conditions the
            // shared corpus asserts on, and would swallow a depth error hiding in a later child.
            let is_or = logic == "OR";
            let mut passed = !is_or;
            for child in children {
                let result = eval_node(
                    child,
                    variables,
                    &logic,
                    depth + 1,
                    trace.as_deref_mut(),
                )?;
                passed = if is_or { passed || result } else { passed && result };
            }
            if let Some(sink) = trace.as_mut() {
                sink.groups.push(json!({
                    "id": node.get("id").and_then(Value::as_str).unwrap_or("group"),
                    "logic": logic,
                    "passed": passed,
                    "childCount": children.len(),
                }));
            }
            Ok(passed)
        }
    }
}

/// Evaluate a v2 rule tree node against a map of variable values.
/// Returns `Ok(true)` when the node passes, `Err` when the tree nests past
/// [`MAX_RULE_TREE_DEPTH`].
pub fn evaluate_node(node: &Value, variables: &Value) -> Result<bool, EvalError> {
    eval_node(node, variables, &root_logic(node), 0, None)
}

fn tree_outcome(tree: &Value, passed: bool) -> String {
    let key = if passed { "onPass" } else { "onFail" };
    tree.get(key)
        .and_then(Value::as_str)
        .unwrap_or(if passed { "approve" } else { "reject" })
        .to_string()
}

/// Evaluate a full rule tree; returns the resolved outcome string.
pub fn evaluate_tree(tree: &Value, variables: &Value) -> Result<String, EvalError> {
    let passed = eval_node(tree, variables, &root_logic(tree), 0, None)?;
    Ok(tree_outcome(tree, passed))
}

/// Evaluate a full rule tree and collect the per-condition / per-group trace.
pub fn evaluate_tree_traced(tree: &Value, variables: &Value) -> Result<RuleTrace, EvalError> {
    let logic = root_logic(tree);
    let mut sink = TraceSink::default();
    let passed = eval_node(tree, variables, &logic, 0, Some(&mut sink))?;
    Ok(RuleTrace {
        conditions: sink.conditions,
        group_results: sink.groups,
        passed,
        outcome: tree_outcome(tree, passed),
        logic,
    })
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
    /// Compiled variables in declared order (register-machine instructions). Empty when the
    /// bundle only carries pre-computed variables and the caller passes them directly.
    compiled_variables: Vec<Value>,
}

impl CompiledBundle {
    pub fn from_json(bundle_json: &str) -> Result<Self, serde_json::Error> {
        Ok(Self::from_value(serde_json::from_str(bundle_json)?))
    }

    /// Compile an already-parsed bundle. `serde_json`'s parser refuses input nested deeper than
    /// 128, so this is the only way to build a bundle whose rule tree reaches the engine's own
    /// 200-node depth bound — and the way a caller that already holds a `Value` (PyO3, a native
    /// consumer) avoids a re-serialize/re-parse round trip.
    pub fn from_value(bundle: Value) -> Self {
        let policy = bundle.get("policy").cloned().unwrap_or(Value::Null);
        let mut rules = HashMap::new();
        if let Some(map) = bundle.get("rules").and_then(Value::as_object) {
            for (key, value) in map {
                rules.insert(key.clone(), value.clone());
            }
        }
        let compiled_variables = bundle
            .get("variables")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Self {
            policy,
            rules,
            compiled_variables,
        }
    }

    /// Compute the bundle's compiled variables from a raw payload, then decide — the fully
    /// self-contained hot path (payload in, outcome out) matching the SDK evaluators. When the
    /// bundle has no compiled variables, the payload is used directly as the variable map.
    pub fn decide_from_payload(&self, payload: &Value) -> Result<String, EvalError> {
        self.run(payload, None)
    }

    /// [`Self::decide_from_payload`] plus a per-rule trace (`conditions[]` / `groupResults[]`).
    pub fn decide_from_payload_traced(
        &self,
        payload: &Value,
    ) -> Result<(String, Vec<Value>), EvalError> {
        let mut traces = Vec::new();
        let outcome = self.run(payload, Some(&mut traces))?;
        Ok((outcome, traces))
    }

    /// Run the policy's rule/outcome steps over a variable map; returns the outcome.
    pub fn decide(&self, variables: &Value) -> Result<String, EvalError> {
        self.steps(variables, None)
    }

    /// [`Self::decide`] plus a per-rule trace, one entry per evaluated rule step.
    pub fn decide_traced(&self, variables: &Value) -> Result<(String, Vec<Value>), EvalError> {
        let mut traces = Vec::new();
        let outcome = self.steps(variables, Some(&mut traces))?;
        Ok((outcome, traces))
    }

    /// Compute variables (when the bundle carries them) and then run the steps.
    fn run(&self, payload: &Value, traces: Option<&mut Vec<Value>>) -> Result<String, EvalError> {
        if self.compiled_variables.is_empty() {
            return self.steps(payload, traces);
        }
        let variables = variables::compute_variables(&self.compiled_variables, payload);
        self.steps(&variables, traces)
    }

    fn steps(
        &self,
        variables: &Value,
        mut traces: Option<&mut Vec<Value>>,
    ) -> Result<String, EvalError> {
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
                                let oc = match traces.as_mut() {
                                    Some(sink) => {
                                        let trace = evaluate_tree_traced(tree, variables)?;
                                        let mut entry = trace.to_json();
                                        if let Some(obj) = entry.as_object_mut() {
                                            obj.insert("ruleId".into(), json!(ref_id));
                                        }
                                        sink.push(entry);
                                        trace.outcome
                                    }
                                    None => evaluate_tree(tree, variables)?,
                                };
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
        Ok(if outcome == "pending" {
            String::from("review")
        } else {
            outcome
        })
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
        // `cast`, not `downcast`: pyo3 0.24 renamed it on Bound<PyAny>. Dependabot's bump
        // (#144) changed only Cargo.toml and Cargo.lock, so the crate did not compile with the
        // `python` feature at all -- and nothing noticed, because the feature is off by default
        // and `cargo test --no-default-features` never builds this path.
        if let Ok(dict) = obj.cast::<PyDict>() {
            let mut map = serde_json::Map::new();
            for (key, value) in dict.iter() {
                let k = key
                    .extract::<String>()
                    .unwrap_or_else(|_| key.str().map(|s| s.to_string()).unwrap_or_default());
                map.insert(k, py_to_value(&value));
            }
            return Value::Object(map);
        }
        if let Ok(list) = obj.cast::<PyList>() {
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
        ///
        /// A tree nested past `MAX_RULE_TREE_DEPTH` raises `ValueError`, exactly as
        /// `app/logic.py::evaluate_rule_tree` does — it must never abort the interpreter.
        fn decide(&self, payload: &Bound<'_, PyAny>) -> PyResult<String> {
            let vars = py_to_value(payload);
            self.inner.decide(&vars).map_err(eval_err)
        }

        /// [`Bundle::decide`] plus the per-rule trace, returned as a JSON string.
        fn decide_traced(&self, payload: &Bound<'_, PyAny>) -> PyResult<(String, String)> {
            let vars = py_to_value(payload);
            let (outcome, traces) = self.inner.decide_traced(&vars).map_err(eval_err)?;
            Ok((outcome, Value::Array(traces).to_string()))
        }
    }

    /// Depth (and any future eval) failures surface as Python `ValueError`, the same exception
    /// type and message the pure-Python engine raises.
    fn eval_err(e: crate::EvalError) -> PyErr {
        pyo3::exceptions::PyValueError::new_err(e.to_string())
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

    fn parse_pair(tree_json: &str, variables_json: &str) -> PyResult<(Value, Value)> {
        let tree: Value = serde_json::from_str(tree_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let vars: Value = serde_json::from_str(variables_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        Ok((tree, vars))
    }

    #[pyfunction]
    #[pyo3(name = "evaluate_tree")]
    fn py_evaluate_tree(tree_json: &str, variables_json: &str) -> PyResult<String> {
        let (tree, vars) = parse_pair(tree_json, variables_json)?;
        crate::evaluate_tree(&tree, &vars).map_err(eval_err)
    }

    /// Evaluate a tree and return the trace (`conditions` / `groupResults` / `passed` /
    /// `outcome` / `logic`) as a JSON string — the shape `app/logic.py::evaluate_rule_tree`
    /// returns, so the Python arm can diff it directly.
    #[pyfunction]
    #[pyo3(name = "evaluate_tree_traced")]
    fn py_evaluate_tree_traced(tree_json: &str, variables_json: &str) -> PyResult<String> {
        let (tree, vars) = parse_pair(tree_json, variables_json)?;
        let trace = crate::evaluate_tree_traced(&tree, &vars).map_err(eval_err)?;
        Ok(trace.to_json().to_string())
    }

    #[pymodule]
    fn rulemind_core_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(py_compare, m)?)?;
        m.add_function(wrap_pyfunction!(py_evaluate_tree, m)?)?;
        m.add_function(wrap_pyfunction!(py_evaluate_tree_traced, m)?)?;
        m.add("MAX_RULE_TREE_DEPTH", crate::MAX_RULE_TREE_DEPTH)?;
        m.add("REGEX_INPUT_MAX", crate::REGEX_INPUT_MAX)?;
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

    /// Evaluate a v2 rule tree; returns the outcome string. A tree nested past
    /// `MAX_RULE_TREE_DEPTH` throws (a JS `Error`), matching the other engines — never a
    /// stack overflow, which in WASM traps and poisons the module instance.
    #[wasm_bindgen(js_name = evaluateTree)]
    pub fn evaluate_tree_wasm(tree_json: &str, variables_json: &str) -> Result<String, JsValue> {
        let tree: Value = serde_json::from_str(tree_json).unwrap_or(Value::Null);
        let vars: Value = serde_json::from_str(variables_json).unwrap_or(Value::Null);
        crate::evaluate_tree(&tree, &vars).map_err(|e| JsValue::from_str(&e.to_string()))
    }

    /// Evaluate a v2 rule tree and return the trace JSON (`conditions` / `groupResults` /
    /// `passed` / `outcome` / `logic`).
    #[wasm_bindgen(js_name = evaluateTreeTraced)]
    pub fn evaluate_tree_traced_wasm(
        tree_json: &str,
        variables_json: &str,
    ) -> Result<String, JsValue> {
        let tree: Value = serde_json::from_str(tree_json).unwrap_or(Value::Null);
        let vars: Value = serde_json::from_str(variables_json).unwrap_or(Value::Null);
        crate::evaluate_tree_traced(&tree, &vars)
            .map(|trace| trace.to_json().to_string())
            .map_err(|e| JsValue::from_str(&e.to_string()))
    }

    /// Decide a compiled bundle against a payload; returns the outcome string.
    #[wasm_bindgen(js_name = decide)]
    pub fn decide_wasm(bundle_json: &str, payload_json: &str) -> Result<String, JsValue> {
        let bundle = crate::CompiledBundle::from_json(bundle_json)
            .map_err(|e| JsValue::from_str(&e.to_string()))?;
        let vars: Value = serde_json::from_str(payload_json).unwrap_or(Value::Null);
        bundle
            .decide(&vars)
            .map_err(|e| JsValue::from_str(&e.to_string()))
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
        assert_eq!(evaluate_tree(&tree, &json!({"score": 750})).unwrap(), "approve");
        assert_eq!(evaluate_tree(&tree, &json!({"score": 650})).unwrap(), "reject");
    }

    /// Nest a tree past MAX_RULE_TREE_DEPTH and require an `Err` — the Rust arm of
    /// `apps/python-executor/tests/test_large_policy_conformance.py`
    /// `test_depth_guard_raises_on_pathological_nesting`. Before the guard this recursed into a
    /// stack overflow, which Rust turns into a SIGSEGV/abort: the test binary (and, in the decide
    /// service, the whole server) died rather than returning an error.
    ///
    /// The tree is built as a `Value` rather than parsed from a string on purpose: serde_json's
    /// parser caps nesting at 128, so a parsed tree can never reach the engine's own bound.
    #[test]
    fn depth_guard_errors_instead_of_overflowing_the_stack() {
        let mut node = json!({"type": "condition", "variable": "x", "operator": ">=", "value": 1});
        for _ in 0..(MAX_RULE_TREE_DEPTH + 5) {
            node = json!({"type": "group", "logic": "AND", "children": [node]});
        }
        assert_eq!(evaluate_node(&node, &json!({"x": 5})), Err(EvalError::DepthExceeded));
        assert_eq!(evaluate_tree(&node, &json!({"x": 5})), Err(EvalError::DepthExceeded));
        assert_eq!(
            evaluate_tree_traced(&node, &json!({"x": 5})).unwrap_err(),
            EvalError::DepthExceeded
        );
        assert_eq!(
            EvalError::DepthExceeded.to_string(),
            "Rule tree nesting exceeds the maximum depth (200)"
        );
    }

    /// The guard must not fire one node too early: a tree exactly at the limit still evaluates.
    #[test]
    fn depth_guard_admits_a_tree_exactly_at_the_limit() {
        let mut node = json!({"type": "condition", "variable": "x", "operator": ">=", "value": 1});
        // The condition sits at depth MAX_RULE_TREE_DEPTH after this many wrappers.
        for _ in 0..MAX_RULE_TREE_DEPTH {
            node = json!({"type": "group", "logic": "AND", "children": [node]});
        }
        assert_eq!(evaluate_node(&node, &json!({"x": 5})), Ok(true));
        // One more wrapper pushes the leaf past the bound.
        let deeper = json!({"type": "group", "logic": "AND", "children": [node]});
        assert_eq!(evaluate_node(&deeper, &json!({"x": 5})), Err(EvalError::DepthExceeded));
    }

    /// A `not` node nests too, so the guard has to count it — otherwise an all-NOT chain still
    /// overflows.
    #[test]
    fn depth_guard_counts_not_nodes() {
        let mut node = json!({"type": "condition", "variable": "x", "operator": ">=", "value": 1});
        for _ in 0..(MAX_RULE_TREE_DEPTH + 5) {
            node = json!({"type": "not", "child": node});
        }
        assert_eq!(evaluate_node(&node, &json!({"x": 5})), Err(EvalError::DepthExceeded));
    }

    /// `fieldType` is case-insensitive, as it is in Python, Kotlin and Dart. A field declared
    /// "Boolean"/"Date" must take the SAME typed path as "boolean"/"date" — otherwise the same
    /// policy decides differently here than on three of the other four engines.
    #[test]
    fn field_type_is_matched_case_insensitively() {
        for spelling in ["boolean", "Boolean", "BOOLEAN"] {
            // Untyped, "yes" != true; declared boolean, both coerce to true.
            assert!(
                compare(&json!("yes"), "==", &json!(true), &Value::Null, Some(spelling)),
                "fieldType {spelling:?} must take the boolean path"
            );
        }
        assert!(!compare(&json!("yes"), "==", &json!(true), &Value::Null, None));

        for spelling in ["date", "Date", "DATE"] {
            // Untyped, these two strings are unequal; declared date, they are the same instant.
            assert!(
                compare(
                    &json!("2026-01-01"),
                    "==",
                    &json!("2026-1-1"),
                    &Value::Null,
                    Some(spelling)
                ),
                "fieldType {spelling:?} must take the date path"
            );
            // ...and a non-ISO value under a date field is false on every operator.
            assert!(!compare(
                &json!("not-a-date"),
                "==",
                &json!("2026-01-01"),
                &Value::Null,
                Some(spelling)
            ));
        }
        assert!(!compare(&json!("2026-01-01"), "==", &json!("2026-1-1"), &Value::Null, None));
    }

    /// Regex input cap — parity with `app/logic.py::_safe_regex_search`. The pattern matches at
    /// position 0 in every case here, so the ONLY thing deciding the result is the length bound:
    /// at the cap it matches, one character over it fails closed.
    #[test]
    fn regex_input_cap_matches_python() {
        let pattern = json!("^a+$");
        let at_cap = Value::String("a".repeat(REGEX_INPUT_MAX));
        let over_cap = Value::String("a".repeat(REGEX_INPUT_MAX + 1));
        assert!(compare(&at_cap, "regex", &pattern, &Value::Null, None), "4096 chars is allowed");
        assert!(
            !compare(&over_cap, "regex", &pattern, &Value::Null, None),
            "4097 chars must fail closed even though the pattern matches"
        );
        // Measured in CODE POINTS, as Python's len() is — not bytes. 4096 three-byte chars is
        // 12288 bytes but still exactly at the cap.
        let multibyte = Value::String("あ".repeat(REGEX_INPUT_MAX));
        assert!(compare(&multibyte, "regex", &json!("^あ+$"), &Value::Null, None));
        let multibyte_over = Value::String("あ".repeat(REGEX_INPUT_MAX + 1));
        assert!(!compare(&multibyte_over, "regex", &json!("^あ+$"), &Value::Null, None));
    }

    /// The trace shape the other four engines emit: one `conditions[]` entry per condition in
    /// evaluation order, one `groupResults[]` entry per group/NOT appended post-order.
    #[test]
    fn trace_records_every_condition_and_group() {
        let tree = json!({
            "type": "group", "id": "root", "logic": "AND", "onPass": "approve", "onFail": "reject",
            "children": [
                {"type": "condition", "variable": "score", "operator": ">=", "value": 700},
                {"type": "group", "id": "inner", "logic": "OR", "children": [
                    {"type": "condition", "variable": "state", "operator": "==", "value": "KA"},
                    {"type": "condition", "variable": "state", "operator": "==", "value": "MH"}
                ]},
                {"type": "not", "id": "blocked", "child":
                    {"type": "condition", "variable": "fraud", "operator": "==", "value": true, "fieldType": "boolean"}}
            ]
        });
        let trace = evaluate_tree_traced(&tree, &json!({"score": 750, "state": "MH", "fraud": false}))
            .unwrap();
        assert_eq!(trace.conditions.len(), 4);
        // score>=700 true, state=="KA" false, state=="MH" true, fraud==true false.
        assert_eq!(trace.passed_conditions(), 2);
        assert!(trace.passed);
        assert_eq!(trace.outcome, "approve");
        assert_eq!(trace.logic, "AND");
        // `group` on a condition is the ENCLOSING logic, as app/logic.py records it.
        assert_eq!(trace.conditions[0]["group"], json!("AND"));
        assert_eq!(trace.conditions[1]["group"], json!("OR"));
        assert_eq!(trace.conditions[3]["group"], json!("NOT"));
        assert_eq!(trace.conditions[0]["variable_id"], json!("score"));
        assert_eq!(trace.conditions[0]["variable_name"], json!("score"));
        assert_eq!(trace.conditions[0]["threshold"], json!(700));
        assert_eq!(trace.conditions[0]["value"], json!(750));
        assert_eq!(trace.conditions[0]["passed"], json!(true));
        assert!(trace.conditions[0].get("source_id").is_some());
        // Post-order: inner group, NOT, then the root.
        let ids: Vec<&str> = trace
            .group_results
            .iter()
            .map(|g| g["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, vec!["inner", "blocked", "root"]);
        assert_eq!(trace.group_results[0]["logic"], json!("OR"));
        assert_eq!(trace.group_results[0]["childCount"], json!(2));
        assert_eq!(trace.group_results[1]["logic"], json!("NOT"));
        assert_eq!(trace.group_results[2]["childCount"], json!(3));
        // Serialized form uses the cross-engine key names.
        let as_json = trace.to_json();
        assert!(as_json.get("groupResults").is_some());
        assert!(as_json.get("conditions").is_some());
    }

    /// A false first child must NOT stop the rest of the tree from being traced — Python builds
    /// the full list of child results, so a short-circuiting `.all()` would under-count.
    #[test]
    fn trace_does_not_short_circuit() {
        let tree = json!({
            "type": "group", "logic": "AND", "children": [
                {"type": "condition", "variable": "score", "operator": ">=", "value": 900},
                {"type": "condition", "variable": "score", "operator": ">=", "value": 100},
                {"type": "condition", "variable": "score", "operator": ">=", "value": 200}
            ]
        });
        let trace = evaluate_tree_traced(&tree, &json!({"score": 750})).unwrap();
        assert!(!trace.passed);
        assert_eq!(trace.conditions.len(), 3, "every child is evaluated, not just the first");
        assert_eq!(trace.passed_conditions(), 2);
    }

    /// The same non-short-circuiting rule applies to the *untraced* path: a depth violation
    /// hiding behind an already-false sibling still has to surface.
    #[test]
    fn depth_error_is_not_swallowed_by_a_false_sibling() {
        let mut deep = json!({"type": "condition", "variable": "x", "operator": ">=", "value": 1});
        for _ in 0..(MAX_RULE_TREE_DEPTH + 5) {
            deep = json!({"type": "group", "logic": "AND", "children": [deep]});
        }
        let tree = json!({
            "type": "group", "logic": "AND", "children": [
                {"type": "condition", "variable": "x", "operator": ">=", "value": 9999},
                deep
            ]
        });
        assert_eq!(evaluate_tree(&tree, &json!({"x": 5})), Err(EvalError::DepthExceeded));
    }

    /// A depth violation inside a bundle's rule must come back as an error from `decide`, not
    /// as an outcome and not as an abort.
    #[test]
    fn compiled_bundle_propagates_the_depth_error() {
        let mut node = json!({"type": "condition", "variable": "x", "operator": ">=", "value": 1});
        for _ in 0..(MAX_RULE_TREE_DEPTH + 5) {
            node = json!({"type": "group", "logic": "AND", "children": [node]});
        }
        let bundle = CompiledBundle::from_value(json!({
            "rules": { "deep": { "id": "deep", "tree": node } },
            "policy": { "id": "p", "steps": [ { "type": "rule", "ref_id": "deep" } ] }
        }));
        assert_eq!(bundle.decide(&json!({"x": 5})), Err(EvalError::DepthExceeded));
        assert_eq!(
            bundle.decide_from_payload(&json!({"x": 5})),
            Err(EvalError::DepthExceeded)
        );
    }
}
