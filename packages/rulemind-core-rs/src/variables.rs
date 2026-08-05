//! Compiled-variable register machine — the Rust port of the on-device VariableVM
//! (Kotlin `VariableVM.kt` / Dart `variable_vm.dart`). It evaluates the Python-free compiled
//! variable instructions emitted by the compiler, so the standalone decide service can compute
//! a policy's variables from a raw payload with the SAME semantics as the SDK evaluators —
//! use-case agnostic, no domain assumptions.

use serde_json::{Map, Value};

fn numeric(value: &Value) -> f64 {
    match value {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => s.trim().parse::<f64>().unwrap_or(0.0),
        Value::Bool(b) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        _ => 0.0,
    }
}

fn normalize(value: &Value) -> Value {
    match value {
        Value::Number(n) => Value::from(n.as_f64().unwrap_or(0.0)),
        Value::String(s) => Value::String(s.trim().to_string()),
        other => other.clone(),
    }
}

fn as_list(value: &Value) -> Vec<Value> {
    match value {
        Value::Array(a) => a.clone(),
        Value::Null => vec![],
        other => vec![other.clone()],
    }
}

/// Resolve a dotted path with optional `[index]` segments (supports negative indices),
/// tolerating a leading `$.`.
fn resolve_path<'a>(root: &'a Value, raw_path: &str) -> Option<Value> {
    let path = raw_path.strip_prefix("$.").unwrap_or(raw_path);
    if path.is_empty() {
        return Some(root.clone());
    }
    let mut current = root.clone();
    for segment in path.split('.') {
        let (key, index) = parse_segment(segment)?;
        current = match &current {
            Value::Object(map) => map.get(&key).cloned().unwrap_or(Value::Null),
            _ => return None,
        };
        if let Some(idx) = index {
            let list = match &current {
                Value::Array(a) => a,
                _ => return None,
            };
            let resolved = if idx < 0 {
                (list.len() as i64 + idx) as usize
            } else {
                idx as usize
            };
            current = list.get(resolved).cloned().unwrap_or(Value::Null);
        }
    }
    Some(current)
}

fn parse_segment(segment: &str) -> Option<(String, Option<i64>)> {
    if let Some(open) = segment.find('[') {
        let key = segment[..open].to_string();
        let close = segment.find(']')?;
        let idx: i64 = segment[open + 1..close].parse().ok()?;
        Some((key, Some(idx)))
    } else {
        Some((segment.to_string(), None))
    }
}

fn compare(actual: &Value, operator: &str, expected: &Value) -> bool {
    match operator {
        ">" => numeric(actual) > numeric(expected),
        ">=" => numeric(actual) >= numeric(expected),
        "<" => numeric(actual) < numeric(expected),
        "<=" => numeric(actual) <= numeric(expected),
        "!=" => normalize(actual) != normalize(expected),
        "in" => as_list(expected).iter().map(normalize).any(|v| v == normalize(actual)),
        "not_in" => !as_list(expected).iter().map(normalize).any(|v| v == normalize(actual)),
        _ => normalize(actual) == normalize(expected),
    }
}

fn cast_value(value: &Value, ty: &str) -> Value {
    match ty.to_lowercase().as_str() {
        "int" | "integer" => Value::from(numeric(value) as i64),
        "float" | "double" | "number" => Value::from(numeric(value)),
        "bool" | "boolean" => Value::Bool(match value {
            Value::Bool(b) => *b,
            Value::Number(n) => n.as_f64().unwrap_or(0.0) != 0.0,
            Value::String(s) => s.eq_ignore_ascii_case("true") || s == "1",
            Value::Null => false,
            _ => true,
        }),
        "list" => Value::Array(as_list(value)),
        _ => match value {
            Value::Null => Value::Null,
            Value::String(s) => Value::String(s.clone()),
            other => Value::String(other.to_string()),
        },
    }
}

