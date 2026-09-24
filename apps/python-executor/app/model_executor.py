"""
Model executor for hosted ML models: ONNX, PMML (Scorecard / RegressionModel / TreeModel),
XGBoost JSON, LightGBM model files, and pickled scikit-learn-compatible models.

Every model is loaded and scored in a dedicated, isolated process pool. The format is detected
from the blob's own bytes (`detect_format`); only pickle executes code when loaded, and it can be
switched off entirely with MODEL_ALLOW_PICKLE=0.
"""

from __future__ import annotations

import base64
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import env


MAX_MODEL_SIZE_MB = 50

# Wall-clock cap for a single model deserialize+predict in the isolated worker.
MODEL_TIMEOUT_MS = env.env_int("MODEL_TIMEOUT_MS", 5000)
# Address-space cap for a model worker. Much higher than the variable sandbox's because numpy +
# scikit-learn reserve large virtual regions just to import; too low a cap makes every prediction
# fail. Still a hard ceiling on a runaway model.
#
# THE DEFAULT USED TO BE 512, AND THAT BROKE EVERY LINUX DEPLOYMENT. Bisected inside the shipped
# api image (python 3.11, aarch64 linux, scipy-openblas):
#
#     256 MB -> fails: libscipy_openblas-*.so: failed to map segment from shared object
#     512 MB -> fails: sklearn/metrics/_pairwise_distances_reduction/*.so
#     768 MB -> imports OK
#
# So the shipped default sat below the floor for the stack shipped alongside it: uploading a
# scikit-learn model returned 422 "Invalid model: failed to map segment from shared object", and
# every `model` policy step failed, in compose and in the chart alike.
#
# None of the ~5,000 tests could see it. They run on darwin, where setrlimit(RLIMIT_AS) is
# REFUSED outright ("current limit exceeds maximum limit"), _model_worker_init swallows that
# OSError, and the cap is therefore never applied -- sklearn imports fine and every model test
# passes. The mechanism under test does nothing on the only platform that tests it.
#
# 2048 gives real headroom over the 768 MB import floor for the model's own working set. RLIMIT_AS
# caps VIRTUAL address space, which a BLAS stack reserves lavishly and which is a poor proxy for
# RSS, so a tight value here buys less than it looks like it does. The containment that actually
# holds is the process boundary, the kill-on-timeout, and the container's own memory limit.
MODEL_MEMORY_FLOOR_MB = 768  # measured; below this the shipped ML stack cannot import at all
_MODEL_MEMORY_MB = env.env_int("MODEL_MEMORY_MB", 2048)

# A DEDICATED process pool for models, separate from the variable sandbox: models need far more
# memory and run a different (heavier, rarer) workload, so sharing one pool would either starve
# models of memory or over-provision every variable worker.
_model_pool: Optional[ProcessPoolExecutor] = None
_model_pool_lock = threading.Lock()
_MODEL_POOL_SIZE = env.env_int("MODEL_POOL_SIZE", 2)


def _model_worker_init() -> None:
    # Reuse the sandbox's env scrub so an uploaded pickle executing in this worker cannot read
    # the master encryption key or DB credentials from the inherited environment.
    from . import sandbox

    sandbox._scrub_worker_env()
    try:
        import resource  # type: ignore

        bytes_limit = _MODEL_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
    except (ImportError, ValueError, OSError) as exc:
        # Say so. This silently passed, and the silence is why a 512 MB default that broke every
        # Linux deployment survived: on darwin the call is REFUSED, the cap is never applied, and
        # nothing anywhere reported that the isolation this worker advertises was absent.
        # One line per worker start, to stderr, so it lands in container logs.
        print("model worker: RLIMIT_AS not applied (%s) -- this worker has NO memory ceiling"
              % exc, file=sys.stderr, flush=True)


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


def _run_isolated(func_name: str, *args: Any, timeout_ms: Optional[int] = None) -> Dict[str, Any]:
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
        return future.result(timeout=(timeout_ms or MODEL_TIMEOUT_MS) / 1000.0)
    except FuturesTimeoutError:
        _recycle_model_pool(pool)
        return {"prediction": None, "valid": False, "error": "Model execution timed out.",
                "latency_ms": float(timeout_ms or MODEL_TIMEOUT_MS)}
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


