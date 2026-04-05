import ast
import hashlib
import inspect
import json
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, Optional


MAX_EXECUTION_MS = 2000
MAX_MEMORY_MB = 128
SAFE_IMPORTS = {"math", "datetime", "json", "re"}
BLOCKED_NAMES = {"open", "eval", "exec", "__import__", "os", "sys", "subprocess", "socket", "requests"}
SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}

# Cache for validated+compiled code to avoid re-parsing on every call
_validated_code_cache: Dict[str, bool] = {}
_compiled_code_cache: Dict[str, Any] = {}

# Pre-warmed process pool (lazy-initialized)
_pool: Optional[ProcessPoolExecutor] = None
_POOL_SIZE = int(os.getenv("SANDBOX_POOL_SIZE", "4"))


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        mp_context = multiprocessing.get_context("spawn")
        _pool = ProcessPoolExecutor(max_workers=_POOL_SIZE, mp_context=mp_context)
    return _pool


def variable(func=None, **metadata):
    def decorator(inner):
        inner.__rulemind_variable__ = True
        inner.__rulemind_metadata__ = metadata
        return inner

    if callable(func):
        return decorator(func)

    return decorator


def validate_source(source: str) -> None:
    code_hash = hashlib.md5(source.encode()).hexdigest()
    if code_hash in _validated_code_cache:
        return

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_names = [alias.name.split(".")[0] for alias in node.names]
            for module_name in module_names:
                if module_name not in SAFE_IMPORTS:
                    raise ValueError("Import not allowed: {0}".format(module_name))
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise ValueError("Blocked symbol detected: {0}".format(node.id))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"open", "eval", "exec"}:
            raise ValueError("Blocked call detected: {0}".format(node.func.id))

    _validated_code_cache[code_hash] = True


def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):  # pylint: disable=redefined-builtin
    base_name = name.split(".")[0]
    if base_name not in SAFE_IMPORTS:
        raise ImportError("Import not allowed: {0}".format(base_name))
    return __import__(name, globals, locals, fromlist, level)


def resolve_callable(namespace: Dict[str, Any]):
    decorated = [
        value for value in namespace.values() if callable(value) and getattr(value, "__rulemind_variable__", False)
    ]
    if decorated:
        return decorated[0]

    fallbacks = [
        value
        for key, value in namespace.items()
        if callable(value) and not key.startswith("_") and key not in {"variable", "restricted_import", "resolve_callable"}
    ]
    if not fallbacks:
        raise ValueError("No callable variable function found.")
    return fallbacks[0]


def _execute_in_process(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any], timeout_ms: int, memory_mb: int) -> Dict[str, Any]:
    """Execute variable code in a pool worker process with sandboxing."""
    started = time.perf_counter()
    try:
        try:
            import resource  # type: ignore

            bytes_limit = int(memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
        except Exception:
            pass

        validate_source(code)
        namespace = {
            "__builtins__": dict(SAFE_BUILTINS, __import__=restricted_import),
            "variable": variable,
            "json": json,
        }
        exec(code, namespace, namespace)  # pylint: disable=exec-used
        function = resolve_callable(namespace)
        signature = inspect.signature(function)
        parameter_count = len(signature.parameters)
        if parameter_count >= 3:
            result = function(payload, variables_map, {"connectors": {}})
        else:
            result = function(payload, variables_map)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return {"value": result, "error": None, "latency_ms": latency_ms, "variable_name": function.__name__}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return {"value": None, "error": str(exc), "latency_ms": latency_ms, "variable_name": None}


def _execute_inline(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any]) -> Dict[str, Any]:
    """Fast in-process execution for pre-validated code (no process spawn overhead)."""
    started = time.perf_counter()
    try:
        validate_source(code)
        namespace = {
            "__builtins__": dict(SAFE_BUILTINS, __import__=restricted_import),
            "variable": variable,
            "json": json,
        }
        exec(code, namespace, namespace)  # pylint: disable=exec-used
        function = resolve_callable(namespace)
        signature = inspect.signature(function)
        parameter_count = len(signature.parameters)
        if parameter_count >= 3:
            result = function(payload, variables_map, {"connectors": {}})
        else:
            result = function(payload, variables_map)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return {"value": result, "error": None, "latency_ms": latency_ms, "variable_name": function.__name__}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return {"value": None, "error": str(exc), "latency_ms": latency_ms, "variable_name": None}


def execute_variable(code: str, payload: Dict[str, Any], variables_map: Optional[Dict[str, Any]] = None, timeout_ms: int = MAX_EXECUTION_MS, memory_mb: int = MAX_MEMORY_MB) -> Dict[str, Any]:
    context = variables_map or {}
    timeout_seconds = max(float(timeout_ms) / 1000.0, 0.1)

    # Fast path: execute inline in the current process when sandbox isolation
    # is not strictly required (e.g. dev/test mode). The code is still validated
    # against the AST allowlist, but avoids ~200-500ms process spawn overhead.
    if os.getenv("SANDBOX_MODE", "pool") == "inline":
        return _execute_inline(code, payload, context)

    # Production path: submit to pre-warmed process pool instead of spawning
    # a fresh process per call. This amortizes the ~500ms spawn cost across
    # all requests and allows concurrent variable execution.
    try:
        pool = _get_pool()
        future = pool.submit(_execute_in_process, code, payload, context, timeout_ms, memory_mb)
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        return {"value": None, "error": "Variable execution timed out.", "latency_ms": float(timeout_ms), "variable_name": None}
    except Exception as exc:
        if os.getenv("SANDBOX_FALLBACK_MODE", "inline") == "inline":
            fallback = _execute_inline(code, payload, context)
            fallback["sandbox_fallback"] = "inline"
            fallback["sandbox_error"] = str(exc)
            return fallback
        return {"value": None, "error": "Sandbox execution unavailable: {0}".format(exc), "latency_ms": 0.0, "variable_name": None}


# Legacy API preserved for backward compatibility
def _execute_worker(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any], queue, timeout_ms: int, memory_mb: int):
    result = _execute_in_process(code, payload, variables_map, timeout_ms, memory_mb)
    queue.put(result)
