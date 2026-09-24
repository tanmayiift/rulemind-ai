import ast
import hashlib
import inspect
import json
import multiprocessing
import os
import pickle
import signal
import time
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import env


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
_CODE_CACHE_CAPACITY = env.env_int("SANDBOX_CODE_CACHE_SIZE", 2048)

# Cache for validated+compiled code to avoid re-parsing on every call
_validated_code_cache: Dict[str, bool] = _BoundedCache(_CODE_CACHE_CAPACITY)
_compiled_code_cache: Dict[str, Any] = _BoundedCache(_CODE_CACHE_CAPACITY)
_param_count_cache: Dict[str, int] = _BoundedCache(_CODE_CACHE_CAPACITY)
# Cache of the *resolved callable* per source hash. A variable is a pure function
# of (payload, variables); after the first exec we keep the function object and
# reuse it on every subsequent call — no re-parse, no re-exec, no re-resolve.
# This is the single biggest per-request win on the inline path.
_function_cache: Dict[str, Any] = _BoundedCache(_CODE_CACHE_CAPACITY)

# (code object, parameter count) by source hash, for the POOL worker path. Separate from
# _function_cache on purpose: that one caches a resolved function, and therefore its
# `__globals__`, which two tenants with byte-identical source would then share. This one holds
# only immutable code objects and an int, so it is safe to share across tenants in a worker
# that serves all of them. See the note in _execute_in_process.
_prepared_cache: Dict[str, Any] = _BoundedCache(_CODE_CACHE_CAPACITY)


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
_POOL_SIZE = env.env_int("SANDBOX_POOL_SIZE", 4)
# Pool-wide address-space cap, applied once per worker at spawn (see _worker_init). It is
# pool-wide and not per-tenant ON PURPOSE: workers are reused across tenants, and RLIMIT_AS can
# only be lowered, so a per-call setrlimit in a reused worker meant the FIRST caller's cap stuck
# for every later caller in that worker -- tenant A's 64MB silently applied to tenant B. A
# uniform cap set at spawn is the only correct model for a shared pool.
_POOL_MEMORY_MB = env.env_int("SANDBOX_MEMORY_MB", MAX_MEMORY_MB)


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


def probe() -> Dict[str, Any]:
    """Can this runtime actually execute a sandboxed variable? {"status", "mode", "detail"}.

    The readiness probe checked the database and Redis and nothing else, so on a runtime that
    forbids `spawn` -- some serverless and hardened container platforms do -- the process
    reported READY while every Python variable failed. The decisions still come back, because
    T1.2 makes a failed variable fail closed rather than fail open, but they are decided without
    the variables the policy is written around, and nothing upstream says so. A load balancer
    routes traffic to it, and the deploy looks successful.

    Runs one trivial expression through the real path, so it answers the question that matters:
    not "is the pool object present" but "did a worker start and return a value".
    """
    mode = "inline" if env.env_str("SANDBOX_MODE", "pool") == "inline" else "pool"
    try:
        result = execute_variable(
            "def rulemind_readiness_probe(payload, variables):\n    return 1\n",
            {}, {}, timeout_ms=5000)
    except Exception as exc:  # noqa: BLE001 - a probe must report, never raise
        return {"status": "error", "mode": mode, "detail": str(exc)}
    if result.get("error"):
        return {"status": "error", "mode": mode, "detail": str(result["error"])}
    if result.get("value") != 1:
        return {"status": "error", "mode": mode,
                "detail": "sandbox returned {0!r}, expected 1".format(result.get("value"))}
    detail = ""
    if result.get("sandbox_fallback"):
        # SANDBOX_FALLBACK_MODE=inline: it worked, but NOT in a subprocess. Untrusted customer
        # Python is running in the API process with no RLIMIT and no process boundary, which is
        # a materially different security posture and must not read as plain "ok".
        return {"status": "degraded", "mode": "inline_fallback",
                "detail": "the pool is unavailable and SANDBOX_FALLBACK_MODE=inline is running "
                          "untrusted code in the API process: {0}".format(result.get("sandbox_error"))}
    return {"status": "ok", "mode": mode, "detail": detail}


