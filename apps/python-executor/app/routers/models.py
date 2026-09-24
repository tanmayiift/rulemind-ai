"""Hosted-model endpoints — list/get/create, predict, test, delete (ONNX, PMML, XGBoost, LightGBM, pickle).
Extracted verbatim from app/main.py. Stable helpers/models imported by value from app.main; direct
storage calls use main.storage live."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

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

    # Models were the one entity type still deriving their id from `name`, and on a
    # collision appending random hex — so uploading the same model twice produced
    # `my_model` and `my_model_a3f19c`, neither predictable by the caller. Honour an
    # explicit id like the other types do, and reject a collision rather than
    # inventing a different id.
    existing_ids = {m["id"] for m in main.storage.list_models()}
    if request.id:
        model_id = slugify(request.id) or request.id
        if model_id in existing_ids:
            raise HTTPException(
                status_code=409,
                detail=f"model id '{model_id}' already exists. Use PUT to update it, or choose another id.",
            )
    else:
        model_id = slugify(request.name)
        if model_id in existing_ids:
            model_id = f"{model_id}_{uuid.uuid4().hex[:6]}"

    model_data = {
        "id": model_id,
        "name": request.name,
        "description": request.description,
        # Both from the bytes, not from the request: a declared type is a label the caller
        # typed, and the loader is chosen by what the file actually is.
        "model_type": validation.get("model_type") or request.model_type,
        "model_format": validation.get("model_format", "pickle"),
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


def _expected_features(model: Dict[str, Any], request) -> Optional[List[str]]:
    """The ordered feature list to score against.

    Falls back to the model's declared `input_schema.features` when the caller
    sends none — otherwise a request that simply omits a feature would define its
    own (shorter) feature list and the missing-value check could never fire.
    """
    if request.features:
        return request.features
    schema = model.get("input_schema") or {}
    declared = schema.get("features") if isinstance(schema, dict) else None
    if isinstance(declared, list) and declared:
        return [str(f) for f in declared]
    return None


@router.post("/api/v1/models/{model_id}/predict")
def predict_model(model_id: str, request: ModelPredictRequest) -> Dict[str, Any]:
    from ..model_executor import INVALID_INPUT, execute_model

    model = main.storage.get_model(model_id, include_blob=True)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    model_blob = model.get("model_blob")
    if not model_blob:
        raise HTTPException(status_code=422, detail="Model has no binary data.")

    result = execute_model(model_blob, request.input_data,
                           features=_expected_features(model, request),
                           allow_missing=request.allow_missing)
    if result.get("error"):
        # A bad feature value is the caller's error, not the server's.
        status = 422 if result.get("error_kind") == INVALID_INPUT else 500
        raise HTTPException(status_code=status, detail=result["error"])
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

    result = execute_model(model_blob, request.input_data,
                           features=_expected_features(model, request),
                           allow_missing=request.allow_missing)
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
