"""Property-based accuracy: the engine evaluates ARBITRARY rule shapes correctly, not just the
hand-written fixtures.

For thousands of RANDOM rule trees — random AND/OR/NOT nesting, random depth/breadth, all 12
operators — we build each leaf so we KNOW its intended truth (a "satisfying" value => the condition
is true, a "violating" value => false), then check the production engine (app/logic.evaluate_rule_tree)
against an INDEPENDENT, trivially-correct boolean oracle for BOTH the final outcome AND the exact
passed-condition count. Agreement over the whole random space is strong evidence the engine is accurate
for every rule shape, not only the shapes we thought to write down. (Operator truth values here are the
same ones pinned by the committed operator + large-policy conformance fixtures.)
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.logic import MAX_RULE_TREE_DEPTH, evaluate_rule_definition, evaluate_rule_tree  # noqa: E402

OPS = ["==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists"]

# Values that make a condition on the given operator TRUE / FALSE. "__OMIT__" => leave the variable
# out of the payload entirely (for exists / !exists).
_SAT = {"==": False, "!=": 101, ">": 600, ">=": 500, "<": 499, "<=": 500, "between": 500,
        "in": "beta", "not_in": "ok", "regex": "Apple", "exists": 1, "!exists": "__OMIT__"}
_VIO = {"==": True, "!=": 100, ">": 400, ">=": 499, "<": 501, "<=": 501, "between": 900,
        "in": "zeta", "not_in": "x", "regex": "Banana", "exists": "__OMIT__", "!exists": 1}


def _condition(var: str, op: str) -> dict:
    if op == "==":
        return {"type": "condition", "variable": var, "operator": "==", "value": False, "fieldType": "boolean"}
    if op == "!=":
        return {"type": "condition", "variable": var, "operator": "!=", "value": 100}
    if op in (">", ">=", "<", "<="):
        return {"type": "condition", "variable": var, "operator": op, "value": 500}
    if op == "between":
        return {"type": "condition", "variable": var, "operator": "between", "value": 200, "value2": 800}
    if op == "in":
        return {"type": "condition", "variable": var, "operator": "in", "value": "alpha,beta,gamma"}
    if op == "not_in":
        return {"type": "condition", "variable": var, "operator": "not_in", "value": "x,y,z"}
    if op == "regex":
        return {"type": "condition", "variable": var, "operator": "regex", "value": "^A"}
    return {"type": "condition", "variable": var, "operator": op, "value": None}  # exists / !exists


class _Gen:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.n = 0
        self.leaves: list = []  # (var, op, intended_truth)

    def tree(self, depth: int) -> dict:
        # A leaf, or (with more depth left) a group; leaves may be NOT-wrapped.
        if depth <= 0 or self.rng.random() < 0.34:
            var = "v{0}".format(self.n)
            self.n += 1
            op = self.rng.choice(OPS)
            truth = self.rng.random() < 0.5
            self.leaves.append((var, op, truth))
            return _condition(var, op)
        logic = self.rng.choice(["AND", "OR"])
        children = []
        for _ in range(self.rng.randint(1, 4)):
            child = self.tree(depth - 1)
            if self.rng.random() < 0.15:
                child = {"type": "not", "child": child}
            children.append(child)
        return {"type": "group", "logic": logic, "children": children}


def _payload(leaves) -> dict:
    p = {}
    for var, op, truth in leaves:
        val = _SAT[op] if truth else _VIO[op]
        if val != "__OMIT__":
            p[var] = val
    return p


def _oracle(node: dict, truth_by_var: dict) -> bool:
    """A dead-simple, obviously-correct evaluator over the KNOWN leaf truths."""
    t = node.get("type")
    if t == "condition":
        return truth_by_var[node["variable"]]
    if t == "not":
        return not _oracle(node["child"], truth_by_var)
    results = [_oracle(c, truth_by_var) for c in node.get("children", [])]
    if node.get("logic") == "OR":
        return any(results) if results else False
    return all(results)  # AND (all([]) == True)


class RuleEnginePropertyAccuracyTests(unittest.TestCase):
    def test_engine_matches_independent_oracle_over_random_trees(self):
        rng = random.Random(1234)
        cases = 5000
        for i in range(cases):
            gen = _Gen(rng)
            root = gen.tree(depth=rng.randint(1, min(6, MAX_RULE_TREE_DEPTH - 1)))
            root["onPass"] = "approve"
            root["onFail"] = "reject"
            payload = _payload(gen.leaves)
            truth_by_var = {var: truth for var, _op, truth in gen.leaves}

            expected_pass = _oracle(root, truth_by_var)
            expected_outcome = "approve" if expected_pass else "reject"
            expected_true = sum(1 for _v, _o, truth in gen.leaves if truth)

            rule = {"id": "rp", "rule_format": "v2", "ruleFormat": "v2", "tree": root}
            got_outcome = evaluate_rule_definition(rule, payload)["outcome"]
            got_true = sum(1 for c in evaluate_rule_tree(root, payload)["conditions"] if c["passed"])

            self.assertEqual(got_outcome, expected_outcome,
                             "case {0}: outcome mismatch (leaves={1})".format(i, len(gen.leaves)))
            self.assertEqual(got_true, expected_true,
                             "case {0}: passed-count mismatch".format(i))

    def test_generator_actually_covers_all_operators_and_nesting(self):
        rng = random.Random(99)
        ops_seen, max_leaves, saw_not = set(), 0, False
        for _ in range(400):
            gen = _Gen(rng)
            root = gen.tree(depth=5)

            def walk(n):
                nonlocal saw_not
                if n.get("type") == "condition":
                    ops_seen.add(n["operator"])
                if n.get("type") == "not":
                    saw_not = True
                    walk(n["child"])
                for c in n.get("children", []):
                    walk(c)
            walk(root)
            max_leaves = max(max_leaves, len(gen.leaves))
        self.assertEqual(ops_seen, set(OPS), "random trees did not exercise every operator")
        self.assertTrue(saw_not, "random trees never produced a NOT")
        self.assertGreater(max_leaves, 15, "random trees never got reasonably large")


if __name__ == "__main__":
    unittest.main()
