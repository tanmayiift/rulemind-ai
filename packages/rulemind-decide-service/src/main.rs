//! Standalone Rust decision service — the dedicated high-throughput `/decide` hot path.
//!
//! It serves a compiled RuleMind bundle (the Python-free edge bundle) over HTTP, computing the
//! policy's variables + evaluating its rules entirely in native Rust — no Python, no DB on the
//! request path. The FastAPI control plane keeps authoring/admin and the complex policy steps
//! (connectors, scorecards, human review); this tier absorbs the pure-compute rule decisions at
//! very high QPS and sub-millisecond latency. It is use-case agnostic: it evaluates whatever
//! decisioning bundle it is given.
//!
//! Config:
//!   RULEMIND_BUNDLE_PATH   path to the compiled bundle JSON (required)
//!   RULEMIND_DECIDE_ADDR   listen address (default 0.0.0.0:8090)
//!
//! Endpoints:
//!   GET  /healthz          liveness
//!   GET  /readyz           readiness (bundle loaded)
//!   POST /decide           { "payload": {...} }  ->  { "outcome": "...", "latency_us": N }
//!                          { "variables": {...} } is also accepted (pre-computed inputs)
//!                          { "trace": true } adds the per-rule condition/group trace
//!
//! A rule tree nested past the engine's depth bound is answered with 422 and a JSON error —
//! it must never take the process down, since the tree is author-supplied.

use std::sync::Arc;
use std::time::Instant;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use rulemind_core_rs::CompiledBundle;
use serde::Deserialize;
use serde_json::{json, Value};

struct AppState {
    bundle: CompiledBundle,
    bundle_version: String,
}

#[derive(Deserialize)]
struct DecideRequest {
    #[serde(default)]
    payload: Option<Value>,
    #[serde(default)]
    variables: Option<Value>,
    /// Return the per-rule `conditions[]` / `groupResults[]` trace alongside the outcome.
    #[serde(default)]
    trace: bool,
}

async fn healthz() -> impl IntoResponse {
    (StatusCode::OK, Json(json!({ "status": "ok" })))
}

async fn readyz(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(json!({ "status": "ready", "bundleVersion": state.bundle_version })),
    )
}

async fn decide(
    State(state): State<Arc<AppState>>,
    Json(req): Json<DecideRequest>,
) -> impl IntoResponse {
    let started = Instant::now();
    // Pre-computed variables take precedence; otherwise compute them from the raw payload.
    let bundle = &state.bundle;
    let evaluated = match (req.variables, req.trace) {
        (Some(vars), false) => bundle.decide(&vars).map(|o| (o, None)),
        (Some(vars), true) => bundle.decide_traced(&vars).map(|(o, t)| (o, Some(t))),
        (None, false) => bundle
            .decide_from_payload(&req.payload.unwrap_or(Value::Null))
            .map(|o| (o, None)),
        (None, true) => bundle
            .decide_from_payload_traced(&req.payload.unwrap_or(Value::Null))
            .map(|(o, t)| (o, Some(t))),
    };
    let latency_us = started.elapsed().as_micros();

    // An evaluation error is a bad *request/bundle*, not a dead server. Before the depth guard
    // this case blew the native stack and aborted the whole process, taking every in-flight
    // request with it; now one request gets a 422 and the service keeps serving.
    let (outcome, trace) = match evaluated {
        Ok(pair) => pair,
        Err(e) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": e.to_string(),
                    "latency_us": latency_us,
                    "bundleVersion": state.bundle_version,
                })),
            )
        }
    };
    let mut body = json!({
        "outcome": outcome,
        "latency_us": latency_us,
        "bundleVersion": state.bundle_version,
    });
    if let (Some(trace), Some(obj)) = (trace, body.as_object_mut()) {
        obj.insert("trace".into(), Value::Array(trace));
    }
    (StatusCode::OK, Json(body))
}

fn build_router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/decide", post(decide))
        .with_state(state)
}