def shutdown_pool() -> None:
    """Terminate the sandbox's worker processes. Wired into FastAPI's shutdown event.

    WHY THIS IS NEEDED AT ALL. Unlike decision_log's THREAD pool, this one spawns child
    PROCESSES, and a child process outlives its parent unless something reaps it.
    ProcessPoolExecutor registers its own atexit handler, but that only runs on a clean
    interpreter exit -- not when the parent is SIGKILLed, and not when a dev server or a test
    run is interrupted.

    The symptom, measured 2026-09-14: **159 orphaned sandbox workers** on this machine, dating
    back three weeks, all reparented to launchd. Mostly harmless at 32KB each -- but one of them
    had been spinning at 99% CPU since 2026-08-29, which is a full core burned continuously, and
    it corrupted a performance baseline badly enough to look like an 83% regression.

    IN A CONTAINER THIS CANNOT HAPPEN and that is worth stating, so nobody reads this as a
    production leak: the PID namespace reaps every child when the container dies, whatever the
    signal. This matters on a developer's host, where there is no such boundary.

    Best-effort by design: a shutdown path that raises is worse than one that leaks.
    """
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is None:
        return
    for process in list(getattr(pool, "_processes", {}).values()):
        try:
            process.terminate()
        except Exception:  # pragma: no cover - best-effort kill
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # pragma: no cover
        pass


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
        # COMPILE ONCE PER SOURCE, EXEC EVERY CALL. This used to re-validate, re-compile and
        # re-`inspect.signature` on every invocation -- 55 times per decision on the seeded
        # tenant -- while `_execute_inline` one function below cached exactly this. Production
        # traffic takes THIS path (SANDBOX_MODE=pool is the default); the inline path is the
        # fallback. So the cached path was the one nobody ran.
        #
        # Measured in this image, one seeded variable, 2000 iterations: preparation 33.8 us,
        # the variable's own body 0.3 us -- the preparation was 113x the computation. With
        # compile and validation cached it is 1.5 us, so ~32 us x 55 variables is ~1.8 ms off
        # a decision. Worth having and not the headline: the sandbox's real cost is the 55 IPC
        # round trips, which is plan item 4.4.
        #
        # THE NAMESPACE IS STILL FRESH EVERY CALL, deliberately. Caching the resolved FUNCTION
        # -- which `_execute_inline` does -- also caches its `__globals__`, so two tenants whose
        # variable source is byte-identical hash to the same entry and would share any
        # module-level state the code mutates. A code object has no such state: it is
        # immutable and safe to share. This is a 6.7 us saving that cannot leak; the extra
        # 1.5 us is the price of not introducing a cross-tenant channel for it.
        code_hash = _source_key(code)
        cached = _prepared_cache.get(code_hash)
        if cached is None:
            validate_source(code)
            compiled = compile(code, "<variable>", "exec")
            probe_namespace = {
                "__builtins__": dict(SAFE_BUILTINS, __import__=restricted_import),
                "variable": variable,
                "json": json,
            }
            exec(compiled, probe_namespace, probe_namespace)  # pylint: disable=exec-used
            parameter_count = len(inspect.signature(resolve_callable(probe_namespace)).parameters)
            _prepared_cache[code_hash] = (compiled, parameter_count)
        else:
            compiled, parameter_count = cached
        namespace = {
            "__builtins__": dict(SAFE_BUILTINS, __import__=restricted_import),
            "variable": variable,
            "json": json,
        }
        exec(compiled, namespace, namespace)  # pylint: disable=exec-used
        function = resolve_callable(namespace)
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


# ── One round trip for a decision's variables (plan 4.4) ──────────────────────────────────
#
# A decision computes up to ~55 variables, and the per-call path above makes one pool round trip
# for each: measured at 6.97 ms of a 15.5 ms decision, of which the variables' own work is under
# 20 us. R10.14 measured the two cheaper ideas and reverted both (trimming the pickled map saves
# 0.006 ms a trip; fanning calls out concurrently is 15% at best, because ProcessPoolExecutor
# funnels every submit through one queue). What is left is to make ONE trip and run the group
# inside the worker -- which has to keep, per variable, every guarantee the per-call path gets
# for free from its process boundary:
#
#   order and accumulation  each variable sees the values of the ones before it, as before.
#   isolation               each gets its OWN copy of its source payload and of every earlier
#                           mutable value (pickled once, unpickled per variable, 0.13 ms for 55),
#                           because the per-call path's pickling gave it that: a variable that
#                           appends to a list it was handed must not change what the next sees.
#   its own deadline        an interval timer raises inside the variable at min(timeout, budget
#                           left), and a result that arrives late is a timeout anyway -- variable
#                           code may catch exceptions, including this one.
#   error isolation         one variable raising, or returning something that cannot be pickled,
#                           errors that variable only.
#   the evaluation budget   the worker stops starting variables when the budget is spent and
#                           marks the rest `abandoned`; the caller then raises exactly as before.
#   fail closed             a broken pool is recycled and falls back (or fails) per the configured
#                           policy for the whole group; a worker that ignores its timers is killed
#                           at the group's own deadline and every variable in it errors -- which
#                           sends the decision to review, as one failed variable already does.

_IMMUTABLE = (type(None), bool, int, float, complex, str, bytes)
_GROUP_GRACE_S = 0.5
TIMED_OUT = "Variable execution timed out."


class _VariableTimedOut(BaseException):
    """Not an Exception: `_execute_in_process` catches Exception, and a timeout must get past it."""


def batching_enabled() -> bool:
    """One trip per decision in pool mode. SANDBOX_BATCH=0 restores one trip per variable."""
    return os.getenv("SANDBOX_MODE", "pool") != "inline" and env.env_bool("SANDBOX_BATCH", True)


def _timed_out(limit_ms: float) -> Dict[str, Any]:
    return {"value": None, "error": TIMED_OUT, "latency_ms": float(limit_ms), "variable_name": None}


