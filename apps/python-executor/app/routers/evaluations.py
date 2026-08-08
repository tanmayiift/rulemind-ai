"""Model-evaluation + label-based backtesting endpoints.

Fills RuleMind's gap: it could host/serve models and run outcome A/B, but not answer
"is this model good?" against ground truth. These endpoints compute the credit-risk metric
suite (Gini/AUC/KS/PR-AUC/calibration/PSI/decile), plus multi-label (propensity) and uplift
(cross-sell) metrics, a temporal (vintage) backtest, and a promotion-gate verdict.

Only metrics + a dataset summary are persisted — never the raw scored rows.
Mirrors the hosted-model router (app/routers/models.py) for auth/tenant/storage conventions."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from .. import main
from ..logic import slugify
from ..main import EvaluationCreateRequest, ModelEvalRequest

router = APIRouter()


def _new_public_id(name: str) -> str:
    public_id = slugify(name) or "evaluation"
    existing = {e["id"] for e in main.storage.list_evaluations()}
    if public_id in existing:
        public_id = f"{public_id}_{uuid.uuid4().hex[:6]}"
    return public_id


def _persist(name: str, description: str | None, status: str, result: Dict[str, Any], model_id: str | None) -> Dict[str, Any]:
    return main.storage.create_evaluation(
        {
            "id": _new_public_id(name),
            "name": name,
            "description": description,
            "model_id": model_id,
            "task": result["task"],
            "dataset_summary": result["dataset_summary"],
            "metrics": result["metrics"],
            "segments": result.get("segments") or {},
            "temporal": result.get("temporal") or {},
            "gate_status": result.get("gate_status", "unknown"),
            "gate_result": result.get("gate_result") or {},
            "status": status,
        }
    )


@router.get("/api/v1/evaluations")
def list_evaluations() -> List[Dict[str, Any]]:
    return main.storage.list_evaluations()


@router.get("/api/v1/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str) -> Dict[str, Any]:
    ev = main.storage.get_evaluation(evaluation_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found.")
    return ev


@router.post("/api/v1/evaluations")
def create_evaluation(request: EvaluationCreateRequest) -> Dict[str, Any]:
    from ..model_evaluation import run_evaluation

    config = {**(request.config or {}), "task": request.task}
    try:
        result = run_evaluation(request.rows, config)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Evaluation failed: {exc}")
    return _persist(request.name, request.description, request.status, result, model_id=None)


@router.post("/api/v1/evaluations/backtest")
def backtest_evaluation(request: EvaluationCreateRequest) -> Dict[str, Any]:
    """Label-based temporal (vintage) backtest — same as create but requires a date column
    so the metric suite is recomputed per cohort."""
    from ..model_evaluation import run_evaluation

    config = {**(request.config or {}), "task": request.task}
    if not config.get("date_col"):
        raise HTTPException(status_code=422, detail="backtest requires config.date_col")
    try:
        result = run_evaluation(request.rows, config)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Backtest failed: {exc}")
    return _persist(request.name, request.description, request.status, result, model_id=None)


@router.post("/api/v1/evaluations/from-model/{model_id}")
def evaluate_hosted_model(model_id: str, request: ModelEvalRequest) -> Dict[str, Any]:
    """Score `rows` (feature dicts + a label column) with a hosted model, then evaluate."""
    from ..model_evaluation import run_evaluation, score_rows_with_model

    model = main.storage.get_model(model_id, include_blob=True)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    blob = model.get("model_blob")
    if not blob:
        raise HTTPException(status_code=422, detail="Model has no binary data.")

    try:
        scored = score_rows_with_model(blob, request.rows, request.features, request.label_col)
        config = {**(request.config or {}), "task": "binary", "score_col": "score", "label_col": "label"}
        result = run_evaluation(scored, config)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Evaluation failed: {exc}")
    return _persist(request.name, request.description, request.status, result, model_id=model_id)


@router.delete("/api/v1/evaluations/{evaluation_id}")
def delete_evaluation(evaluation_id: str) -> Dict[str, Any]:
    if not main.storage.get_evaluation(evaluation_id):
        raise HTTPException(status_code=404, detail="Evaluation not found.")
    main.storage.delete_evaluation(evaluation_id)
    return {"deleted": True, "id": evaluation_id}
