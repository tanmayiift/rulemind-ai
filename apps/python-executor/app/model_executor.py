"""
Model executor for hosting and running pickle (.pkl) ML models.

Models are loaded and executed in the sandbox process pool for security.
Supports scikit-learn compatible models (predict / predict_proba).
"""

from __future__ import annotations

import base64
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable, Dict, List, Optional


MAX_MODEL_SIZE_MB = 50

# Wall-clock cap for a single model deserialize+predict in the isolated worker.
MODEL_TIMEOUT_MS = int(os.getenv("MODEL_TIMEOUT_MS", "5000"))
# Address-space cap for a model worker. Much higher than the variable sandbox's because numpy +
# scikit-learn reserve large virtual regions just to import; too low a cap makes every prediction
# fail with MemoryError. Still a hard ceiling on a runaway model.
_MODEL_MEMORY_MB = int(os.getenv("MODEL_MEMORY_MB", "512"))

# A DEDICATED process pool for models, separate from the variable sandbox: models need far more
# memory and run a different (heavier, rarer) workload, so sharing one pool would either starve
# models of memory or over-provision every variable worker.
_model_pool: Optional[ProcessPoolExecutor] = None
_model_pool_lock = threading.Lock()
_MODEL_POOL_SIZE = int(os.getenv("MODEL_POOL_SIZE", "2"))


def _model_worker_init() -> None:
    # Reuse the sandbox's env scrub so an uploaded pickle executing in this worker cannot read
    # the master encryption key or DB credentials from the inherited environment.
    from . import sandbox

    sandbox._scrub_worker_env()
    try:
        import resource  # type: ignore

        bytes_limit = _MODEL_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
    except (ImportError, ValueError, OSError):
        pass


def _get_model_pool() -> ProcessPoolExecutor:
    global _model_pool
    with _model_pool_lock:
        if _model_pool is None or getattr(_model_pool, "_broken", False):
            import multiprocessing

            mp_context = multiprocessing.get_context("spawn")
            _model_pool = ProcessPoolExecutor(
                max_workers=_MODEL_POOL_SIZE, mp_context=mp_context, initializer=_model_worker_init)
        return _model_pool


def _recycle_model_pool(pool: ProcessPoolExecutor) -> None:
    global _model_pool
    with _model_pool_lock:
        if _model_pool is not pool:
            return
        for process in list(getattr(pool, "_processes", {}).values()):
            try:
                process.terminate()
            except Exception:  # pragma: no cover
                pass
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover
            pass
        _model_pool = None


def _run_isolated(func_name: str, *args: Any) -> Dict[str, Any]:
    """Run execute_model/validate_model in the model pool.

    This is the actual fix for the pickle RCE. pickle.loads runs the payload's __reduce__ on
    DESERIALIZATION -- before any predict() -- so unpickling an untrusted blob is arbitrary code
    execution. Doing it here, in a spawned worker with the secrets scrubbed from its environment,
    a memory cap, and a kill-on-timeout, means a malicious pickle executes in a disposable
    process instead of in the API process that holds every tenant's credentials. Upload is also
    gated behind MANAGE_ACCESS (rbac), so it takes an admin to get a blob this far at all.
    """
    pool = _get_model_pool()
    target = _ISOLATED_TARGETS[func_name]
    try:
        future: Any = pool.submit(target, *args)
        return future.result(timeout=MODEL_TIMEOUT_MS / 1000.0)
    except FuturesTimeoutError:
        _recycle_model_pool(pool)
        return {"prediction": None, "valid": False, "error": "Model execution timed out.", "latency_ms": float(MODEL_TIMEOUT_MS)}
    except BrokenProcessPool as exc:
        _recycle_model_pool(pool)
        return {"prediction": None, "valid": False, "error": "Model sandbox unavailable: {0}".format(exc), "latency_ms": 0.0}




# Errors the CALLER caused (bad or absent feature values) are tagged so the router
# can answer 422 instead of 500. A malformed request is not a server fault, and a
# 5xx on it pages on-call and burns the error budget for someone else's typo.
INVALID_INPUT = "invalid_input"


