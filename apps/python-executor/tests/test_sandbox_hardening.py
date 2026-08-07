"""Variable sandbox hardening: the dunder-attribute escape surface is closed.

The classic CPython sandbox escape needs no builtins — attribute traversal alone
(().__class__.__bases__[0].__subclasses__()) reaches arbitrary loaded classes. validate_source
must reject dunder attribute/name access and the str.format format-spec bypass, while still allowing
ordinary variable code.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.sandbox import execute_variable, validate_source  # noqa: E402

LEGIT = (
    "@variable(source='bureau')\n"
    "def score(payload, variables):\n"
    "    return round(sum(a.get('bal', 0) for a in payload.get('accounts', [])) / 100.0, 2)\n"
)

ESCAPES = {
    "dunder_attr": "def f(p, v):\n    return ().__class__.__bases__[0].__subclasses__()\n",
    "dunder_name": "def f(p, v):\n    return __builtins__\n",
    "getattr_reflection": "def f(p, v):\n    return getattr((), '__class__')\n",
    "format_spec_bypass": "def f(p, v):\n    return '{0.__class__}'.format(())\n",
    "disallowed_import": "import os\ndef f(p, v):\n    return os.getpid()\n",
    "eval_call": "def f(p, v):\n    return eval('1+1')\n",
}


class SandboxHardeningTests(unittest.TestCase):
    def setUp(self):
        # Run these inline for speed — but confine the env change to this test and RESTORE it, so we
        # don't leak SANDBOX_MODE=inline into the shared process and break tests that exercise the
        # pool path (e.g. test_sdk_runtime's pool-startup-failure fallback).
        self._prev_mode = os.environ.get("SANDBOX_MODE")
        os.environ["SANDBOX_MODE"] = "inline"

    def tearDown(self):
        if self._prev_mode is None:
            os.environ.pop("SANDBOX_MODE", None)
        else:
            os.environ["SANDBOX_MODE"] = self._prev_mode

    def test_legit_variable_still_validates_and_runs(self):
        validate_source(LEGIT)  # must not raise
        out = execute_variable(LEGIT, {"accounts": [{"bal": 250}, {"bal": 250}]}, {})
        self.assertIsNone(out["error"])
        self.assertEqual(out["value"], 5.0)

    def test_all_escapes_rejected_at_validation(self):
        for name, src in ESCAPES.items():
            with self.subTest(escape=name):
                with self.assertRaises(ValueError, msg=f"{name} was not rejected"):
                    validate_source(src)

    def test_escape_returns_error_not_value_through_execute(self):
        # Even through the full execute path, an escape attempt yields an error result, never a value.
        for name, src in ESCAPES.items():
            with self.subTest(escape=name):
                out = execute_variable(src, {}, {})
                self.assertIsNone(out["value"], f"{name} produced a value")
                self.assertIsNotNone(out["error"], f"{name} did not error")

    def test_fstring_dunder_is_also_caught(self):
        # An f-string's expression is AST-visible, so a dunder inside it is still rejected.
        with self.assertRaises(ValueError):
            validate_source("def f(p, v):\n    return f'{().__class__}'\n")


if __name__ == "__main__":
    unittest.main()
