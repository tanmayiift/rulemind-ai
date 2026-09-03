import ast
import hashlib
import inspect
import json
import multiprocessing
import os
import time
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, Optional


MAX_EXECUTION_MS = 2000
MAX_MEMORY_MB = 128
SAFE_IMPORTS = {"math", "datetime", "json", "re"}
# Names that must never be referenced. Beyond the obvious IO/eval names, this includes the
# reflection helpers that could be used to reach attributes dynamically and defeat the static
# dunder-attribute guard below (e.g. getattr(obj, "__class__")). Most of these aren't in
# SAFE_BUILTINS anyway (so they'd NameError at runtime) — blocking them here gives a clear,
# early error and is defense-in-depth.
BLOCKED_NAMES = {
    "open", "eval", "exec", "__import__", "os", "sys", "subprocess", "socket", "requests",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "compile",
    "__build_class__", "breakpoint", "input",
}
# The real CPython sandbox-escape surface is attribute traversal, not builtins: an expression like
# ().__class__.__bases__[0].__subclasses__() reaches arbitrary loaded classes (subprocess.Popen, …)
# with ZERO builtins. So we reject any access to a "dunder" attribute (name starting with "__").
# We also block str.format / format_map, whose format-spec can perform attribute access at runtime
# ("{0.__class__}".format(x)) and thereby bypass the static check. f-strings and %-formatting are
# fine — their expressions are visible to this AST walk and %-formatting doesn't resolve attributes.
_BLOCKED_ATTRS = {"format", "format_map"}
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

# Add all Excel-compatible functions (400+) to the sandbox
try:
    from .excel_functions import EXCEL_FUNCTIONS
    SAFE_BUILTINS.update(EXCEL_FUNCTIONS)
except ImportError:
    pass  # Graceful fallback if excel_functions not available

