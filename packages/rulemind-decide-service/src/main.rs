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
    let outcome = if let Some(vars) = req.variables {
        state.bundle.decide(&vars)
    } else {
        let payload = req.payload.unwrap_or(Value::Null);
        state.bundle.decide_from_payload(&payload)
    };
    let latency_us = started.elapsed().as_micros();
    (
        StatusCode::OK,
        Json(json!({
            "outcome": outcome,
            "latency_us": latency_us,
            "bundleVersion": state.bundle_version,
        })),
    )
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

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/decide", post(decide))
        .with_state(state.clone());

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
