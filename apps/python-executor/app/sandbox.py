import ast
import inspect
import json
import multiprocessing
import time
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


def variable(func=None, **metadata):
    def decorator(inner):
        inner.__rulemind_variable__ = True
        inner.__rulemind_metadata__ = metadata
        return inner

    if callable(func):
        return decorator(func)

    return decorator


def validate_source(source: str) -> None:
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
        value for key, value in namespace.items() if callable(value) and not key.startswith("_")
    ]
    if not fallbacks:
        raise ValueError("No callable variable function found.")
    return fallbacks[0]


def _execute_worker(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any], queue, timeout_ms: int, memory_mb: int):
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
        queue.put({"value": result, "error": None, "latency_ms": latency_ms, "variable_name": function.__name__})
    except Exception as exc:  # pragma: no cover - returned through queue
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        queue.put({"value": None, "error": str(exc), "latency_ms": latency_ms, "variable_name": None})


def execute_variable(code: str, payload: Dict[str, Any], variables_map: Optional[Dict[str, Any]] = None, timeout_ms: int = MAX_EXECUTION_MS, memory_mb: int = MAX_MEMORY_MB) -> Dict[str, Any]:
    context = variables_map or {}
    mp_context = multiprocessing.get_context("spawn")
    queue = mp_context.Queue()
    process = mp_context.Process(
        target=_execute_worker,
        args=(code, payload, context, queue, timeout_ms, memory_mb),
    )
    process.start()
    process.join(max(float(timeout_ms) / 1000.0, 0.1))

    if process.is_alive():
        process.terminate()
        process.join()
        return {"value": None, "error": "Variable execution timed out.", "latency_ms": float(timeout_ms), "variable_name": None}

    if queue.empty():
        return {"value": None, "error": "Variable execution failed.", "latency_ms": 0.0, "variable_name": None}

    return queue.get()
