"""A whitelisting AST evaluator for scorecard/metric formulas.

This replaces a bare `eval()` (logic.py) that ran author-supplied formula strings with only a
builtins dict for a guard. That guard is not a boundary: `().__class__.__mro__[1].__subclasses__()`
walks from any literal to every class in the interpreter, and from there to os/subprocess -- a
classic Python sandbox escape, and this repo is multi-tenant with untrusted authors. It happened
to be unreachable only because `_scorecard_to_dict` drops the `formula` field on the way out, so
one added column or one new endpoint would have turned it into remote code execution in the API
process that holds RULEMIND_CONFIG_KEY and every tenant's DB credentials.

Rather than harden a denylist (which the escape above defeats by construction), this parses the
formula to an AST and evaluates ONLY an allowlisted node set: arithmetic, comparison, boolean and
conditional expressions over names from the caller's context and calls to a fixed function table.
Anything else -- attribute access, subscripting a call result, comprehensions, lambdas, walrus,
starred args -- is refused before evaluation. There is no `eval`, no `__builtins__`, and no way to
reach an attribute, so the subclasses walk has nothing to stand on.
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Callable, Dict, Mapping

__all__ = ["evaluate_formula", "FormulaError"]


class FormulaError(Exception):
    """A formula was syntactically invalid, used a disallowed construct, or failed at runtime."""


# Binary and unary operators, mapped explicitly so an operator we did not vet cannot slip in via a
# new ast node type. `**` is intentionally supported but its exponent is bounded at eval time --
# `10**10**10` is a trivial CPU/memory bomb otherwise.
_BIN_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: Dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_,
}
_COMPARE_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}

# Guardrails on the two ways a pure-arithmetic formula can still burn the box.
_MAX_POW_EXPONENT = 1000
_MAX_STRING_MULTIPLY = 100_000


def evaluate_formula(formula: str, context: Mapping[str, Any], functions: Mapping[str, Callable[..., Any]]) -> Any:
    """Evaluate `formula` over `context` (names) and `functions` (callables). Raises FormulaError.

    `functions` is the caller's allowlist -- typically the Excel function table. Names are resolved
    from `context` first, then treated as a zero-arg reference to a function only if used in a call.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError("invalid formula syntax: {0}".format(exc)) from exc
    return _Evaluator(context, functions).visit(tree.body)


class _Evaluator:
    def __init__(self, context: Mapping[str, Any], functions: Mapping[str, Callable[..., Any]]) -> None:
        self._context = context
        self._functions = functions

    def visit(self, node: ast.AST) -> Any:
        handler = getattr(self, "_visit_" + type(node).__name__, None)
        if handler is None:
            # The default is refusal, not best-effort. Every allowed construct is opted in by
            # name below, so a node type nobody vetted -- Attribute, Lambda, Comprehension,
            # Subscript-of-a-call, the walrus -- lands here and is rejected before it runs.
            raise FormulaError("disallowed expression: {0}".format(type(node).__name__))
        return handler(node)

    def _visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float, bool, str)) or node.value is None:
            return node.value
        raise FormulaError("disallowed constant")

    def _visit_Name(self, node: ast.Name) -> Any:
        if node.id in self._context:
            return self._context[node.id]
        raise FormulaError("unknown name: {0}".format(node.id))

    def _visit_BinOp(self, node: ast.BinOp) -> Any:
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise FormulaError("disallowed operator: {0}".format(type(node.op).__name__))
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and right > _MAX_POW_EXPONENT:
            raise FormulaError("exponent too large")
        if isinstance(node.op, ast.Mult) and (
            (isinstance(left, str) and isinstance(right, int) and right > _MAX_STRING_MULTIPLY)
            or (isinstance(right, str) and isinstance(left, int) and left > _MAX_STRING_MULTIPLY)
        ):
            raise FormulaError("string repetition too large")
        try:
            return op(left, right)
        except FormulaError:
            raise
        except Exception as exc:
            # An operator can fail on perfectly well-formed input -- 1/0, 1%0, "a"-1. Only
            # _visit_Call wrapped its failures, so those escaped as a raw ZeroDivisionError or
            # TypeError straight out of evaluate_scorecard, breaking the contract stated in
            # logic._evaluate_formula ("a bad formula still returns {"error": ...} rather than
            # raising into the scorecard path"). An author's arithmetic typo must not 500 a
            # decision.
            raise FormulaError("{0} failed: {1}".format(type(node.op).__name__.lower(), exc)) from exc

    def _visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise FormulaError("disallowed unary operator")
        try:
            return op(self.visit(node.operand))
        except FormulaError:
            raise
        except Exception as exc:  # e.g. -"abc"
            raise FormulaError("unary operator failed: {0}".format(exc)) from exc

    def _visit_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.visit(v) for v in node.values]
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in values:
                result = value
                if not value:
                    return value
            return result
        result = False
        for value in values:
            result = value
            if value:
                return value
        return result

    def _visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            op = _COMPARE_OPS.get(type(op_node))
            if op is None:
                raise FormulaError("disallowed comparison")
            right = self.visit(comparator)
            if not op(left, right):
                return False
            left = right
        return True

    def _visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def _visit_Call(self, node: ast.Call) -> Any:
        # Only a direct call to a name in the function table. `f()()`, `obj.method()` and
        # `funcs[x]()` never reach a callable because the callee is not an ast.Name we resolve.
        if not isinstance(node.func, ast.Name):
            raise FormulaError("only direct function calls are allowed")
        func = self._functions.get(node.func.id)
        if func is None:
            raise FormulaError("unknown function: {0}".format(node.func.id))
        if node.keywords:
            raise FormulaError("keyword arguments are not allowed in formulas")
        args = [self.visit(arg) for arg in node.args]
        try:
            return func(*args)
        except FormulaError:
            raise
        except Exception as exc:  # a whitelisted function raised on its inputs -- surface it
            raise FormulaError("{0} failed: {1}".format(node.func.id, exc)) from exc

    # Container literals, so a formula may pass a list to e.g. SUM([...]).
    def _visit_List(self, node: ast.List) -> Any:
        return [self.visit(e) for e in node.elts]

    def _visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(e) for e in node.elts)