fn load_bundle() -> Result<(CompiledBundle, String), String> {
    let path = std::env::var("RULEMIND_BUNDLE_PATH")
        .map_err(|_| "RULEMIND_BUNDLE_PATH is required (path to the compiled bundle JSON)".to_string())?;
    let text = std::fs::read_to_string(&path).map_err(|e| format!("read {}: {}", path, e))?;
    let version = serde_json::from_str::<Value>(&text)
        .ok()
        .and_then(|v| {
            v.get("blockVersion")
                .or_else(|| v.get("bundleVersion"))
                .map(|x| x.as_str().map(String::from).unwrap_or_else(|| x.to_string()))
        })
        .unwrap_or_else(|| "unknown".to_string());
    let bundle = CompiledBundle::from_json(&text).map_err(|e| format!("parse bundle: {}", e))?;
    Ok((bundle, version))
}

#[tokio::main]
async fn main() {
    let (bundle, bundle_version) = match load_bundle() {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("rulemind-decide-service: {}", e);
            std::process::exit(1);
        }
    };
    let state = Arc::new(AppState {
        bundle,
        bundle_version,
    });

    let app = build_router(state.clone());

    let addr = std::env::var("RULEMIND_DECIDE_ADDR").unwrap_or_else(|_| "0.0.0.0:8090".to_string());
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("rulemind-decide-service: bind {}: {}", addr, e);
            std::process::exit(1);
        }
    };
    eprintln!(
        "rulemind-decide-service: serving bundle {} on {}",
        state.bundle_version, addr
    );
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .unwrap();
}

