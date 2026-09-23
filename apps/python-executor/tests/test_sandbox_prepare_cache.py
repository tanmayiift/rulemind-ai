"""The pool worker must not re-compile a variable's source on every call — and must not share
one tenant's module state with another to avoid it.

`_execute_in_process` re-ran `validate_source`, `exec(code, ...)` and `inspect.signature` on
EVERY invocation — 55 times per decision on the seeded tenant — while `_execute_inline`, one
function below, cached exactly that work. `SANDBOX_MODE=pool` is the default, so production
traffic took the uncached path and the cached path was the one nobody ran.

Measured in the API image, one seeded variable, 2000 iterations: preparation 33.8 µs against
0.3 µs for the variable's own body. **The preparation was 113× the computation.**

The fix has a trap in it, and these tests are mostly about the trap. Caching the resolved
FUNCTION (what `_execute_inline` does) also caches its `__globals__`. Two tenants whose
variable source is byte-identical hash to the same cache entry, so any module-level state the
code mutates becomes a cross-tenant channel — a data leak introduced by a performance fix, in
the default execution mode. So only the CODE OBJECT is cached: immutable, no state, safe to
share. `test_module_level_state_does_not_leak_between_calls` is the assertion that keeps it
that way.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import sandbox  # noqa: E402

SOURCE = (
    "@variable(source=\"custom\")\n"
    "def ratio(payload, variables, apis):\n"
    "    return payload.get(\"a\", 0) + payload.get(\"b\", 0)\n"
)

# Module-level mutable state. If the resolved function (and therefore its __globals__) were
# cached, this list would accumulate across calls -- and across TENANTS, because the cache key
# is the source hash and two tenants can submit identical source.
STATEFUL = (
    "SEEN = []\n"
    "@variable(source=\"custom\")\n"
    "def counter(payload, variables, apis):\n"
    "    SEEN.append(payload.get(\"n\"))\n"
    "    return len(SEEN)\n"
)


class PreparedCacheTests(unittest.TestCase):
    def setUp(self):
        sandbox._prepared_cache.clear()

    def test_the_variable_still_computes_the_right_answer(self):
        result = sandbox._execute_in_process(SOURCE, {"a": 2, "b": 3}, {}, 2000, 128)
        self.assertIsNone(result["error"])
        self.assertEqual(result["value"], 5)

    def test_the_source_is_compiled_once_and_reused(self):
        """Otherwise this change is a comment. Counts real `compile` calls across 5 executions
        of the same source: uncached that is 5, cached it is 1."""
        import builtins

        compiles = []
        original = builtins.compile

        def counting(source, filename, mode, *args, **kwargs):
            if filename == "<variable>":
                compiles.append(filename)
            return original(source, filename, mode, *args, **kwargs)

        builtins.compile = counting
        try:
            for _ in range(5):
                sandbox._execute_in_process(SOURCE, {"a": 1, "b": 1}, {}, 2000, 128)
        finally:
            builtins.compile = original
        self.assertEqual(len(compiles), 1, f"compiled {len(compiles)} times, expected 1")

    def test_module_level_state_does_not_leak_between_calls(self):
        """THE test. Caching the resolved function would cache its `__globals__` too, so `SEEN`
        would accumulate -- and because the key is a source hash, it would accumulate across
        TENANTS with identical source. Every call must start from a fresh namespace, so the
        answer is 1 every time."""
        for call in range(4):
            result = sandbox._execute_in_process(STATEFUL, {"n": call}, {}, 2000, 128)
            self.assertIsNone(result["error"])
            self.assertEqual(result["value"], 1,
                             "module-level state survived into the next call -- one tenant's "
                             "data is reachable from another's variable with identical source")

    def test_different_sources_get_different_entries(self):
        first = sandbox._execute_in_process(SOURCE, {"a": 1, "b": 1}, {}, 2000, 128)
        other = SOURCE.replace("+ payload.get(\"b\", 0)", "* 10")
        second = sandbox._execute_in_process(other, {"a": 4}, {}, 2000, 128)
        self.assertEqual(first["value"], 2)
        self.assertEqual(second["value"], 40)

    def test_invalid_source_is_still_rejected_and_not_cached(self):
        """Validation runs on the miss. A rejected source must not land in the cache, or the
        second attempt would find an entry and skip the check."""
        bad = "import os\n@variable(source=\"custom\")\ndef x(p, v, a):\n    return os.getcwd()\n"
        for _ in range(2):
            result = sandbox._execute_in_process(bad, {}, {}, 2000, 128)
            self.assertIsNotNone(result["error"])
        self.assertNotIn(sandbox._source_key(bad), sandbox._prepared_cache)

    def test_a_two_parameter_variable_still_works(self):
        """The parameter count is cached alongside the code object, so the 2-arg and 3-arg
        calling conventions must both survive the cache."""
        two = ("@variable(source=\"custom\")\n"
               "def two_args(payload, variables):\n"
               "    return payload.get(\"a\", 0) * 2\n")
        result = sandbox._execute_in_process(two, {"a": 21}, {}, 2000, 128)
        self.assertIsNone(result["error"])
        self.assertEqual(result["value"], 42)


if __name__ == "__main__":
    unittest.main()