/// Evaluate a single compiled variable's instruction list against a payload + prior variables.
/// Mirrors VariableVM.evaluate. Returns the variable's value (or Null).
pub fn evaluate_variable(variable: &Value, payload: &Value, variables: &Value) -> Value {
    let source_id = variable.get("source_id").and_then(Value::as_str).unwrap_or("");
    let source_payload = payload
        .get(source_id)
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| payload.clone());

    let mut registers: Map<String, Value> = Map::new();
    registers.insert("payload".into(), payload.clone());
    registers.insert("variables".into(), variables.clone());
    registers.insert("context".into(), variables.clone());
    registers.insert("source".into(), source_payload.clone());

    let empty: Vec<Value> = vec![];
    let instructions = variable
        .get("instructions")
        .and_then(Value::as_array)
        .unwrap_or(&empty);

    let get = |ins: &Value, k: &str| ins.get(k).cloned();
    let resolve_register = |name: Option<&str>, regs: &Map<String, Value>| -> Value {
        let name = match name {
            Some(n) if !n.is_empty() => n,
            _ => return Value::Null,
        };
        if let Some(v) = regs.get(name) {
            return v.clone();
        }
        for base in ["source", "payload", "variables"] {
            if let Some(root) = regs.get(base) {
                if let Some(v) = resolve_path(root, name) {
                    if !v.is_null() {
                        return v;
                    }
                }
            }
        }
        Value::Null
    };
    let resolve_operand = |name: Option<&str>, regs: &Map<String, Value>, fallback: Value| -> Value {
        let v = resolve_register(name, regs);
        if v.is_null() {
            fallback
        } else {
            v
        }
    };

    for ins in instructions {
        let op = ins.get("op").and_then(Value::as_str).unwrap_or("").to_lowercase();
        let target = ins.get("target").and_then(Value::as_str).map(str::to_string);
        match op.as_str() {
            "literal" => {
                if let Some(t) = target {
                    registers.insert(t, get(ins, "value").unwrap_or(Value::Null));
                }
            }
            "get" => {
                if let Some(t) = target {
                    let src_name = ins.get("source").and_then(Value::as_str);
                    let root = {
                        let r = resolve_register(src_name, &registers);
                        if r.is_null() {
                            source_payload.clone()
                        } else {
                            r
                        }
                    };
                    let path = ins
                        .get("path")
                        .and_then(Value::as_str)
                        .or_else(|| ins.get("key").and_then(Value::as_str));
                    let mut out = match path {
                        Some(p) => resolve_path(&root, p).unwrap_or(Value::Null),
                        None => root,
                    };
                    if out.is_null() {
                        out = get(ins, "default").or_else(|| get(ins, "defaultValue")).unwrap_or(Value::Null);
                    }
                    registers.insert(t, out);
                }
            }
            "cast" => {
                if let Some(t) = target {
                    let src = resolve_register(ins.get("source").and_then(Value::as_str), &registers);
                    let ty = ins.get("type").and_then(Value::as_str).unwrap_or("string");
                    registers.insert(t, cast_value(&src, ty));
                }
            }
            "add" | "subtract" | "multiply" | "divide" => {
                if let Some(t) = target {
                    let left = numeric(&resolve_operand(ins.get("left").and_then(Value::as_str), &registers, Value::Null));
                    let right_fallback = get(ins, "right")
                        .and_then(|v| if v.is_string() { None } else { Some(v) })
                        .or_else(|| get(ins, "value"))
                        .or_else(|| get(ins, "default"))
                        .unwrap_or(Value::Null);
                    let right = numeric(&resolve_operand(ins.get("right").and_then(Value::as_str), &registers, right_fallback));
                    let result = match op.as_str() {
                        "divide" => {
                            if right == 0.0 {
                                0.0
                            } else {
                                left / right
                            }
                        }
                        "multiply" => left * right,
                        "subtract" => left - right,
                        _ => left + right,
                    };
                    registers.insert(t, Value::from(result));
                }
            }
            "min" | "max" => {
                if let Some(t) = target {
                    let vals = numeric_operands(ins, &registers, &resolve_register);
                    let picked = if op == "min" {
                        vals.iter().cloned().fold(f64::INFINITY, f64::min)
                    } else {
                        vals.iter().cloned().fold(f64::NEG_INFINITY, f64::max)
                    };
                    let picked = if picked.is_finite() { picked } else { 0.0 };
                    registers.insert(t, Value::from(picked));
                }
            }
            "compare" => {
                if let Some(t) = target {
                    let left = resolve_operand(ins.get("left").and_then(Value::as_str), &registers, Value::Null);
                    let opr = ins
                        .get("predicateOperator")
                        .and_then(Value::as_str)
                        .or_else(|| ins.get("type").and_then(Value::as_str))
                        .unwrap_or("==");
                    let right = resolve_operand(ins.get("right").and_then(Value::as_str), &registers, get(ins, "value").unwrap_or(Value::Null));
                    registers.insert(t, Value::Bool(compare(&left, opr, &right)));
                }
            }
            "return" => {
                let name = ins
                    .get("source")
                    .and_then(Value::as_str)
                    .or(target.as_deref());
                return resolve_operand(name, &registers, get(ins, "value").unwrap_or(Value::Null));
            }
            _ => {}
        }
    }
    Value::Null
}

fn numeric_operands<F>(ins: &Value, registers: &Map<String, Value>, resolve: &F) -> Vec<f64>
where
    F: Fn(Option<&str>, &Map<String, Value>) -> Value,
{
    if let Some(args) = ins.get("args").and_then(Value::as_array) {
        if !args.is_empty() {
            return args
                .iter()
                .map(|a| numeric(&resolve(a.as_str(), registers)))
                .collect();
        }
    }
    let base = resolve(ins.get("source").and_then(Value::as_str), registers);
    as_list(&base).iter().map(numeric).collect()
}

/// Compute all of a bundle's compiled variables from a payload, in declared order (later
/// variables can read earlier ones), returning the variable-id -> value map.
pub fn compute_variables(compiled_variables: &[Value], payload: &Value) -> Value {
    let mut variables = Map::new();
    for variable in compiled_variables {
        let id = match variable.get("id").and_then(Value::as_str) {
            Some(id) => id.to_string(),
            None => continue,
        };
        let value = evaluate_variable(variable, payload, &Value::Object(variables.clone()));
        variables.insert(id, value);
    }
    Value::Object(variables)
}