// ---------------------------------------------------------------------------
// Service tests.
//
// These drive the real `Router` end to end with `tower::ServiceExt::oneshot`: request in,
// status + JSON body out, through the same extractor and handler production traffic hits.
// The point is to test the SERVICE, not the library it wraps — a decision that is right in
// `rulemind_core_rs` but 500s at the HTTP boundary is still a broken service.
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use rulemind_core_rs::MAX_RULE_TREE_DEPTH;
    use tower::ServiceExt;

    fn service(bundle: Value) -> Router {
        build_router(Arc::new(AppState {
            // from_value, not from_json: serde_json's parser refuses nesting deeper than 128,
            // so a *parsed* bundle can never reach the engine's own 200-deep bound. The depth
            // test below needs a tree that does.
            bundle: CompiledBundle::from_value(bundle),
            bundle_version: "test-1".to_string(),
        }))
    }

    async fn post_decide(app: Router, body: Value) -> (StatusCode, Value) {
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/decide")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .expect("the service must answer, not panic");
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let json: Value = serde_json::from_slice(&bytes)
            .unwrap_or_else(|e| panic!("non-JSON body {:?}: {}", String::from_utf8_lossy(&bytes), e));
        (status, json)
    }

    /// One condition wrapped in a minimal single-rule policy: onPass -> approve, onFail -> reject.
    fn single_condition_bundle(condition: Value) -> Value {
        json!({
            "rules": { "r": { "id": "r", "tree": {
                "type": "group", "id": "root", "logic": "AND", "children": [condition],
                "onPass": "approve", "onFail": "reject"
            } } },
            "policy": { "id": "p", "steps": [ { "type": "rule", "ref_id": "r" } ] }
        })
    }

    fn shared_spec(name: &str) -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("shared")
            .join(name);
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {:?}: {}", path, e));
        serde_json::from_str(&text).unwrap()
    }

    #[tokio::test]
    async fn healthz_and_readyz_report_the_loaded_bundle() {
        let app = service(single_condition_bundle(
            json!({"type": "condition", "variable": "v", "operator": ">=", "value": 1}),
        ));
        let health = app
            .clone()
            .oneshot(Request::builder().uri("/healthz").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(health.status(), StatusCode::OK);

        let ready = app
            .oneshot(Request::builder().uri("/readyz").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(ready.status(), StatusCode::OK);
        let bytes = ready.into_body().collect().await.unwrap().to_bytes();
        let body: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(body["status"], "ready");
        assert_eq!(body["bundleVersion"], "test-1");
    }

    /// The shared 90-case operator contract, run THROUGH the service: each case becomes a
    /// one-condition policy and a POST, and the HTTP outcome must match the case's expectation.
    /// This is the cross-engine contract arriving over the wire, not a library call.
    #[tokio::test]
    async fn operators_spec_conforms_over_http() {
        let spec = shared_spec("operators.spec.json");
        let cases = spec["cases"].as_array().expect("cases");
        assert!(cases.len() >= 90, "expected the full operator matrix, got {}", cases.len());
        for case in cases {
            let name = case["name"].as_str().unwrap_or("?");
            let mut condition = json!({
                "type": "condition",
                "variable": "v",
                "operator": case["operator"],
                "value": case.get("value").cloned().unwrap_or(Value::Null),
            });
            let obj = condition.as_object_mut().unwrap();
            if let Some(v2) = case.get("value2") {
                obj.insert("value2".into(), v2.clone());
            }
            if let Some(ft) = case.get("fieldType") {
                obj.insert("fieldType".into(), ft.clone());
            }
            let app = service(single_condition_bundle(condition));
            let (status, body) =
                post_decide(app, json!({ "variables": { "v": case["actual"] } })).await;
            assert_eq!(status, StatusCode::OK, "case {name} status");
            let want = if case["expected"].as_bool().unwrap() { "approve" } else { "reject" };
            assert_eq!(body["outcome"], want, "case {name} outcome");
        }
    }

    #[tokio::test]
    async fn decide_accepts_a_raw_payload_and_computes_variables() {
        let app = service(json!({
            "variables": [ { "id": "ratio", "source_id": "custom", "instructions": [
                { "op": "get", "target": "a", "path": "amount" },
                { "op": "get", "target": "b", "path": "income" },
                { "op": "divide", "target": "r", "left": "a", "right": "b" },
                { "op": "return", "source": "r" } ] } ],
            "rules": { "r": { "id": "r", "tree": {
                "type": "condition", "variable": "ratio", "operator": "<", "value": 0.4,
                "onPass": "approve", "onFail": "reject" } } },
            "policy": { "id": "p", "steps": [ { "type": "rule", "ref_id": "r" } ] }
        }));
        let (status, body) =
            post_decide(app, json!({ "payload": { "amount": 3000, "income": 10000 } })).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["outcome"], "approve");
        assert!(body["latency_us"].is_number());
        assert_eq!(body["bundleVersion"], "test-1");
    }

    // -- fix 1: the depth guard, at the HTTP boundary ------------------------

    fn deep_tree(depth: usize) -> Value {
        let mut node = json!({"type": "condition", "variable": "v", "operator": ">=", "value": 1});
        for _ in 0..depth {
            node = json!({"type": "group", "logic": "AND", "children": [node]});
        }
        json!({
            "rules": { "r": { "id": "r", "tree": node } },
            "policy": { "id": "p", "steps": [ { "type": "rule", "ref_id": "r" } ] }
        })
    }

    /// A rule tree nested past the bound must come back as a 4xx with a JSON error. Before the
    /// guard this recursed until the native stack blew and the process ABORTED — the whole
    /// server, not the request. `oneshot(..).await` returning at all is half the assertion.
    #[tokio::test]
    async fn overdeep_rule_tree_is_a_422_not_a_dead_process() {
        let app = service(deep_tree(MAX_RULE_TREE_DEPTH + 5));
        let (status, body) = post_decide(app, json!({ "variables": { "v": 5 } })).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(
            body["error"],
            "Rule tree nesting exceeds the maximum depth (200)"
        );
        assert!(body.get("outcome").is_none(), "an error response carries no outcome");
    }

    /// ...and the service keeps serving afterwards: a second request on the same process still
    /// gets a normal answer. This is what "not a crash" actually means.
    #[tokio::test]
    async fn service_survives_an_overdeep_tree_and_keeps_serving() {
        let deep = service(deep_tree(MAX_RULE_TREE_DEPTH + 5));
        let (status, _) = post_decide(deep, json!({ "variables": { "v": 5 } })).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);

        let ok = service(single_condition_bundle(
            json!({"type": "condition", "variable": "v", "operator": ">=", "value": 1}),
        ));
        let (status, body) = post_decide(ok, json!({ "variables": { "v": 5 } })).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["outcome"], "approve");
    }

    /// A tree just inside the bound still decides normally — the guard must not cost legal depth.
    #[tokio::test]
    async fn tree_at_the_depth_limit_still_decides() {
        let app = service(deep_tree(MAX_RULE_TREE_DEPTH));
        let (status, body) = post_decide(app, json!({ "variables": { "v": 5 } })).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["outcome"], "approve");
    }

    // -- fix 2: the per-condition trace, at the HTTP boundary ----------------

    #[tokio::test]
    async fn trace_is_returned_only_when_requested() {
        let bundle = json!({
            "rules": { "r": { "id": "r", "tree": {
                "type": "group", "id": "root", "logic": "AND", "onPass": "approve", "onFail": "reject",
                "children": [
                    // The FAILING condition is first on purpose: a short-circuiting AND would
                    // stop here and report one condition instead of three.
                    {"type": "condition", "variable": "score", "operator": ">=", "value": 900},
                    {"type": "condition", "variable": "score", "operator": ">=", "value": 700},
                    {"type": "condition", "variable": "state", "operator": "in", "value": ["KA", "MH"]}
                ] } } },
            "policy": { "id": "p", "steps": [ { "type": "rule", "ref_id": "r" } ] }
        });
        let vars = json!({ "score": 750, "state": "KA" });

        let (status, plain) =
            post_decide(service(bundle.clone()), json!({ "variables": vars })).await;
        assert_eq!(status, StatusCode::OK);
        assert!(plain.get("trace").is_none(), "no trace unless asked for");

        let (status, body) =
            post_decide(service(bundle), json!({ "variables": vars, "trace": true })).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["outcome"], "reject");
        let rules = body["trace"].as_array().expect("trace array");
        assert_eq!(rules.len(), 1);
        let rule = &rules[0];
        assert_eq!(rule["ruleId"], "r");
        assert_eq!(rule["logic"], "AND");
        assert_eq!(rule["passed"], false);
        // Every condition is reported, including the ones after the first failure — this is the
        // passed-condition COUNT the shared corpus asserts on.
        let conditions = rule["conditions"].as_array().unwrap();
        assert_eq!(conditions.len(), 3);
        let passed = conditions.iter().filter(|c| c["passed"] == true).count();
        assert_eq!(passed, 2);
        // The cross-engine field names (app/logic.py / RuleEvaluator.kt).
        for field in ["variable_id", "variable_name", "operator", "threshold", "value", "passed", "group"] {
            assert!(conditions[0].get(field).is_some(), "condition is missing {field}");
        }
        let groups = rule["groupResults"].as_array().unwrap();
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0]["id"], "root");
        assert_eq!(groups[0]["childCount"], 3);
    }

    /// The trace path is depth-guarded too — it must not become a second way to abort.
    #[tokio::test]
    async fn traced_decide_also_rejects_an_overdeep_tree() {
        let app = service(deep_tree(MAX_RULE_TREE_DEPTH + 5));
        let (status, body) =
            post_decide(app, json!({ "variables": { "v": 5 }, "trace": true })).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert!(body["error"].as_str().unwrap().contains("maximum depth"));
    }

    // -- fix 3: the regex input cap, at the HTTP boundary --------------------

    /// A regex condition whose pattern WOULD match: at the 4096-character cap it matches, one
    /// character over it fails closed — the same answer app/logic.py gives, so a giant field
    /// can't decide differently on this engine (nor burn CPU proportional to its size).
    #[tokio::test]
    async fn regex_input_over_the_cap_fails_closed_over_http() {
        let bundle = single_condition_bundle(
            json!({"type": "condition", "variable": "v", "operator": "regex", "value": "^a+$"}),
        );
        let cap = 4096;
        for (len, want) in [(cap, "approve"), (cap + 1, "reject")] {
            let (status, body) = post_decide(
                service(bundle.clone()),
                json!({ "variables": { "v": "a".repeat(len) } }),
            )
            .await;
            assert_eq!(status, StatusCode::OK, "len {len} status");
            assert_eq!(body["outcome"], want, "len {len} outcome");
        }
    }

    /// A megabyte-scale field is answered, not burned on. (axum's default body limit is 2 MiB,
    /// so 1 MiB is the largest field that actually reaches the handler — anything bigger is
    /// rejected by the extractor before the engine ever sees it.)
    #[tokio::test]
    async fn megabyte_regex_input_is_answered_not_burned_on() {
        let bundle = single_condition_bundle(
            json!({"type": "condition", "variable": "v", "operator": "regex", "value": "^a+$"}),
        );
        let started = std::time::Instant::now();
        let (status, body) = post_decide(
            service(bundle),
            json!({ "variables": { "v": "a".repeat(1024 * 1024) } }),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["outcome"], "reject");
        // The cap means the engine never touches the 1 MiB string; the whole round trip is
        // dominated by JSON, not by matching.
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "an over-cap input must be rejected on length, not scanned"
        );
    }
}