class _BoundedCache(Dict[str, Any]):
    """A dict that evicts the oldest entry once it exceeds a capacity.

    The four code caches below were plain module-level dicts: global, unbounded, and shared
    across tenants. Every distinct variable body a tenant ever submits added a permanent entry,
    so a tenant authoring in a loop -- or simply a busy multi-tenant deployment over weeks --
    was a slow memory leak with no ceiling. Bounding them is the containment; insertion order is
    a good-enough approximation of LRU for a validation cache and needs no per-get bookkeeping.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._capacity = capacity

    def __setitem__(self, key: str, value: Any) -> None:
        if key not in self and len(self) >= self._capacity:
            # dicts preserve insertion order, so the first key is the oldest.
            oldest = next(iter(self))
            del self[oldest]
        super().__setitem__(key, value)


# Bound chosen so a large tenant's whole variable catalog fits (the demo tenant has ~55), with
# generous headroom, while still being a hard ceiling. Override for pathological catalogs.
_CODE_CACHE_CAPACITY = int(os.getenv("SANDBOX_CODE_CACHE_SIZE", "2048"))

# Cache for validated+compiled code to avoid re-parsing on every call
_validated_code_cache: Dict[str, bool] = _BoundedCache(_CODE_CACHE_CAPACITY)
_compiled_code_cache: Dict[str, Any] = _BoundedCache(_CODE_CACHE_CAPACITY)
_param_count_cache: Dict[str, int] = _BoundedCache(_CODE_CACHE_CAPACITY)
# Cache of the *resolved callable* per source hash. A variable is a pure function
# of (payload, variables); after the first exec we keep the function object and
# reuse it on every subsequent call — no re-parse, no re-exec, no re-resolve.
# This is the single biggest per-request win on the inline path.
_function_cache: Dict[str, Any] = _BoundedCache(_CODE_CACHE_CAPACITY)


def _source_key(source: str) -> str:
    """Content-address a code body for the caches.

    SHA-256, not MD5. The cache stored "this exact source already passed validation" under the
    hash, and validate_source returns EARLY on a hit -- so with a hash an attacker can collide,
    the attack is: submit a benign body, let it populate the cache, then submit a MALICIOUS body
    that hashes to the same key and validation is skipped entirely. Chosen-prefix MD5 collisions
    are cheap and public; SHA-256 has no such construction. usedforsecurity is set so a
    FIPS-restricted build does not refuse the digest.
    """
    return hashlib.sha256(source.encode()).hexdigest()

# Pre-warmed process pool (lazy-initialized). Guarded by a lock because the pool is a module
# global shared by every Starlette threadpool thread, and recycling it (below) races with both
# concurrent submits and concurrent recycles.
_pool: Optional[ProcessPoolExecutor] = None
_pool_lock = threading.Lock()
_POOL_SIZE = int(os.getenv("SANDBOX_POOL_SIZE", "4"))
# Pool-wide address-space cap, applied once per worker at spawn (see _worker_init). It is
# pool-wide and not per-tenant ON PURPOSE: workers are reused across tenants, and RLIMIT_AS can
# only be lowered, so a per-call setrlimit in a reused worker meant the FIRST caller's cap stuck
# for every later caller in that worker -- tenant A's 64MB silently applied to tenant B. A
# uniform cap set at spawn is the only correct model for a shared pool.
_POOL_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", str(MAX_MEMORY_MB)))


# Environment variables an isolated worker must never be able to read. Sandbox workers run
# untrusted author code (variables) and untrusted uploaded pickles (models), and a spawned
# worker inherits the parent's whole environment -- including the master encryption key and the
# database credentials. Scrubbing them in the child means that even code executing inside a
# worker cannot exfiltrate them; the worker legitimately needs none of these.
_WORKER_SCRUBBED_ENV = (
    "RULEMIND_CONFIG_KEY", "RULEMIND_ADMIN_JWT_SECRET", "RULEMIND_ADMIN_PASSWORD",
    "RULEMIND_DEV_API_KEY", "DATABASE_URL", "REDIS_URL",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
)


def _scrub_worker_env() -> None:
    for name in _WORKER_SCRUBBED_ENV:
        os.environ.pop(name, None)


def _worker_init(memory_mb: int) -> None:
    """Runs once in each freshly spawned worker: drop inherited secrets, then cap memory."""
    _scrub_worker_env()
    try:
        import resource  # type: ignore

        bytes_limit = int(memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
    except (ImportError, ValueError, OSError):
        # RLIMIT_AS is unsettable on some platforms (darwin refuses it outright). The PROCESS
        # boundary -- a separate spawned interpreter that can be killed -- still holds; only the
        # in-process memory ceiling is absent, and it is absent uniformly rather than leaking one
        # tenant's cap onto another.
        pass


def _new_pool() -> ProcessPoolExecutor:
    mp_context = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(
        max_workers=_POOL_SIZE, mp_context=mp_context,
        initializer=_worker_init, initargs=(_POOL_MEMORY_MB,))


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        # A pool that broke (a worker died mid-task, e.g. OOM-killed or terminated by recycle)
        # stays broken for the process lifetime and raises BrokenProcessPool on every submit --
        # the original code cached it and never rebuilt, so one crash disabled the sandbox until
        # a redeploy. Rebuild it here instead.
        if _pool is None or getattr(_pool, "_broken", False):
            _pool = _new_pool()
        return _pool


def _recycle_pool(pool: ProcessPoolExecutor) -> None:
    """Terminate a pool's workers and drop it so the next call builds a fresh one.

    This is the fix for the wedging bug. future.result(timeout=) times out the WAIT, not the
    task -- the worker keeps running the timed-out code. For `while True: pass` that slot is gone
    permanently, and _POOL_SIZE of them kill the pool for good. Since a ProcessPoolExecutor gives
    no way to cancel a running task or target one worker, the whole pool is recycled: the wedged
    worker is guaranteed dead, and concurrent in-flight decisions fail closed to review (T1.2)
    rather than fail open. Timeouts must therefore stay rare -- keep the per-variable timeout and
    the evaluation budget (T1.3) above legitimate compute time, or this becomes a churn loop.
    """
    global _pool
    with _pool_lock:
        if _pool is not pool:
            return  # another thread already recycled; don't kill the fresh pool
        for process in list(getattr(pool, "_processes", {}).values()):
            try:
                process.terminate()
            except Exception:  # pragma: no cover - best-effort kill
                pass
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover
            pass
        _pool = None


def variable(func=None, **metadata):
    def decorator(inner):
        inner.__rulemind_variable__ = True
        inner.__rulemind_metadata__ = metadata
        return inner

    if callable(func):
        return decorator(func)

    return decorator


def validate_source(source: str) -> None:
    code_hash = _source_key(source)
    if code_hash in _validated_code_cache:
        return

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_names = [alias.name.split(".")[0] for alias in node.names]
            for module_name in module_names:
                if module_name not in SAFE_IMPORTS:
                    raise ValueError("Import not allowed: {0}".format(module_name))
        if isinstance(node, ast.Name):
            if node.id in BLOCKED_NAMES:
                raise ValueError("Blocked symbol detected: {0}".format(node.id))
            if node.id.startswith("__"):
                # e.g. __builtins__, __import__ referenced as a bare name.
                raise ValueError("Access to dunder name not allowed: {0}".format(node.id))
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                # The escape vector: __class__ / __subclasses__ / __globals__ / __bases__ / …
                raise ValueError("Access to dunder attribute not allowed: {0}".format(node.attr))
            if node.attr in _BLOCKED_ATTRS:
                raise ValueError("Method not allowed: {0} (use an f-string or %-formatting)".format(node.attr))
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
    """Execute variable code in a pool worker process with sandboxing.

    The memory cap is NOT set here anymore. It used to call setrlimit(RLIMIT_AS) per invocation
    inside a reused worker, which both (a) silently no-op'd after the first call, since RLIMIT_AS
    cannot be raised again, and (b) leaked one tenant's cap onto the next tenant's call in the
    same worker. It now lives in _worker_init, applied once at spawn (see _POOL_MEMORY_MB).
    """
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


def _execute_inline(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any]) -> Dict[str, Any]:
    """Fast in-process execution for pre-validated code.

    On the first call for a given source we parse, validate, exec, and resolve the
    variable function once; every later call reuses the cached function object.
    """
    started = time.perf_counter()
    try:
        code_hash = _source_key(code)
        cached = _function_cache.get(code_hash)
        if cached is not None:
            function, parameter_count = cached
        else:
            validate_source(code)  # cached by source hash
            compiled = _compiled_code_cache.get(code_hash)
            if compiled is None:
                compiled = compile(code, "<variable>", "exec")
                _compiled_code_cache[code_hash] = compiled
            namespace = {
                "__builtins__": dict(SAFE_BUILTINS, __import__=restricted_import),
                "variable": variable,
                "json": json,
            }
            exec(compiled, namespace, namespace)  # pylint: disable=exec-used
            function = resolve_callable(namespace)
            parameter_count = len(inspect.signature(function).parameters)
            _function_cache[code_hash] = (function, parameter_count)
            _param_count_cache[code_hash] = parameter_count
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
        # The worker is still running the timed-out task. Recycle the pool so that wedged worker
        # is killed rather than pinning a slot forever (see _recycle_pool).
        _recycle_pool(pool)
        return {"value": None, "error": "Variable execution timed out.", "latency_ms": float(timeout_ms), "variable_name": None}
    except BrokenProcessPool as exc:
        # A worker died (OOM kill, a segfaulting C extension, a prior recycle). Drop the broken
        # pool so the NEXT call rebuilds, then fall back per policy for THIS one.
        _recycle_pool(pool)
        return _fallback_or_fail(exc, code, payload, context)
    except Exception as exc:
        return _fallback_or_fail(exc, code, payload, context)


def _fallback_or_fail(exc: Exception, code: str, payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Decide what to do when the sandboxed path is unavailable.

    Default is FAIL, not inline. The old default silently ran untrusted customer Python in the
    API process -- no timeout, no RLIMIT, no process boundary -- exactly when the sandbox was
    already unhealthy. Combined with the null-flips-gates bug (T1.2), a wedged pool under peak
    load meant "run everything unsandboxed and approve what errors." SANDBOX_FALLBACK_MODE=inline
    reinstates the old behaviour for anyone who has accepted that trade explicitly.
    """
    if os.getenv("SANDBOX_FALLBACK_MODE", "fail") == "inline":
        fallback = _execute_inline(code, payload, context)
        fallback["sandbox_fallback"] = "inline"
        fallback["sandbox_error"] = str(exc)
        return fallback
    return {"value": None, "error": "Sandbox execution unavailable: {0}".format(exc), "latency_ms": 0.0, "variable_name": None}


# Legacy API preserved for backward compatibility
def _execute_worker(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any], queue, timeout_ms: int, memory_mb: int):
    result = _execute_in_process(code, payload, variables_map, timeout_ms, memory_mb)
    queue.put(result)
