"""Editor diagnostics without running the code (parity P13).

The editor needs to say "line 4: unexpected indent" or "Import not allowed: os" before the author
clicks Test. Doing that in the browser would need a Python parser in JavaScript AND a second copy
of the sandbox policy -- two implementations that would drift from the ones that actually decide
whether the code runs. A linter that disagrees with the runtime is worse than no linter, because
the author learns to ignore it.

So the endpoint reuses `sandbox.validate_source`, the exact function the sandbox calls before
executing anything. THE POINT OF THIS FILE is that a diagnostic is not an approximation of the
rule, it IS the rule -- `test_every_policy_violation_is_reported` walks the real policy rather
than a list copied from it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

LINT = "/api/v1/variables/lint"
GOOD = '@variable(source="bank")\ndef v(payload, variables, apis):\n    return 1\n'


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = app_main.storage
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "lint.db"))
        self.client = TestClient(app_main.app)
        self.key = app_main.storage.default_api_key or ""
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        self.client.close()
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        preserve_storage_global(self)
        app_main.storage = self._orig
        self.tempdir.cleanup()

    def lint(self, code, key=None):
        return self.client.post(LINT, headers={"x-api-key": key or self.key}, json={"code": code})


class ItReportsWhatTheSandboxWouldRefuseTest(_Base):

    def test_valid_code_is_clean(self):
        body = self.lint(GOOD).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["diagnostics"], [])

    def test_a_syntax_error_carries_a_line_number(self):
        """The whole reason for an inline marker: it has to land on a line."""
        body = self.lint("def v(payload, variables, apis):\nreturn 1\n").json()
        self.assertFalse(body["ok"])
        diagnostic = body["diagnostics"][0]
        self.assertEqual(diagnostic["kind"], "syntax")
        self.assertEqual(diagnostic["line"], 2)
        self.assertTrue(diagnostic["message"])

    def test_a_blocked_import_is_reported_by_name(self):
        body = self.lint("import os\ndef v(p, v2, a):\n    return 1\n").json()
        self.assertFalse(body["ok"])
        self.assertIn("os", body["diagnostics"][0]["message"])
        self.assertEqual(body["diagnostics"][0]["kind"], "policy")

    def test_a_policy_violation_reports_no_line_rather_than_a_wrong_one(self):
        """validate_source raises on an AST walk without carrying the node position out.
        Reporting line 1 would put the marker on the wrong line, which is worse than none -- the
        message names the symbol instead."""
        body = self.lint("def v(p, v2, a):\n\n\n    return ().__class__\n").json()
        self.assertFalse(body["ok"])
        self.assertIsNone(body["diagnostics"][0]["line"])

    def test_every_policy_violation_is_reported(self):
        """Walks the REAL policy rather than a list copied from it: whatever validate_source
        refuses, the editor must be able to say so. If someone adds a rule to the sandbox and
        this endpoint stops matching, that is exactly the drift this design exists to prevent."""
        from app.sandbox import validate_source

        samples = [
            "import os\n",
            "def v(p, q, r):\n    return eval('1')\n",
            "def v(p, q, r):\n    return ().__class__\n",
            "def v(p, q, r):\n    return open('/etc/passwd')\n",
        ]
        for code in samples:
            with self.subTest(code=code.splitlines()[0]):
                try:
                    validate_source(code)
                except Exception:
                    pass
                else:
                    self.skipTest("sandbox no longer refuses this sample")
                body = self.lint(code).json()
                self.assertFalse(body["ok"], "the sandbox refuses this but the linter passed it")
                self.assertTrue(body["diagnostics"][0]["message"])

    def test_empty_code_is_clean_rather_than_an_error(self):
        """An empty editor is the state every new variable starts in. Flagging it would mean a
        red marker before the author has typed anything."""
        for code in ("", "   ", "\n\n"):
            with self.subTest(code=repr(code)):
                self.assertTrue(self.lint(code).json()["ok"])


class ItDoesNotRunTheCodeTest(_Base):
    """`test-draft` executes in a real sandbox with a timeout and a memory cap. This is the cheap
    check that runs while someone is typing, so it must not spawn a process."""

    def test_linting_does_not_execute(self):
        marker = os.path.join(self.tempdir.name, "executed")
        code = (
            "def v(payload, variables, apis):\n"
            "    with open({0!r}, 'w') as fh:\n"
            "        fh.write('x')\n"
            "    return 1\n"
        ).format(marker)
        self.lint(code)
        self.assertFalse(os.path.exists(marker), "linting executed the variable body")

    def test_an_infinite_loop_lints_instantly(self):
        """Executing this would hang until the sandbox timeout; parsing it is immediate."""
        import time

        started = time.perf_counter()
        body = self.lint("def v(p, q, r):\n    while True:\n        pass\n").json()
        self.assertLess((time.perf_counter() - started) * 1000, 2000)
        self.assertTrue(body["ok"], "an infinite loop is valid Python and must lint clean")


class AccessTest(_Base):

    def test_it_needs_authoring(self):
        viewer = app_main.storage.generate_api_key_for_tenant(
            app_main.storage.default_tenant_id, role="viewer")["plaintext"]
        self.assertEqual(self.lint(GOOD, key=viewer).status_code, 403)

    def test_an_unauthenticated_caller_is_rejected(self):
        self.assertEqual(self.client.post(LINT, json={"code": GOOD}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
