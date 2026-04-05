from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .logic import flatten_tree_to_nodes, json_dumps
from .storage import Storage


_COMPILER_DEBOUNCE: dict[str, float] = {}
_PRIVATE_KEY = None
_PUBLIC_KEY = None


class NoProductionAssetsError(ValueError):
    pass


class BundleCompilationError(RuntimeError):
    def __init__(self, message: str, *, entity_type: Optional[str] = None, entity_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.entity_type = entity_type
        self.entity_id = entity_id


def _load_signing_keypair():
    global _PRIVATE_KEY, _PUBLIC_KEY
    if _PRIVATE_KEY is not None and _PUBLIC_KEY is not None:
        return _PRIVATE_KEY, _PUBLIC_KEY
    pem = os.getenv("RULEMIND_SIGNING_PRIVATE_KEY")
    if pem:
        private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    else:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _PRIVATE_KEY = private_key
    _PUBLIC_KEY = private_key.public_key()
    return _PRIVATE_KEY, _PUBLIC_KEY


def _parse_public_key(header_value: Optional[str]):
    if not header_value:
        return None
    raw = header_value.encode("utf-8")
    try:
        decoded = base64.b64decode(raw)
        if b"BEGIN PUBLIC KEY" in decoded:
            return serialization.load_pem_public_key(decoded)
        try:
            return serialization.load_der_public_key(decoded)
        except Exception:
            decoded = raw
    except Exception:
        decoded = raw
    if b"BEGIN PUBLIC KEY" in decoded:
        return serialization.load_pem_public_key(decoded)
    return serialization.load_der_public_key(decoded)


def _sign_payload(payload: bytes) -> str:
    private_key, _ = _load_signing_keypair()
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def _encrypt_bundle_payload(payload: bytes, client_public_key_header: Optional[str]) -> Tuple[str, Optional[str]]:
    aes_key = AESGCM.generate_key(bit_length=256)
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)
    encrypted = aesgcm.encrypt(nonce, payload, None)
    encrypted_payload = base64.b64encode(nonce + encrypted).decode("utf-8")
    public_key = _parse_public_key(client_public_key_header)
    if public_key is None:
        return encrypted_payload, None
    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return encrypted_payload, base64.b64encode(encrypted_key).decode("utf-8")


