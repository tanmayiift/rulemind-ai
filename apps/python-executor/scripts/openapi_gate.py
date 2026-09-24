"""Fail CI when the public API contract breaks (Track B6).

A snapshot of the OpenAPI document lives at apps/python-executor/openapi.json. This script
regenerates it from the live app and classifies the difference.

WHY NOT A PLAIN SNAPSHOT DIFF. The obvious implementation -- regenerate, `diff`, fail on any
change -- fails on every additive change too. Adding an endpoint would turn CI red, so the
snapshot gets regenerated reflexively, and once regenerating is a reflex it stops being a
check: the one time it fires for a real removal, it gets waved through with the same command.

So the exit code distinguishes:

  BREAKING  -> exit 1. An existing client stops working:
                 * an endpoint or method disappears
                 * a request field becomes required (old clients omit it)
                 * a response field disappears, stops being guaranteed, or may now be null
                 * a typed response changes type, or an operation's credentials change
                 * an enum loses a value the client may still send
  ADDITIVE  -> exit 0, printed. New endpoints, new optional request fields, new response
               fields, a response field becoming guaranteed. Old clients are unaffected, so
               this must not block a merge.

Request and response schemas follow opposite rules (see `_directions`), which only started to
matter once responses were typed (plan 5.1) -- before that the "response field disappears" rule
had no response fields to compare and could not fire.

Both cases print the command to refresh the snapshot. A genuinely intended break is approved
by committing the new snapshot -- which makes the break visible in review as a diff to a
checked-in contract, rather than invisible in a handler change.

  python scripts/openapi_gate.py           # check (CI)
  python scripts/openapi_gate.py --write   # refresh the snapshot
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

SNAPSHOT = APP_ROOT / "openapi.json"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def generate() -> Dict[str, Any]:
    from app.main import app

    return app.openapi()


def _operations(spec: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    ops = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in (item or {}).items():
            if method.lower() in HTTP_METHODS:
                ops[(path, method.lower())] = operation or {}
    return ops


def _schemas(spec: Dict[str, Any]) -> Dict[str, Any]:
    return (spec.get("components") or {}).get("schemas") or {}


def _refs(node: Any, found: Set[str]) -> Set[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)
    return found


def _directions(spec: Dict[str, Any]) -> Dict[str, Set[str]]:
    """Which way each component schema travels: "request", "response", or both.

    The rules differ by direction. A request field becoming required breaks clients that omit
    it; a RESPONSE field becoming required is a new guarantee and breaks no one. A response
    field becoming optional breaks clients that relied on it; a request field becoming optional
    breaks no one. Treating every schema the same -- as this gate did while no response was
    typed -- would call every new guarantee a break, and the reflexive snapshot refresh that
    follows is how a gate stops being one.

    A schema no operation references gets both, i.e. the strictest reading."""
    schemas = _schemas(spec)
    seeds: Dict[str, Set[str]] = {"request": set(), "response": set()}
    for operation in _operations(spec).values():
        _refs(operation.get("requestBody"), seeds["request"])
        _refs(operation.get("parameters"), seeds["request"])
        _refs(operation.get("responses"), seeds["response"])
    directions: Dict[str, Set[str]] = {name: set() for name in schemas}
    for direction, reached in seeds.items():
        frontier = set(reached)
        while frontier:
            for name in frontier:
                directions.setdefault(name, set()).add(direction)
            frontier = _refs([schemas.get(name) for name in frontier], set()) - reached
            reached |= frontier
    for found in directions.values():
        if not found:
            found.update({"request", "response"})
    return directions


def _type_of(prop: Dict[str, Any]) -> Tuple[str, bool]:
    """(what the value is, whether it may be null). Titles, descriptions and defaults are not
    part of the contract and are ignored."""
    prop = prop or {}
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[1], False
    members = prop.get("anyOf") or prop.get("oneOf")
    if members:
        kinds = [_type_of(member) for member in members]
        nullable = any(kind == "null" for kind, _ in kinds) or any(null for _, null in kinds)
        shown = sorted({kind for kind, _ in kinds if kind != "null"})
        return "|".join(shown) or "null", nullable
    kind = prop.get("type")
    if isinstance(kind, list):
        return "|".join(sorted(k for k in kind if k != "null")), "null" in kind
    if kind == "array":
        return "array<{0}>".format(_type_of(prop.get("items") or {})[0]), False
    return kind or "any", kind == "null"


def _response_type(operation: Dict[str, Any], code: str) -> str:
    schema = ((((operation.get("responses") or {}).get(code) or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    return _type_of(schema)[0]


def _security(spec: Dict[str, Any], operation: Dict[str, Any]) -> Set[str]:
    chosen = operation["security"] if "security" in operation else spec.get("security", [])
    return {"+".join(sorted(alternative)) for alternative in (chosen or [])}


def compare(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[str]]:
    """Classify old -> new. Returns {"breaking": [...], "additive": [...]}."""
    breaking: List[str] = []
    additive: List[str] = []

    old_ops, new_ops = _operations(old), _operations(new)
    for key in sorted(set(old_ops) - set(new_ops)):
        breaking.append(f"removed operation: {key[1].upper()} {key[0]}")
    for key in sorted(set(new_ops) - set(old_ops)):
        additive.append(f"new operation: {key[1].upper()} {key[0]}")

    # Security is compared only when BOTH documents describe it. The first document that did
    # would otherwise report every operation as newly guarded.
    both_describe_security = bool(((old.get("components") or {}).get("securitySchemes"))
                                  and ((new.get("components") or {}).get("securitySchemes")))

    for key in sorted(set(old_ops) & set(new_ops)):
        name = f"{key[1].upper()} {key[0]}"
        # requestBody.required. Found by shipping a change the gate could not see: relaxing
        # /promote from a required body to an optional one moved `"required": true` out of the
        # document and the classifier reported "unchanged". The relaxation is harmless, but the
        # OPPOSITE direction -- making a body newly required -- breaks every client that was
        # omitting it, and would have been just as invisible.
        was = bool((old_ops[key].get("requestBody") or {}).get("required"))
        now = bool((new_ops[key].get("requestBody") or {}).get("required"))
        if now and not was:
            breaking.append(f"request body newly required: {name}")
        elif was and not now:
            additive.append(f"request body now optional: {name}")

        # A typed success response that changes type, or stops being typed, breaks every
        # generated client that reads it. Gaining a type is additive.
        for code in sorted((old_ops[key].get("responses") or {})):
            if not code.startswith("2"):
                continue
            before, after = _response_type(old_ops[key], code), _response_type(new_ops[key], code)
            untyped = ("any", "object", "array<any>", "array<object>")
            if before != after and before not in untyped:
                breaking.append(f"response type changed: {name} {code} ({before} -> {after})")
            elif before != after:
                additive.append(f"response now typed: {name} {code} ({after})")

        if both_describe_security:
            before, after = _security(old, old_ops[key]), _security(new, new_ops[key])
            if before and not after:
                # Not a break for any client -- but an endpoint silently becoming public is
                # exactly what a contract gate should stop and make someone look at.
                breaking.append(f"now public (was guarded): {name}")
            elif after and not before:
                breaking.append(f"now requires credentials: {name}")
            elif before - after:
                breaking.append(f"stopped accepting {', '.join(sorted(before - after))}: {name}")
            elif after - before:
                additive.append(f"also accepts {', '.join(sorted(after - before))}: {name}")

    old_schemas, new_schemas = _schemas(old), _schemas(new)
    directions = _directions(new)
    for name, found in _directions(old).items():
        directions.setdefault(name, set()).update(found)

    for name in sorted(set(old_schemas) - set(new_schemas)):
        # A schema is only reachable through an operation, so a removed schema whose operations
        # all survive means those operations changed shape -- still a contract change worth
        # surfacing, but the operation-level checks carry the specifics.
        breaking.append(f"removed schema: {name}")

    for name in sorted(set(old_schemas) & set(new_schemas)):
        before, after = old_schemas[name] or {}, new_schemas[name] or {}
        request = "request" in directions.get(name, {"request", "response"})
        response = "response" in directions.get(name, {"request", "response"})
        old_fields, new_fields = before.get("properties") or {}, after.get("properties") or {}
        old_required = set(before.get("required") or [])
        new_required = set(after.get("required") or [])

        for prop in sorted(set(old_fields) - set(new_fields)):
            breaking.append(f"removed field: {name}.{prop}")
        for prop in sorted(new_required - old_required):
            if request:
                # Breaking whether or not the field is new: an existing client that has never
                # sent it now gets a 422.
                breaking.append(f"newly required field: {name}.{prop}")
            else:
                additive.append(f"field now always present: {name}.{prop}")
        for prop in sorted((old_required - new_required) & set(new_fields)):
            if response:
                breaking.append(f"field no longer guaranteed: {name}.{prop}")
            else:
                additive.append(f"field now optional: {name}.{prop}")
        for prop in sorted(set(new_fields) - set(old_fields)):
            if prop not in new_required:
                additive.append(f"new optional field: {name}.{prop}")

        for prop in sorted(set(old_fields) & set(new_fields)):
            (was_kind, was_null), (now_kind, now_null) = _type_of(old_fields[prop]), _type_of(new_fields[prop])
            if was_kind != now_kind and "any" not in (was_kind, now_kind):
                breaking.append(f"changed type: {name}.{prop} ({was_kind} -> {now_kind})")
            elif now_null and not was_null and response:
                breaking.append(f"may now be null: {name}.{prop}")
            elif was_null and not now_null and request:
                breaking.append(f"no longer accepts null: {name}.{prop}")

            old_enum = set(old_fields[prop].get("enum") or [])
            new_enum = set(new_fields[prop].get("enum") or [])
            if old_enum:
                for value in sorted(old_enum - new_enum):
                    breaking.append(f"removed enum value: {name}.{prop} = {value!r}")
                for value in sorted(new_enum - old_enum):
                    additive.append(f"new enum value: {name}.{prop} = {value!r}")

    return {"breaking": breaking, "additive": additive}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="refresh the committed snapshot")
    args = parser.parse_args(argv)

    current = generate()

    if args.write:
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"wrote {SNAPSHOT.relative_to(APP_ROOT.parent.parent)}")
        return 0

    if not SNAPSHOT.exists():
        print(f"No snapshot at {SNAPSHOT}. Create it with:\n"
              f"  python apps/python-executor/scripts/openapi_gate.py --write")
        return 1

    baseline = json.loads(SNAPSHOT.read_text())
    report = compare(baseline, current)

    for line in report["additive"]:
        print(f"  additive  {line}")
    for line in report["breaking"]:
        print(f"  BREAKING  {line}")

    if report["breaking"]:
        print(f"\n{len(report['breaking'])} breaking API change(s).")
        print("If intended, refresh the snapshot so the break is visible in review:")
        print("  python apps/python-executor/scripts/openapi_gate.py --write")
        return 1

    if report["additive"]:
        print(f"\n{len(report['additive'])} additive change(s) — not breaking, merge is fine.")
        print("Refresh the snapshot when convenient:")
        print("  python apps/python-executor/scripts/openapi_gate.py --write")
    else:
        print("  API contract unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