def _execute_group_in_process(items: Sequence[Tuple[str, str, Optional[str]]], sources: Dict[Any, Any],
                              timeout_ms: int, memory_mb: int, budget_ms: Optional[float]) -> List[Dict[str, Any]]:
    """Runs in a pool worker. `items` is (variable_id, code, source_id), in evaluation order."""
    started = time.perf_counter()
    blobs = {source_id: pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
             for source_id, payload in sources.items()}
    values: Dict[str, Any] = {}
    mutable: Dict[str, bytes] = {}
    results: List[Dict[str, Any]] = []
    armed = {"on": False}

    def _alarm(_signum: int, _frame: Any) -> None:
        if armed["on"]:
            raise _VariableTimedOut()

    use_timer = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGALRM, _alarm) if use_timer else None
    try:
        for index, (variable_id, code, source_id) in enumerate(items):
            left = None if budget_ms is None else budget_ms - (time.perf_counter() - started) * 1000.0
            if left is not None and left <= 0:
                results.extend({"abandoned": True} for _ in items[index:])
                break
            limit = timeout_ms if left is None else max(1.0, min(float(timeout_ms), left))
            payload = pickle.loads(blobs[source_id]) if source_id in blobs else {}
            view = dict(values)
            for key, blob in mutable.items():
                view[key] = pickle.loads(blob)
            variable_started = time.perf_counter()
            # The except sits OUTSIDE the finally on purpose: an alarm landing inside the finally,
            # before `armed` is cleared, raises there -- and with the except inside, it would
            # escape the loop and reach the caller as an uncaught BaseException.
            try:
                try:
                    armed["on"] = True
                    if use_timer:
                        # Re-fires every 50 ms after the first expiry, so one caught timeout is
                        # not the end of it.
                        signal.setitimer(signal.ITIMER_REAL, limit / 1000.0, 0.05)
                    result = _execute_in_process(code, payload, view, int(limit), memory_mb)
                finally:
                    armed["on"] = False
                    if use_timer:
                        signal.setitimer(signal.ITIMER_REAL, 0)
            except _VariableTimedOut:
                armed["on"] = False
                if use_timer:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                result = _timed_out(limit)
            if (time.perf_counter() - variable_started) * 1000.0 > limit:
                result = _timed_out(limit)
            value = result.get("value")
            try:
                blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as exc:  # noqa: BLE001 - one unpicklable result errors one variable
                result = {"value": None, "error": "Variable returned a value that cannot be serialised: {0}".format(exc),
                          "latency_ms": result.get("latency_ms", 0.0), "variable_name": result.get("variable_name")}
                value, blob = None, None
            if blob is not None and not isinstance(value, _IMMUTABLE):
                mutable[variable_id] = blob
            values[variable_id] = value
            results.append(result)
    finally:
        if use_timer:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return results


def _group_fallback_or_fail(exc: Exception, items: Sequence[Tuple[str, str, Optional[str]]],
                            sources: Dict[Any, Any]) -> List[Dict[str, Any]]:
    """`_fallback_or_fail` for a whole group, keeping accumulation when it falls back inline."""
    values: Dict[str, Any] = {}
    results = []
    for variable_id, code, source_id in items:
        result = _fallback_or_fail(exc, code, sources.get(source_id, {}), dict(values))
        values[variable_id] = result.get("value")
        results.append(result)
    return results


def execute_variable_group(items: Sequence[Tuple[str, str, Optional[str]]], sources: Dict[Any, Any],
                           timeout_ms: int = MAX_EXECUTION_MS, memory_mb: int = MAX_MEMORY_MB,
                           budget_ms: Optional[float] = None) -> List[Dict[str, Any]]:
    """Evaluate a decision's variables in ONE pool round trip. Returns one result per item, in
    order, each shaped like `execute_variable`'s; items the budget did not reach are
    `{"abandoned": True}`."""
    if not items:
        return []
    wait_ms = budget_ms if budget_ms is not None else float(timeout_ms) * len(items)
    pool = None
    try:
        pool = _get_pool()
        future = pool.submit(_execute_group_in_process, list(items), dict(sources), timeout_ms, memory_mb, budget_ms)
        return future.result(timeout=max(wait_ms / 1000.0, 0.1) + _GROUP_GRACE_S)
    except (FuturesTimeoutError, _VariableTimedOut):
        # The worker ignored its timers (variable code may catch them), or -- which the loop is
        # built to prevent -- a timeout escaped it. Kill the worker with the pool.
        if pool is not None:
            _recycle_pool(pool)
        return [_timed_out(timeout_ms) for _ in items]
    except BrokenProcessPool as exc:
        if pool is not None:
            _recycle_pool(pool)
        return _group_fallback_or_fail(exc, items, sources)
    except Exception as exc:  # noqa: BLE001 - the same stance as execute_variable
        return _group_fallback_or_fail(exc, items, sources)


# Legacy API preserved for backward compatibility
def _execute_worker(code: str, payload: Dict[str, Any], variables_map: Dict[str, Any], queue, timeout_ms: int, memory_mb: int):
    result = _execute_in_process(code, payload, variables_map, timeout_ms, memory_mb)
    queue.put(result)