def prepare_features(
    input_data: Dict[str, Any],
    features: Optional[List[str]] = None,
    allow_missing: bool = False,
) -> Dict[str, Any]:
    """Turn a feature dict into the ordered numeric row a model expects.

    Two failure modes used to pass silently or blow up:

    * A feature absent from `input_data` was substituted with `0`. For a scoring
      model that is indistinguishable from a genuine zero, so an upstream
      integration that dropped a field got a confident, wrong prediction with no
      error and no warning. Missing features are now rejected by default; pass
      `allow_missing=True` to opt into imputation, and the response then names
      every feature that was imputed.
    * A non-numeric value raised out of `numpy.array(..., dtype=float)` and
      surfaced as a 500.
    """
    ordered = list(features) if features else list(input_data.keys())
    if not ordered:
        return {"error": "No features supplied.", "error_kind": INVALID_INPUT}

    missing = [f for f in ordered if input_data.get(f) is None]
    if missing and not allow_missing:
        return {
            "error": "Missing value for feature(s): {0}. Supply them, or set "
                     "allow_missing=true to impute 0 (the response will say which were imputed).".format(
                         ", ".join(sorted(missing))),
            "error_kind": INVALID_INPUT,
        }

    row: List[float] = []
    bad: List[str] = []
    for name in ordered:
        raw = input_data.get(name)
        if raw is None:
            row.append(0.0)
            continue
        if isinstance(raw, bool):
            row.append(1.0 if raw else 0.0)
            continue
        try:
            row.append(float(raw))
        except (TypeError, ValueError):
            bad.append("{0}={1!r}".format(name, raw))
    if bad:
        return {
            "error": "Non-numeric value for feature(s): {0}.".format(", ".join(bad)),
            "error_kind": INVALID_INPUT,
        }

    return {"values": [row], "features": ordered, "imputed_features": sorted(missing)}


def _execute_model_worker(
    model_blob: bytes,
    input_data: Dict[str, Any],
    features: Optional[List[str]] = None,
    allow_missing: bool = False,
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

        prepared = prepare_features(input_data, features, allow_missing=allow_missing)
        if prepared.get("error"):
            prepared["prediction"] = None
            prepared["latency_ms"] = _ms(started)
            return prepared
        values = prepared["values"]
        features = prepared["features"]
        imputed = prepared["imputed_features"]

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
            # Non-empty only when the caller opted into allow_missing, so an imputed
            # zero is never mistaken for a value the caller actually supplied.
            "imputed_features": imputed,
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


def _validate_model_worker(model_blob: bytes) -> Dict[str, Any]:
    """Validate a model blob. RUNS IN A POOL WORKER -- it calls pickle.loads."""
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


def _too_large(model_blob: bytes) -> Optional[Dict[str, Any]]:
    size_mb = len(model_blob) / (1024 * 1024)
    if size_mb > MAX_MODEL_SIZE_MB:
        return {"error": "Model size ({0:.1f}MB) exceeds limit ({1}MB)".format(size_mb, MAX_MODEL_SIZE_MB)}
    return None


def execute_model(
    model_blob: bytes,
    input_data: Dict[str, Any],
    features: Optional[List[str]] = None,
    allow_missing: bool = False,
) -> Dict[str, Any]:
    """Run a stored model in an ISOLATED worker (see _run_isolated). Public entry point."""
    oversize = _too_large(model_blob)
    if oversize:
        return {"prediction": None, "error": oversize["error"], "latency_ms": 0.0}
    return _run_isolated("execute", model_blob, input_data, features, allow_missing)


def validate_model(model_blob: bytes) -> Dict[str, Any]:
    """Validate an uploaded model in an ISOLATED worker. Public entry point.

    The size guard is enforced HERE, before the blob is unpickled anywhere -- it used to live
    only in execute_model, so validate_model (the upload path) would deserialize an arbitrarily
    large blob and could be used to OOM the process at upload time.
    """
    oversize = _too_large(model_blob)
    if oversize:
        return {"valid": False, "model_type": None, "has_predict": False,
                "has_predict_proba": False, "error": oversize["error"]}
    return _run_isolated("validate", model_blob)


# Name -> worker function, resolved by _run_isolated. Both are module-level so ProcessPoolExecutor
# can pickle the reference across to the spawned worker.
_ISOLATED_TARGETS: Dict[str, "Callable[..., Dict[str, Any]]"] = {
    "execute": _execute_model_worker,
    "validate": _validate_model_worker,
}


def base64_to_model(b64_string: str) -> bytes:
    return base64.b64decode(b64_string)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
