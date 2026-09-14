"""The sandbox refuses every known CPython escape — asserted, not assumed.

WHY THIS EXISTS AS A TEST AND NOT A ONE-OFF AUDIT. The variable sandbox runs UNTRUSTED customer
Python. Its defence is not "restricted builtins" — that is the part everyone gets right and it
stops nothing on its own, because `().__class__.__bases__[0].__subclasses__()` reaches
subprocess.Popen with ZERO builtins. The real defence is the static rejection of dunder
attribute traversal, plus blocking `str.format`/`format_map`, whose format-spec resolves
attributes at RUNTIME and so walks straight past a static name check.

Those are subtle, and subtle defences rot. A refactor of the AST walk that stops visiting
f-string expressions, or a "helpful" re-addition of `getattr` to SAFE_BUILTINS, reopens the
hole silently — nothing else in the suite would notice. So every technique below is a test.

Each payload is a real escape or resource-exhaustion technique. A payload that RETURNS A VALUE
instead of erroring is a sandbox escape and this suite fails.

Audited 2026-09-15: 20 techniques, all refused.
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

from app.sandbox import execute_variable  # noqa: E402


def variable(body, name="probe"):
    return '@variable(source="custom")\ndef {0}(payload, variables, apis):\n{1}\n'.format(
        name, "\n".join("    " + line for line in body.strip().split("\n")))

# (label, code body, why it matters)
ESCAPES = [
    ("subclasses via tuple",
     'return ().__class__.__bases__[0].__subclasses__()',
     "the canonical CPython escape: reaches subprocess.Popen with zero builtins"),

    ("subclasses via list literal",
     'return [].__class__.__mro__[1].__subclasses__()',
     "same reach, different traversal"),

    ("function __globals__",
     'f = lambda: 1\nreturn f.__globals__',
     "a function object exposes the module globals it closed over"),

    ("str.format attribute spec",
     'return "{0.__class__}".format(())',
     "format() resolves attributes at RUNTIME, defeating a static AST check"),

    ("format_map",
     'return "{a.__class__}".format_map({"a": ()})',
     "the same hole through a different method"),

    ("getattr indirection",
     'return getattr((), "__class__")',
     "builds the dunder name as data so a static name check cannot see it"),

    ("f-string attribute access",
     'x = ()\nreturn f"{x.__class__}"',
     "f-strings embed real expressions; if unwalked, the same escape"),

    ("__import__",
     'return __import__("os").listdir(".")',
     "direct import of the filesystem"),

    ("open a file",
     'return open("/etc/passwd").read()',
     "the simplest exfiltration primitive"),

    ("exec",
     'exec("import os")\nreturn 1',
     "arbitrary code from a string"),

    ("eval",
     'return eval("1+1")',
     "arbitrary expression from a string"),

    ("exception traceback walk",
     'try:\n    1/0\nexcept Exception as e:\n    return e.__traceback__.tb_frame.f_globals',
     "a caught exception carries a frame, and a frame carries globals"),

    ("decorator reaching type",
     'return type(())',
     "type() re-opens the class hierarchy"),

    ("comprehension scope leak",
     'return [x for x in ().__class__.__bases__]',
     "attribute access hidden inside a comprehension"),

    ("dynamic attribute via vars",
     'return vars(())',
     "vars() is __dict__ by another name"),

    ("import os directly",
     'import os\nreturn os.getcwd()',
     "the import statement itself"),

    ("socket",
     'import socket\nreturn socket.gethostname()',
     "network egress from inside a variable"),
]

RESOURCE = [
    ("infinite loop", 'while True:\n    pass', "CPU exhaustion — must be bounded by the timeout"),
    ("memory bomb", 'x = []\nwhile True:\n    x.append("a" * 10_000_000)',
     "RAM exhaustion — RLIMIT_AS on Linux; darwin REFUSES that rlimit"),
    ("ReDoS", 'import re\nreturn re.match(r"(a+)+$", "a" * 40 + "b")',
     "catastrophic backtracking — no rlimit covers CPU-in-C"),
]



class SandboxEscapeMatrixTest(unittest.TestCase):
    """Every known escape technique, refused."""

    def _attempt(self, body, budget_ms=2000):
        try:
            return execute_variable(variable(body), {}, {}, timeout_ms=budget_ms, memory_mb=128)
        except Exception as exc:  # the sandbox refusing by raising is also a pass
            return {"error": "{0}: {1}".format(type(exc).__name__, exc), "value": None}

    def test_no_known_escape_technique_succeeds(self):
        for label, body, why in ESCAPES:
            with self.subTest(technique=label):
                result = self._attempt(body)
                self.assertTrue(
                    result.get("error"),
                    "SANDBOX ESCAPE — {0} returned {1!r}.\n  Why it matters: {2}".format(
                        label, result.get("value"), why))

    def test_resource_exhaustion_is_bounded_by_the_timeout(self):
        """The timeout is the backstop that works EVERYWHERE.

        RLIMIT_AS bounds memory on Linux, but darwin refuses to set it (sandbox.py says so), so
        on a developer host the memory bomb is held only by the deadline. That makes the timeout
        load-bearing rather than a convenience, which is why it is asserted separately.
        """
        for label, body, why in RESOURCE:
            with self.subTest(technique=label):
                result = self._attempt(body, budget_ms=1500)
                self.assertTrue(
                    result.get("error"),
                    "UNBOUNDED — {0} completed instead of being stopped.\n  {1}".format(label, why))

    def test_the_matrix_itself_is_not_empty(self):
        """A guard on the guard: an import error or a bad edit that empties these lists would
        make every assertion above vacuously true."""
        self.assertGreaterEqual(len(ESCAPES), 15)
        self.assertGreaterEqual(len(RESOURCE), 3)

    def test_a_benign_variable_still_runs(self):
        """The sandbox must refuse attacks WITHOUT refusing ordinary code — a sandbox that
        blocks everything passes every test above and ships nothing."""
        result = self._attempt('return sum([1, 2, 3]) + len("abcd")')
        self.assertIsNone(result.get("error"), result.get("error"))
        self.assertEqual(result.get("value"), 10)


if __name__ == "__main__":
    unittest.main()
