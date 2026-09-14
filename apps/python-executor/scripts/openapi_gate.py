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
                 * a response field disappears (clients read it)
                 * an enum loses a value the client may still send
  ADDITIVE  -> exit 0, printed. New endpoints, new optional request fields, new response
               fields. Old clients are unaffected, so this must not block a merge.

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
from typing import Any, Dict, List, Tuple

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


def compare(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[str]]:
    """Classify old -> new. Returns {"breaking": [...], "additive": [...]}."""
    breaking: List[str] = []
    additive: List[str] = []

    old_ops, new_ops = _operations(old), _operations(new)
    for key in sorted(set(old_ops) - set(new_ops)):
        breaking.append(f"removed operation: {key[1].upper()} {key[0]}")
    for key in sorted(set(new_ops) - set(old_ops)):
        additive.append(f"new operation: {key[1].upper()} {key[0]}")

    # requestBody.required. Found by shipping a change the gate could not see: relaxing
    # /promote from a required body to an optional one moved `"required": true` out of the
    # document and the classifier reported "unchanged". The relaxation is harmless, but the
    # OPPOSITE direction -- making a body newly required -- breaks every client that was
    # omitting it, and would have been just as invisible.
    for key in sorted(set(old_ops) & set(new_ops)):
        was = bool((old_ops[key].get("requestBody") or {}).get("required"))
        now = bool((new_ops[key].get("requestBody") or {}).get("required"))
        if now and not was:
            breaking.append(f"request body newly required: {key[1].upper()} {key[0]}")
        elif was and not now:
            additive.append(f"request body now optional: {key[1].upper()} {key[0]}")

    old_schemas, new_schemas = _schemas(old), _schemas(new)
    for name in sorted(set(old_schemas) - set(new_schemas)):
        # A schema is only reachable through an operation, so a removed schema whose operations
        # all survive means those operations changed shape -- still a contract change worth
        # surfacing, but the operation-level checks below carry the specifics.
        breaking.append(f"removed schema: {name}")

    for name in sorted(set(old_schemas) & set(new_schemas)):
        before, after = old_schemas[name] or {}, new_schemas[name] or {}
        old_props = set((before.get("properties") or {}))
        new_props = set((after.get("properties") or {}))
        old_required = set(before.get("required") or [])
        new_required = set(after.get("required") or [])

        for prop in sorted(old_props - new_props):
            breaking.append(f"removed field: {name}.{prop}")
        for prop in sorted(new_required - old_required):
            # Newly required is breaking whether or not the field is new: an existing client
            # that has never sent it now gets a 422.
            breaking.append(f"newly required field: {name}.{prop}")
        for prop in sorted(new_props - old_props):
            if prop not in new_required:
                additive.append(f"new optional field: {name}.{prop}")

        for prop in sorted(old_props & new_props):
            old_enum = set((before.get("properties") or {}).get(prop, {}).get("enum") or [])
            new_enum = set((after.get("properties") or {}).get(prop, {}).get("enum") or [])
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
