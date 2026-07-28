"""Stateless decision microservice — the primary Kubernetes deployment artifact.

Loads an immutable, versioned bundle once at startup (from RULEMIND_BUNDLE_PATH)
and serves POST /decide with ZERO database. Horizontally scalable by design: run
N identical replicas behind a Service/Ingress with an HPA on RPS/latency — there
is no shared or sticky state, so pods are disposable and interchangeable.

Run locally:  RULEMIND_BUNDLE_PATH=bundle.json uvicorn app.core.service:app
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import decide

_BUNDLE: Optional[Dict[str, Any]] = None


def load_bundle(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    global _BUNDLE
    path = path or os.getenv("RULEMIND_BUNDLE_PATH")
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            _BUNDLE = json.load(handle)
    return _BUNDLE


def set_bundle(bundle: Optional[Dict[str, Any]]) -> None:
    """Inject a bundle directly (used by tests and in-process embedding)."""
    global _BUNDLE
    _BUNDLE = bundle


app = FastAPI(title="RuleMind Decision Core", version="1.0.0")


class DecideRequest(BaseModel):
    payload: Dict[str, Any]
    policy_id: Optional[str] = None
    subject_id: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None


@app.on_event("startup")
def _startup() -> None:
    load_bundle()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "bundle_loaded": _BUNDLE is not None}


@app.get("/ready")
def ready() -> Dict[str, Any]:
    if _BUNDLE is None:
        raise HTTPException(status_code=503, detail="bundle not loaded")
    return {"status": "ready", "bundle_version": _BUNDLE.get("bundleVersion")}


@app.post("/decide")
def decide_endpoint(request: DecideRequest) -> Dict[str, Any]:
    if _BUNDLE is None:
        raise HTTPException(status_code=503, detail="bundle not loaded")
    context: Dict[str, Any] = {"policy_id": request.policy_id, "subject_id": request.subject_id}
    if request.variables is not None:
        context["variables"] = request.variables
    return decide(_BUNDLE, request.payload, context)
