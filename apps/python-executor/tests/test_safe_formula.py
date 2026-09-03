"""The scorecard formula evaluator must not be a code-execution primitive (Tier 2.4).

It used to be `eval(formula, {"__builtins__": safe_ns}, safe_ns)`. A builtins dict is not a
sandbox -- `().__class__.__mro__[1].__subclasses__()` walks from any literal to every class in
the interpreter, and this product is multi-tenant with untrusted authors. It happened to be
unreachable only because _scorecard_to_dict drops the `formula` field on the way out, so one
added column or one new endpoint would have made it RCE in the API process holding
RULEMIND_CONFIG_KEY and every tenant's DB credentials.

These tests assert the two things that matter: the escapes are dead at the evaluator itself
(not merely unreachable), and every real formula shape still computes.
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

from app.excel_functions import EXCEL_FUNCTIONS  # noqa: E402
from app.logic import _evaluate_formula  # noqa: E402
from app.safe_formula import FormulaError, evaluate_formula  # noqa: E402


class NoCodeExecutionTest(unittest.TestCase):
    """Each of these evaluated in the old eval. Now each must be refused, not merely error."""

    ESCAPES = [
        "().__class__.__mro__[1].__subclasses__()",
        "().__class__.__bases__[0].__subclasses__()",
        "[c for c in ().__class__.__mro__[1].__subclasses__()]",
        "__import__('os').system('id')",
        "(lambda: 1)()",
        "().__class__",
        "globals()",
        "[].__class__.__base__.__subclasses__()",
    ]

    def test_every_known_escape_is_refused(self):
        for payload in self.ESCAPES:
            with self.subTest(payload=payload):
                with self.assertRaises(FormulaError,
                                       msg="the evaluator ran {0!r}".format(payload)):
                    evaluate_formula(payload, {}, EXCEL_FUNCTIONS)

    def test_attribute_access_is_impossible(self):
        """The subclasses walk begins with one attribute access. Refuse the whole node type."""
        with self.assertRaises(FormulaError):
            evaluate_formula("(1).__class__", {}, EXCEL_FUNCTIONS)

    def test_a_name_not_in_context_is_not_a_builtin(self):
        """__builtins__ is not reachable, so a bare builtin name is just unknown."""
        for name in ("open", "eval", "exec", "__import__", "getattr", "setattr", "compile"):
            with self.subTest(name=name):
                with self.assertRaises(FormulaError):
                    evaluate_formula(name, {}, EXCEL_FUNCTIONS)

    def test_calling_through_the_public_path_returns_an_error_dict(self):
        """The scorecard path expects {"error": ...}, never a raise and never a value."""
        result = _evaluate_formula("().__class__.__mro__[1].__subclasses__()", {})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_a_cpu_bomb_is_bounded(self):
        with self.assertRaises(FormulaError):
            evaluate_formula("10 ** 10 ** 10", {}, EXCEL_FUNCTIONS)

    def test_a_memory_bomb_is_bounded(self):
        with self.assertRaises(FormulaError):
            evaluate_formula("'a' * 999999999", {}, EXCEL_FUNCTIONS)


class RealFormulasStillWorkTest(unittest.TestCase):
    """The regression guard. Removing eval is only correct if scoring is unchanged."""

    def test_the_default_scorecard_formula(self):
        ctx = {"base_score": 300, "total_points": 42}
        self.assertEqual(evaluate_formula("base_score + total_points", ctx, EXCEL_FUNCTIONS), 342)

    def test_arithmetic_precedence(self):
        ctx = {"base_score": 300, "total_points": 20}
        self.assertEqual(evaluate_formula("base_score + total_points * 2", ctx, EXCEL_FUNCTIONS), 340)

    def test_the_excel_IF_metric(self):
        """IF(score >= 700, 0.15, 0.05) -- the exact metric shape in the test suite."""
        self.assertEqual(_evaluate_formula("IF(score >= 700, 0.15, 0.05)", {"score": 720}), 0.15)
        self.assertEqual(_evaluate_formula("IF(score >= 700, 0.15, 0.05)", {"score": 650}), 0.05)

    def test_python_ternary_and_comparisons(self):
        self.assertEqual(evaluate_formula("100 if score >= 700 else 0", {"score": 800}, EXCEL_FUNCTIONS), 100)

    def test_boolean_short_circuit_returns_the_value(self):
        self.assertEqual(evaluate_formula("a and b", {"a": 5, "b": 0}, EXCEL_FUNCTIONS), 0)
        self.assertEqual(evaluate_formula("a or b", {"a": 0, "b": 7}, EXCEL_FUNCTIONS), 7)

    def test_builtins_offered_as_functions(self):
        self.assertEqual(evaluate_formula("min(a, b)", {"a": 3, "b": 9},
                                          {**EXCEL_FUNCTIONS, "min": min}), 3)
        self.assertEqual(_evaluate_formula("round(a / b, 2)", {"a": 10, "b": 3}), 3.33)

    def test_nested_excel_functions(self):
        self.assertEqual(_evaluate_formula("MAX(MIN(x, 100), 0)", {"x": 150}), 100)

    def test_list_argument_to_an_aggregate(self):
        self.assertEqual(_evaluate_formula("SUM([1, 2, 3, x])", {"x": 4}), 10)

    def test_division_and_modulo(self):
        self.assertEqual(evaluate_formula("x % 7", {"x": 20}, EXCEL_FUNCTIONS), 6)
        self.assertEqual(evaluate_formula("x // 3", {"x": 20}, EXCEL_FUNCTIONS), 6)

    def test_a_reasonable_power_is_allowed(self):
        self.assertEqual(evaluate_formula("2 ** 10", {}, EXCEL_FUNCTIONS), 1024)

    def test_scorecard_formula_scoring_end_to_end(self):
        """Through evaluate_scorecard, the way production reaches it."""
        from app.logic import evaluate_scorecard

        scorecard = {
            "base_score": 300, "max_score": 900, "scoring_method": "formula",
            "formula": "base_score + total_points * 2",
            "factors": [],
        }
        result = evaluate_scorecard(scorecard, {}, {})
        self.assertEqual(result["scoring_method"], "formula")
        self.assertIsInstance(result["score"], int)

    def test_an_arithmetic_error_returns_an_error_not_a_crash(self):
        """logic._evaluate_formula documents that a bad formula returns {"error": ...} rather
        than raising into the scorecard path. Only _visit_Call honoured that -- _visit_BinOp
        called the operator unguarded, so an author writing `1 / 0` in a scorecard formula
        raised a raw ZeroDivisionError out of evaluate_scorecard and 500'd the decision.
        An author's arithmetic typo must not take down a request."""
        for formula in ("1 / 0", "1 % 0", "1 // 0"):
            with self.subTest(formula=formula):
                with self.assertRaises(FormulaError):
                    evaluate_formula(formula, {}, EXCEL_FUNCTIONS)

    def test_a_unary_operator_error_is_also_wrapped(self):
        with self.assertRaises(FormulaError):
            evaluate_formula("-x", {"x": "abc"}, EXCEL_FUNCTIONS)

    def test_the_scorecard_path_falls_back_instead_of_crashing(self):
        """End to end: the contract is about evaluate_scorecard, not just the evaluator."""
        from app.logic import evaluate_scorecard

        scorecard = {"base_score": 300, "max_score": 900, "scoring_method": "formula",
                     "formula": "1 / 0", "factors": []}
        self.assertEqual(evaluate_scorecard(scorecard, {}, {})["score"], 300)


if __name__ == "__main__":
    unittest.main()