# ── formats ────────────────────────────────────────────────────────────────────────────────
# The format is read from the file's own bytes, never from a declared type: a stored model is run
# by the loader its bytes call for, so no metadata can route a blob to the wrong deserializer.
# The markers are disjoint -- pickle protocol 2+ opens with 0x80, an ONNX ModelProto with field 1
# (ir_version, varint) = 0x08, PMML with '<', XGBoost JSON with '{', a LightGBM model file with
# "tree". Anything else is treated as a legacy (protocol 0/1) pickle, which is how every model
# uploaded before these formats existed is still served.
MODEL_FORMATS = ("pickle", "onnx", "pmml", "xgboost_json", "lightgbm_text")

# Pickle is the one format whose LOAD executes code (see _run_isolated). The worker isolation is
# the containment; this is the switch for an operator who would rather not host pickles at all.
# On by default because existing deployments hold pickled models; a regulated install that
# standardises on ONNX/PMML turns it off and every pickle is refused at upload and at scoring.
ALLOW_PICKLE = env.env_bool("MODEL_ALLOW_PICKLE", True)

# Batch scoring (evaluations): rows per worker call, and the wall clock one call may take. Split so
# a 50k-row evaluation is 25 worker calls, not 50,000 -- and not one call that can outlive any
# sane timeout.
BATCH_CHUNK_ROWS = 2000
MODEL_BATCH_TIMEOUT_MS = env.env_int("MODEL_BATCH_TIMEOUT_MS", 60000)


