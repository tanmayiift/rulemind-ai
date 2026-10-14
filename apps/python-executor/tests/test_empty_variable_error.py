"""A variable that fails with an EMPTY message is still a failed variable.

`str(MemoryError())` is "". The sandbox reported a variable's error as `str(exc)`, and the
runner recorded an error only when that string was truthy -- so a variable that exhausted its
memory limit came back as a successful None, and the decision went on to compare None against
its gates instead of failing closed to review. It only happens where RLIMIT_AS actually fires
(Linux; darwin refuses it), which is why every developer run passed and the first Linux CI run
failed `test_resource_exhaustion_is_bounded_by_the_timeout` (memory bomb).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import sandbox, variable_runner  # noqa: E402

CODE = '@variable(source="custom")\ndef probe(payload, variables):\n    return compute()\n'


class EmptyErrorTests(unittest.TestCase):
    def test_an_exception_with_no_message_reports_its_type(self):
        self.assertEqual(sandbox._error_text(MemoryError()), "MemoryError")
        self.assertEqual(sandbox._error_text(ValueError("bad")), "bad")

    def test_the_in_process_path_never_returns_an_empty_error(self):
        """Raise a message-less MemoryError from inside the variable, as RLIMIT_AS does."""
        def boom(*_args):
            raise MemoryError()

        with mock.patch.object(sandbox, "resolve_callable", return_value=boom):
            for run in (lambda: sandbox._execute_in_process(CODE, {}, {}, 1000, 128),
                        lambda: sandbox._execute_inline(CODE + "# inline\n", {}, {})):
                result = run()
                self.assertIsNone(result["value"])
                self.assertEqual(result["error"], "MemoryError")

    def test_the_runner_treats_an_empty_error_as_a_failure(self):
        """Defence in depth: whatever produced it, error="" must not read as success."""
        values, errors = variable_runner.compute_variables(
            [{"id": "v", "code": CODE, "source_id": "custom"}], {"custom": {}},
            execute=lambda *a, **k: {"value": None, "error": "", "latency_ms": 1.0},
            timeout_ms=1000, memory_mb=128, check_budget=lambda stage: None,
            remaining_ms=lambda: None)
        self.assertEqual(values, {"v": None})
        self.assertIn("v", errors, "an empty error string was read as success")
        self.assertTrue(errors["v"])

    def test_a_successful_variable_records_no_error(self):
        values, errors = variable_runner.compute_variables(
            [{"id": "v", "code": CODE, "source_id": "custom"}], {"custom": {}},
            execute=lambda *a, **k: {"value": 7, "error": None, "latency_ms": 1.0},
            timeout_ms=1000, memory_mb=128, check_budget=lambda stage: None,
            remaining_ms=lambda: None)
        self.assertEqual((values, errors), ({"v": 7}, {}))


if __name__ == "__main__":
    unittest.main()
