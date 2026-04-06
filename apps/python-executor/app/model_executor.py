"""
Model executor for hosting and running pickle (.pkl) ML models.

Models are loaded and executed in the sandbox process pool for security.
Supports scikit-learn compatible models (predict / predict_proba).
"""

from __future__ import annotations

import base64
import io
import time
from typing import Any, Dict, List, Optional


MAX_MODEL_SIZE_MB = 50


def execute_model(
    model_blob: bytes,
    input_data: Dict[str, Any],
    features: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Execute a pickle model with the given input data.

    The model is deserialized in the current process. For production use,
    this should be called inside a sandbox worker process.

    Args:
        model_blob: Raw bytes of the pickled model.
        input_data: Dictionary of feature name -> value.
        features: Optional ordered list of feature names.

    Returns:
        Dictionary with prediction results and metadata.
    """
    started = time.perf_counter()
    try:
        import pickle  # noqa: S403

        # Size guard
        size_mb = len(model_blob) / (1024 * 1024)
        if size_mb > MAX_MODEL_SIZE_MB:
            return {
                "prediction": None,
                "error": f"Model size ({size_mb:.1f}MB) exceeds limit ({MAX_MODEL_SIZE_MB}MB)",
                "latency_ms": _ms(started),
            }

        model = pickle.loads(model_blob)  # noqa: S301

        # Build input array
        if features:
            values = [[input_data.get(f, 0) for f in features]]
        else:
            values = [list(input_data.values())]

        # Try numpy array if available
        try:
            import numpy as np
            values_array = np.array(values, dtype=float)
        except ImportError:
            values_array = values

        # Predict
        prediction = None
        probabilities = None
        prediction_raw = None

        if hasattr(model, "predict"):
            prediction_raw = model.predict(values_array)
            if hasattr(prediction_raw, "tolist"):
                prediction = prediction_raw.tolist()
            else:
                prediction = list(prediction_raw) if hasattr(prediction_raw, "__iter__") else [prediction_raw]

        if hasattr(model, "predict_proba"):
            try:
                proba_raw = model.predict_proba(values_array)
                if hasattr(proba_raw, "tolist"):
                    probabilities = proba_raw.tolist()
                else:
                    probabilities = list(proba_raw) if hasattr(proba_raw, "__iter__") else [proba_raw]
            except Exception:
                probabilities = None

        # Extract model metadata
        model_type = type(model).__name__
        model_classes = None
        if hasattr(model, "classes_"):
            classes = model.classes_
            model_classes = classes.tolist() if hasattr(classes, "tolist") else list(classes)

        feature_importances = None
        if hasattr(model, "feature_importances_"):
            fi = model.feature_importances_
            feature_importances = fi.tolist() if hasattr(fi, "tolist") else list(fi)

        return {
            "prediction": prediction[0] if prediction and len(prediction) == 1 else prediction,
            "probabilities": probabilities[0] if probabilities and len(probabilities) == 1 else probabilities,
            "model_type": model_type,
            "model_classes": model_classes,
            "feature_importances": feature_importances,
            "input_features": features or list(input_data.keys()),
            "input_values": values[0],
            "latency_ms": _ms(started),
            "error": None,
        }

    except ImportError as exc:
        return {
            "prediction": None,
            "error": f"Missing dependency: {exc}. Install scikit-learn/numpy to use model hosting.",
            "latency_ms": _ms(started),
        }
    except Exception as exc:
        return {
            "prediction": None,
            "error": str(exc),
            "latency_ms": _ms(started),
        }


def validate_model(model_blob: bytes) -> Dict[str, Any]:
    """Validate that a model blob is a valid pickle with predict method."""
    try:
        import pickle  # noqa: S403
        model = pickle.loads(model_blob)  # noqa: S301

        has_predict = hasattr(model, "predict")
        has_proba = hasattr(model, "predict_proba")
        model_type = type(model).__name__

        return {
            "valid": has_predict,
            "model_type": model_type,
            "has_predict": has_predict,
            "has_predict_proba": has_proba,
            "error": None if has_predict else "Model does not have a predict() method",
        }
    except Exception as exc:
        return {
            "valid": False,
            "model_type": None,
            "has_predict": False,
            "has_predict_proba": False,
            "error": str(exc),
        }


def model_to_base64(model_blob: bytes) -> str:
    return base64.b64encode(model_blob).decode("ascii")


def base64_to_model(b64_string: str) -> bytes:
    return base64.b64decode(b64_string)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
