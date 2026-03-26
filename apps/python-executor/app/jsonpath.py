from __future__ import annotations

import ast
import math
from typing import Any, Dict


def _resolve_attr(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def resolve_jsonpath(path: str, context: Dict[str, Any]) -> Any:
    if not path.startswith("$."):
        return None
    expression = path[2:]
    current: Any = context
    token = ""
    index_mode = False
    index_buffer = ""
    for char in expression:
        if char == "." and not index_mode:
            if token:
                current = _resolve_attr(current, token)
                token = ""
            continue
        if char == "[":
            index_mode = True
            if token:
                current = _resolve_attr(current, token)
                token = ""
            continue
        if char == "]":
            index_mode = False
            if not isinstance(current, list):
                return None
            try:
                index = int(index_buffer)
            except ValueError:
                return None
            if index < 0:
                index = len(current) + index
            if index < 0 or index >= len(current):
                return None
            current = current[index]
            index_buffer = ""
            continue
        if index_mode:
            index_buffer += char
        else:
            token += char
    if token:
        current = _resolve_attr(current, token)
    return current


def evaluate_math_expr(expression: str, context: Dict[str, Any]) -> Any:
    source = expression.strip()
    if source.startswith("$."):
        source = source.replace("$.", "root.", 1)
    tree = ast.parse(source, mode="eval")
    allowed_functions = {"min": min, "max": max, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil}

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right if right else 0
            raise ValueError("Unsupported operator")
        if isinstance(node, ast.Call):
            func_name = node.func.id if isinstance(node.func, ast.Name) else None
            if func_name not in allowed_functions:
                raise ValueError("Unsupported function")
            return allowed_functions[func_name](*[visit(arg) for arg in node.args])
        if isinstance(node, ast.Attribute):
            base = visit(node.value)
            return _resolve_attr(base, node.attr)
        if isinstance(node, ast.Name):
            if node.id == "root":
                return context
            raise ValueError("Unsupported variable")
        if isinstance(node, ast.Subscript):
            target = visit(node.value)
            index = visit(node.slice)
            if isinstance(target, list) and isinstance(index, int):
                return target[index]
            if isinstance(target, dict):
                return target.get(index)
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        raise ValueError("Unsupported expression")

    return visit(tree)