def detect_format(model_blob: bytes) -> str:
    if model_blob[:1] == b"\x80":
        return "pickle"
    if model_blob[:1] == b"\x08":
        return "onnx"
    head = model_blob[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<"):
        return "pmml"
    if head.startswith(b"{"):
        return "xgboost_json"
    if head.startswith(b"tree"):
        return "lightgbm_text"
    return "pickle"


def _pickle_refused(model_format: str) -> Optional[str]:
    if model_format == "pickle" and not ALLOW_PICKLE:
        return ("Pickle models are disabled on this installation (MODEL_ALLOW_PICKLE=0). "
                "Export the model as ONNX, PMML, XGBoost JSON or a LightGBM model file.")
    return None


class _Loaded:
    """A model loaded in the worker, behind one interface for every format."""

    def __init__(self, model_format: str, model_type: str, score: "Callable[[List[Dict[str, Any]], List[str]], List[Tuple[Any, Any]]]",
                 feature_names: Optional[List[str]] = None, classes: Optional[List[Any]] = None,
                 importances: Optional[List[float]] = None, has_proba: bool = False,
                 numeric: bool = True) -> None:
        self.format = model_format
        self.model_type = model_type
        self.score = score
        # Named-feature formats score BY NAME: the booster's own feature list decides the column
        # order, so a caller listing features in a different order cannot silently shift every
        # value into the wrong column -- the failure a positional model makes invisible.
        self.feature_names = feature_names
        self.classes = classes
        self.importances = importances
        self.has_proba = has_proba
        # PMML takes the raw values (a scorecard bins categorical strings); every other format
        # takes a float matrix.
        self.numeric = numeric


def _tolist(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else value


def _load(model_blob: bytes, model_format: str) -> _Loaded:
    """Deserialize in the WORKER. Only the pickle branch runs code on load."""
    import numpy as np

    if model_format == "onnx":
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(model_blob, sess_options=options, providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        if len(inputs) != 1:
            raise ValueError("ONNX models with {0} inputs are not supported; export with one float tensor "
                             "input of shape [N, features].".format(len(inputs)))
        tensor = inputs[0]
        width = tensor.shape[1] if len(tensor.shape) == 2 and isinstance(tensor.shape[1], int) else None
        outputs = [o.name for o in session.get_outputs()]
        classes_seen: Dict[str, Any] = {}

        def score_onnx(rows: List[Dict[str, Any]], ordered: List[str]) -> List[Tuple[Any, Any]]:
            matrix = np.array([[row[f] for f in ordered] for row in rows], dtype=np.float32)
            if width is not None and matrix.shape[1] != width:
                raise _InputError("The ONNX model takes {0} features; {1} were supplied.".format(width, matrix.shape[1]))
            result = session.run(outputs, {tensor.name: matrix})
            labels = _tolist(result[0])
            probs: List[Any] = [None] * len(rows)
            if len(result) > 1:
                raw = result[1]
                if isinstance(raw, list) and raw and isinstance(raw[0], dict):  # skl2onnx ZipMap
                    keys = sorted(raw[0].keys(), key=lambda k: str(k))
                    classes_seen["classes"] = keys
                    probs = [[float(d[k]) for k in keys] for d in raw]
                else:
                    probs = _tolist(raw)
            return [(_tolist(label), prob) for label, prob in zip(labels, probs, strict=True)]

        meta = session.get_modelmeta()
        loaded = _Loaded("onnx", "onnx:{0}".format(meta.producer_name or "model"), score_onnx,
                         has_proba=len(outputs) > 1)
        loaded.classes = classes_seen  # filled on first score; unwrapped by the caller
        return loaded

    if model_format == "pmml":
        from .pmml import PMMLModel

        pmml_model = PMMLModel(model_blob)

        def score_pmml(rows: List[Dict[str, Any]], ordered: List[str]) -> List[Tuple[Any, Any]]:
            out = []
            for row in rows:
                result = pmml_model.predict(row)
                out.append((result["prediction"], result.get("probabilities")))
            return out

        return _Loaded("pmml", "pmml:{0}".format(pmml_model.kind), score_pmml,
                       feature_names=list(pmml_model.inputs),
                       has_proba=pmml_model.function == "classification", numeric=False)

    if model_format == "xgboost_json":
        import json

        import xgboost as xgb

        try:
            config = json.loads(model_blob)
        except ValueError as exc:
            raise ValueError("Not an XGBoost JSON model: {0}".format(exc)) from exc
        if not isinstance(config, dict) or "learner" not in config:
            raise ValueError("A JSON model file must be an XGBoost model (a 'learner' object).")
        booster = xgb.Booster()
        booster.load_model(bytearray(model_blob))
        objective = json.loads(booster.save_config())["learner"]["objective"]["name"]
        names = list(booster.feature_names or []) or None
        binary = objective.startswith("binary:")

        def score_xgb(rows: List[Dict[str, Any]], ordered: List[str]) -> List[Tuple[Any, Any]]:
            matrix = np.array([[row[f] for f in ordered] for row in rows], dtype=np.float32)
            predictions = booster.predict(xgb.DMatrix(matrix, feature_names=names if names else None))
            return [(float(p), [1.0 - float(p), float(p)] if binary else None) for p in _tolist(predictions)]

        return _Loaded("xgboost_json", "xgboost:{0}".format(objective), score_xgb,
                       feature_names=names, has_proba=binary)

    if model_format == "lightgbm_text":
        import lightgbm as lgb

        booster = lgb.Booster(model_str=model_blob.decode("utf-8"))
        objective = str(booster.dump_model().get("objective", "")).split(" ")[0]
        names = list(booster.feature_name() or []) or None
        # LightGBM names unnamed features Column_0..N; those are positions, not names.
        if names and all(n.startswith("Column_") for n in names):
            names = None
        binary = objective in ("binary", "cross_entropy")

        def score_lgb(rows: List[Dict[str, Any]], ordered: List[str]) -> List[Tuple[Any, Any]]:
            matrix = np.array([[row[f] for f in ordered] for row in rows], dtype=np.float64)
            predictions = booster.predict(matrix)
            return [(float(p), [1.0 - float(p), float(p)] if binary else None) for p in _tolist(predictions)]

        return _Loaded("lightgbm_text", "lightgbm:{0}".format(objective or "model"), score_lgb,
                       feature_names=names, has_proba=binary)

    import pickle  # noqa: S403

    model = pickle.loads(model_blob)  # noqa: S301
    if not hasattr(model, "predict"):
        raise ValueError("Model does not have a predict() method")
    has_proba = hasattr(model, "predict_proba")

    def score_pickle(rows: List[Dict[str, Any]], ordered: List[str]) -> List[Tuple[Any, Any]]:
        matrix = np.array([[row[f] for f in ordered] for row in rows], dtype=float)
        predictions = _tolist(model.predict(matrix))
        probabilities: List[Any] = [None] * len(rows)
        if has_proba:
            try:
                probabilities = _tolist(model.predict_proba(matrix))
            except Exception:
                probabilities = [None] * len(rows)
        return list(zip(predictions, probabilities, strict=True))

    classes = _tolist(model.classes_) if hasattr(model, "classes_") else None
    importances = _tolist(model.feature_importances_) if hasattr(model, "feature_importances_") else None
    return _Loaded("pickle", type(model).__name__, score_pickle, classes=classes,
                   importances=importances, has_proba=has_proba)


class _InputError(ValueError):
    """The caller's data, not the model or the server."""


def _prepare_rows(loaded: _Loaded, rows: List[Dict[str, Any]], features: Optional[List[str]],
                  allow_missing: bool) -> Tuple[List[Dict[str, Any]], List[str], List[List[str]]]:
    """(rows ready to score, feature order, imputed features per row)."""
    ordered = loaded.feature_names or (list(features) if features else list(rows[0].keys()))
    if not loaded.numeric:
        # PMML: values by name, as supplied -- the evaluator decides what a missing value means
        # (a scorecard can bin it; a regression refuses it).
        return [{name: row.get(name) for name in ordered} for row in rows], ordered, [[] for _ in rows]
    prepared_rows, imputed_rows = [], []
    for index, row in enumerate(rows):
        prepared = prepare_features(row, ordered, allow_missing=allow_missing)
        if prepared.get("error"):
            prefix = "" if len(rows) == 1 else "Row {0}: ".format(index)
            raise _InputError(prefix + prepared["error"])
        prepared_rows.append(dict(zip(prepared["features"], prepared["values"][0], strict=True)))
        imputed_rows.append(prepared["imputed_features"])
    return prepared_rows, ordered, imputed_rows


def _execute_model_worker(
    model_blob: bytes,
    input_data: Dict[str, Any],
    features: Optional[List[str]] = None,
    allow_missing: bool = False,
) -> Dict[str, Any]:
    """Score one row. RUNS IN A POOL WORKER. `prediction` is what the format's own runtime calls
    its prediction: the class for a scikit-learn or ONNX classifier, the probability for a binary
    XGBoost/LightGBM booster, the score for a PMML scorecard."""
    started = time.perf_counter()
    model_format = detect_format(model_blob)
    try:
        loaded = _load(model_blob, model_format)
        rows, ordered, imputed = _prepare_rows(loaded, [input_data], features, allow_missing)
        prediction, probabilities = loaded.score(rows, ordered)[0]
        classes = loaded.classes.get("classes") if isinstance(loaded.classes, dict) else loaded.classes
        return {
            "prediction": prediction,
            "probabilities": probabilities,
            "model_type": loaded.model_type,
            "model_format": model_format,
            "model_classes": classes,
            "feature_importances": loaded.importances,
            "input_features": ordered,
            "input_values": [rows[0].get(name) for name in ordered],
            # Non-empty only when the caller opted into allow_missing, so an imputed
            # zero is never mistaken for a value the caller actually supplied.
            "imputed_features": imputed[0],
            "latency_ms": _ms(started),
            "error": None,
        }
    except _InputError as exc:
        return {"prediction": None, "error": str(exc), "error_kind": INVALID_INPUT,
                "model_format": model_format, "latency_ms": _ms(started)}
    except ImportError as exc:
        return {"prediction": None, "model_format": model_format, "latency_ms": _ms(started),
                "error": "Missing dependency for {0} models: {1}.".format(model_format, exc)}
    except Exception as exc:
        return {"prediction": None, "error": str(exc), "model_format": model_format, "latency_ms": _ms(started)}


def _execute_batch_worker(
    model_blob: bytes,
    rows: List[Dict[str, Any]],
    features: Optional[List[str]] = None,
    allow_missing: bool = False,
) -> Dict[str, Any]:
    """Score many rows with ONE load and one vectorised predict. RUNS IN A POOL WORKER."""
    started = time.perf_counter()
    model_format = detect_format(model_blob)
    try:
        loaded = _load(model_blob, model_format)
        prepared, ordered, _ = _prepare_rows(loaded, rows, features, allow_missing)
        scored = loaded.score(prepared, ordered)
        return {"predictions": [p for p, _ in scored], "probabilities": [q for _, q in scored],
                "model_format": model_format, "model_type": loaded.model_type,
                "input_features": ordered, "error": None, "latency_ms": _ms(started)}
    except _InputError as exc:
        return {"error": str(exc), "error_kind": INVALID_INPUT, "model_format": model_format}
    except Exception as exc:
        return {"error": str(exc), "model_format": model_format}


def _validate_model_worker(model_blob: bytes) -> Dict[str, Any]:
    """Load a blob the way scoring will, and report what it is. RUNS IN A POOL WORKER."""
    model_format = detect_format(model_blob)
    try:
        loaded = _load(model_blob, model_format)
        return {"valid": True, "model_type": loaded.model_type, "model_format": model_format,
                "has_predict": True, "has_predict_proba": loaded.has_proba,
                "input_features": loaded.feature_names, "error": None}
    except Exception as exc:
        return {"valid": False, "model_type": None, "model_format": model_format,
                "has_predict": False, "has_predict_proba": False, "error": str(exc)}


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
    refused = _pickle_refused(detect_format(model_blob))
    if refused:
        return {"prediction": None, "error": refused, "latency_ms": 0.0}
    return _run_isolated("execute", model_blob, input_data, features, allow_missing)


def execute_model_batch(
    model_blob: bytes,
    rows: List[Dict[str, Any]],
    features: Optional[List[str]] = None,
    allow_missing: bool = False,
) -> Dict[str, Any]:
    """Score many rows: one model load and one vectorised predict per chunk of BATCH_CHUNK_ROWS,
    each chunk in the isolated worker. Scoring row by row cost a process round trip and a full
    model deserialize PER ROW -- the reason an evaluation over a real portfolio could not finish."""
    oversize = _too_large(model_blob)
    if oversize:
        return {"error": oversize["error"]}
    refused = _pickle_refused(detect_format(model_blob))
    if refused:
        return {"error": refused}
    if not rows:
        return {"predictions": [], "probabilities": [], "error": None}
    predictions: List[Any] = []
    probabilities: List[Any] = []
    last: Dict[str, Any] = {}
    for start in range(0, len(rows), BATCH_CHUNK_ROWS):
        chunk = rows[start:start + BATCH_CHUNK_ROWS]
        last = _run_isolated("batch", model_blob, chunk, features, allow_missing,
                             timeout_ms=MODEL_BATCH_TIMEOUT_MS)
        if last.get("error"):
            if start:
                last["error"] = "Rows {0}-{1}: {2}".format(start, start + len(chunk) - 1, last["error"])
            return last
        predictions.extend(last["predictions"])
        probabilities.extend(last["probabilities"])
    return {**last, "predictions": predictions, "probabilities": probabilities}


def validate_model(model_blob: bytes) -> Dict[str, Any]:
    """Validate an uploaded model in an ISOLATED worker. Public entry point.

    The size guard is enforced HERE, before the blob is loaded anywhere -- it used to live
    only in execute_model, so validate_model (the upload path) would deserialize an arbitrarily
    large blob and could be used to OOM the process at upload time.
    """
    oversize = _too_large(model_blob)
    if oversize:
        return {"valid": False, "model_type": None, "has_predict": False,
                "has_predict_proba": False, "error": oversize["error"]}
    model_format = detect_format(model_blob)
    refused = _pickle_refused(model_format)
    if refused:
        return {"valid": False, "model_type": None, "model_format": model_format, "has_predict": False,
                "has_predict_proba": False, "error": refused}
    return _run_isolated("validate", model_blob)


# Name -> worker function, resolved by _run_isolated. Module-level so ProcessPoolExecutor can
# pickle the reference across to the spawned worker.
_ISOLATED_TARGETS: Dict[str, "Callable[..., Dict[str, Any]]"] = {
    "execute": _execute_model_worker,
    "batch": _execute_batch_worker,
    "validate": _validate_model_worker,
}


def base64_to_model(b64_string: str) -> bytes:
    return base64.b64decode(b64_string)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
