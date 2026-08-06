"""Hosted-model endpoints — list/get/create, predict, test, delete (pickled scikit-style models).
Extracted verbatim from app/main.py. Stable helpers/models imported by value from app.main; direct
storage calls use main.storage live."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from .. import main
from ..logic import now_iso, slugify
from ..main import ModelCreateRequest, ModelPredictRequest

router = APIRouter()


@router.get("/api/v1/models")
def list_models() -> List[Dict[str, Any]]:
    return main.storage.list_models()


@router.get("/api/v1/models/{model_id}")
def get_model(model_id: str) -> Dict[str, Any]:
    model = main.storage.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    return model


@router.post("/api/v1/models")
def create_model(request: ModelCreateRequest) -> Dict[str, Any]:
    from ..model_executor import base64_to_model, validate_model

    model_blob = base64_to_model(request.model_base64)
    validation = validate_model(model_blob)
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=f"Invalid model: {validation['error']}")

    model_id = slugify(request.name)
    existing_ids = {m["id"] for m in main.storage.list_models()}
    if model_id in existing_ids:
        model_id = f"{model_id}_{uuid.uuid4().hex[:6]}"

    model_data = {
        "id": model_id,
        "name": request.name,
        "description": request.description,
        "model_type": validation.get("model_type", request.model_type),
        "model_blob": model_blob,
        "input_schema": request.input_schema,
        "output_schema": request.output_schema,
        "metrics": request.metrics,
        "status": request.status,
        "version": 1,
        "has_predict": validation["has_predict"],
        "has_predict_proba": validation["has_predict_proba"],
    }
    return main.storage.create_model(model_data)


@router.post("/api/v1/models/{model_id}/predict")
def predict_model(model_id: str, request: ModelPredictRequest) -> Dict[str, Any]:
    from ..model_executor import execute_model

    model = main.storage.get_model(model_id, include_blob=True)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    model_blob = model.get("model_blob")
    if not model_blob:
        raise HTTPException(status_code=422, detail="Model has no binary data.")

    result = execute_model(model_blob, request.input_data, features=request.features)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.post("/api/v1/models/{model_id}/test")
def test_model(model_id: str, request: ModelPredictRequest) -> Dict[str, Any]:
    from ..model_executor import execute_model

    model = main.storage.get_model(model_id, include_blob=True)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    model_blob = model.get("model_blob")
    if not model_blob:
        raise HTTPException(status_code=422, detail="Model has no binary data.")

    result = execute_model(model_blob, request.input_data, features=request.features)
    main.storage.update_model(model_id, {"last_test_result": {
        "prediction": result.get("prediction"),
        "latency_ms": result.get("latency_ms"),
        "tested_at": now_iso(),
        "error": result.get("error"),
    }})
    return {"model": main.storage.get_model(model_id), "result": result}


@router.delete("/api/v1/models/{model_id}")
def delete_model(model_id: str) -> Dict[str, Any]:
    model = main.storage.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    main.storage.delete_model(model_id)
    return {"deleted": True, "id": model_id}