def render_bundle_response(bundle_content: Dict[str, Any], client_public_key: Optional[str]) -> Dict[str, Any]:
    payload_bytes = json.dumps(bundle_content, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encrypted_bundle, encrypted_key = _encrypt_bundle_payload(payload_bytes, client_public_key)
    return {
        "bundleVersion": bundle_content["bundleVersion"],
        "encryptedBundle": encrypted_bundle,
        "encryptedKey": encrypted_key,
        "signature": _sign_payload(payload_bytes),
        "checksum": bundle_content["checksum"],
        "compiledAt": bundle_content["compiledAt"],
        "expiresAt": bundle_content["expiresAt"],
    }


def _new_register(counter: Dict[str, int]) -> str:
    counter["value"] += 1
    return "r{0}".format(counter["value"])


def _compile_expr(node: ast.AST, instructions: List[Dict[str, Any]], counter: Dict[str, int], env: Dict[str, str]) -> str:
    if isinstance(node, ast.Constant):
        target = _new_register(counter)
        instructions.append({"op": "literal", "target": target, "value": node.value})
        return target
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise ValueError("unsupported:name:{0}".format(node.id))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        source = _compile_expr(node.operand, instructions, counter, env)
        target = _new_register(counter)
        instructions.append({"op": "multiply", "target": target, "left": source, "rightValue": -1})
        return target
    if isinstance(node, ast.Subscript):
        source = _compile_expr(node.value, instructions, counter, env)
        index = _compile_expr(node.slice, instructions, counter, env)
        target = _new_register(counter)
        instructions.append({"op": "get", "target": target, "source": source, "key": index})
        return target
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            source = _compile_expr(node.func.value, instructions, counter, env)
            key_node = node.args[0] if node.args else ast.Constant(value="")
            default_node = node.args[1] if len(node.args) > 1 else ast.Constant(value=0)
            key_register = _compile_expr(key_node, instructions, counter, env)
            default_register = _compile_expr(default_node, instructions, counter, env)
            target = _new_register(counter)
            instructions.append({"op": "get", "target": target, "source": source, "key": key_register, "default": default_register})
            return target
        if isinstance(node.func, ast.Name) and node.func.id in {"min", "max"}:
            args = [_compile_expr(arg, instructions, counter, env) for arg in node.args]
            if len(args) < 2:
                raise ValueError("unsupported:call:{0}".format(node.func.id))
            target = _new_register(counter)
            instructions.append({"op": node.func.id, "target": target, "args": args})
            return target
        if isinstance(node.func, ast.Name) and node.func.id in {"int", "float", "str", "bool"} and len(node.args) == 1:
            source = _compile_expr(node.args[0], instructions, counter, env)
            target = _new_register(counter)
            instructions.append({"op": "cast", "target": target, "source": source, "type": node.func.id})
            return target
        raise ValueError("unsupported:call")
    if isinstance(node, ast.BinOp):
        left = _compile_expr(node.left, instructions, counter, env)
        right = _compile_expr(node.right, instructions, counter, env)
        op_map = {
            ast.Add: "add",
            ast.Sub: "subtract",
            ast.Mult: "multiply",
            ast.Div: "divide",
        }
        op_name = op_map.get(type(node.op))
        if not op_name:
            raise ValueError("unsupported:binop:{0}".format(type(node.op).__name__))
        target = _new_register(counter)
        instructions.append({"op": op_name, "target": target, "left": left, "right": right})
        return target
    raise ValueError("unsupported:{0}".format(type(node).__name__))


def compile_variable(variable: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    source = str(variable.get("code", "")).replace("@variable", "").strip()
    if source.startswith("("):
        closing = source.find(")")
        if closing != -1:
            source = source[closing + 1 :].strip()
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return False, None, "syntax_error:{0}".format(error.msg)

    banned_nodes = (
        ast.Import,
        ast.ImportFrom,
        ast.Try,
        ast.With,
        ast.AsyncFunctionDef,
        ast.Await,
        ast.Yield,
        ast.ClassDef,
        ast.Lambda,
        ast.Global,
        ast.Nonlocal,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )
    for node in ast.walk(tree):
        if isinstance(node, banned_nodes):
            return False, None, "unsupported:{0}".format(type(node).__name__)

    function_node = next((node for node in tree.body if isinstance(node, ast.FunctionDef)), None)
    if function_node is None:
        return False, None, "unsupported:missing_function"
    instructions: List[Dict[str, Any]] = []
    counter = {"value": 0}
    env = {argument.arg: argument.arg for argument in function_node.args.args}
    try:
        for statement in function_node.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                env[statement.targets[0].id] = _compile_expr(statement.value, instructions, counter, env)
                continue
            if isinstance(statement, ast.Return):
                register = _compile_expr(statement.value or ast.Constant(value=None), instructions, counter, env)
                instructions.append({"op": "return", "source": register})
                break
            raise ValueError("unsupported:stmt:{0}".format(type(statement).__name__))
    except ValueError as error:
        return False, None, str(error)

    if not instructions or instructions[-1].get("op") != "return":
        return False, None, "unsupported:missing_return"

    compiled = {
        "id": variable["id"],
        "sourceId": variable["source_id"],
        "name": variable["name"],
        "returnType": "number",
        "instructions": instructions,
    }
    return True, compiled, None


def compile_bundle(storage: Storage, tenant_id: str, client_public_key: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    last = _COMPILER_DEBOUNCE.get(tenant_id, 0.0)
    now = time.time()
    if not force and now - last < 5:
        latest = storage.latest_bundle(tenant_id=tenant_id)
        if latest:
            return latest
    _COMPILER_DEBOUNCE[tenant_id] = now

    variables = storage.list_variables(status="prod", tenant_id=tenant_id)
    rules = storage.list_rules(status="prod", tenant_id=tenant_id)
    scorecards = storage.list_scorecards(status="prod", tenant_id=tenant_id)
    policies = storage.list_policies(status="prod", tenant_id=tenant_id)
    experiments = [item for item in storage.list_experiments(tenant_id=tenant_id) if item.get("status") == "running"]

    if not any([variables, rules, scorecards, policies]):
        raise NoProductionAssetsError("No production assets to compile")

    compiled_variables: List[Dict[str, Any]] = []
    server_only_variables: List[str] = []
    compilation_warnings: List[Dict[str, Any]] = []
    for variable in variables:
        compilable, compiled, reason = compile_variable(variable)
        if compilable and compiled:
            compiled_variables.append(compiled)
        else:
            if reason and reason.startswith("syntax_error:"):
                raise BundleCompilationError(
                    "Bundle compile failed for variable {0}: {1}".format(variable["id"], reason),
                    entity_type="variable",
                    entity_id=variable["id"],
                )
            server_only_variables.append(variable["id"])
            compilation_warnings.append({"entity_type": "variable", "entity_id": variable["id"], "detail": reason})

    compiled_rules = []
    for rule in rules:
        compiled_rules.append(
            {
                "id": rule["id"],
                "name": rule["name"],
                "ruleFormat": rule.get("rule_format", "v1"),
                "tree": copy.deepcopy(rule.get("tree")),
                "nodes": copy.deepcopy(rule.get("nodes") or flatten_tree_to_nodes(rule.get("tree"))),
                "expression": rule.get("expression"),
                "status": rule.get("status"),
            }
        )

    compiled_policies = []
    for policy in policies:
        server_only_steps = []
        edge_steps = []
        for step in policy.get("steps", []):
            step_type = step.get("type")
            step_id = step.get("id") or step.get("ref_id") or step.get("ref")
            if step_type not in {"connector", "rule", "scorecard", "transform", "action", "review_gate", "outcome"}:
                if step_id:
                    server_only_steps.append(step_id)
                continue
            edge_steps.append(copy.deepcopy(step))
        compiled_policies.append(
            {
                "id": policy["id"],
                "name": policy["name"],
                "steps": edge_steps,
                "serverOnlySteps": server_only_steps,
                "defaultOutcome": policy.get("defaultOutcome"),
                "trigger": copy.deepcopy(policy.get("trigger")),
            }
        )

    latest = storage.latest_bundle(tenant_id=tenant_id)
    version = int(latest["version"]) + 1 if latest else 1
    compiled_at = datetime.utcnow()
    bundle = {
        "bundleVersion": version,
        "bundleId": hashlib.sha256("{0}:{1}".format(tenant_id, version).encode("utf-8")).hexdigest()[:24],
        "tenantId": tenant_id,
        "compiledAt": compiled_at.replace(microsecond=0).isoformat() + "Z",
        "expiresAt": (compiled_at + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z",
        "variables": compiled_variables,
        "rules": compiled_rules,
        "scorecards": scorecards,
        "policies": compiled_policies,
        "experiments": experiments,
        "serverOnlyVariables": server_only_variables,
        "compilationWarnings": compilation_warnings,
    }
    checksum = "sha256:" + hashlib.sha256(json_dumps(bundle).encode("utf-8")).hexdigest()
    bundle["checksum"] = checksum
    payload_bytes = json.dumps(bundle, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encrypted_bundle, encrypted_key = _encrypt_bundle_payload(payload_bytes, client_public_key)
    signature = _sign_payload(payload_bytes)
    return storage.save_bundle(
        {
            "tenant_id": tenant_id,
            "version": version,
            "content": bundle,
            "encrypted_content": encrypted_bundle,
            "encrypted_key": encrypted_key,
            "signature": signature,
            "checksum": checksum,
            "compiled_at": compiled_at,
            "expires_at": compiled_at + timedelta(days=30),
        },
        tenant_id=tenant_id,
    )
