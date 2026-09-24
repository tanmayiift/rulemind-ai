#!/usr/bin/env python3
"""GATE: every endpoint `API.md` names must actually exist.

`API.md` is not a nice-to-have. `/openapi.json`, `/docs` and `/redoc` are all disabled when
`RULEMIND_ENV=production` (`main._docs_enabled`) and the Helm chart sets exactly that, so for a
self-hosted install this file was the ONLY API contract shipped. There is now an authenticated
`GET /api/v1/openapi.json` as well, which is the real answer -- but a prose document that names
endpoints is still the first thing a person reads, and a wrong one costs an integration team a
day before they stop believing it.

Measured 2026-09-24, before this gate: **278 endpoints exist, API.md documented 17, and 13 of
those 17 did not exist.** Not merely incomplete -- wrong about paths:

    GET  /api/v1/api-keys              -> really /api/v1/access/keys
    GET  /api/v1/approvals             -> really /api/v1/reviews
    POST /api/v1/approvals/:id/approve -> really /api/v1/reviews/{task_id}/decide
    POST /api/v1/rules/validate        -> does not exist
    POST /api/v1/rules/:id/evaluate    -> really /api/v1/rules/{rule_id}/test
    GET  /api/v1/dashboard/summary     -> does not exist

Coverage of real endpoints: 4 of 278, about 1%.

This does NOT require API.md to document everything -- it cannot, and the spec is for that.
It requires that what it DOES say is true. Same family as `check_parity.py`,
`verify_chart.py` and `check_config_surface.py`: derived from source, so it cannot be satisfied
by editing a list.

Run:  python scripts/check_api_docs.py [-v]
Exit: 0 = every documented path exists, 1 = violations (printed).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "apps", "python-executor", "app")
DOC = os.path.join(ROOT, "API.md")

# `GET /api/v1/policies` in backticks, the form the document uses for a route.
_CLAIM = re.compile(r"`(GET|POST|PUT|PATCH|DELETE)\s+(/[^`\s]+)`")
# A bare backticked path, with no verb -- the form the "Generic Asset Endpoints" list uses.
# Checked too, because that list named FOUR collections that do not exist (`/api/v1/segments`,
# `/api/v1/ab-tests`, `/api/v1/exports`, `/api/v1/outcome-imports`) and a verb-only check
# would have passed the document with them in it.
_BARE = re.compile(r"`(/(?:api|health|metrics|ready|docs|openapi)[^`\s]*)`")
_ROUTE = re.compile(r'@(?:router|app)\.(get|post|put|patch|delete)\(\s*"([^"]+)"')

# A placeholder, not a claim: API.md uses `<resource>` to describe the CRUD shape shared by
# policies/rules/variables/... rather than to name an endpoint. Allowed, and only this spelling,
# so a real path cannot hide behind the exemption.
_PLACEHOLDER = re.compile(r"<[a-z_]+>")

# Declared on the FastAPI constructor rather than with a decorator, so the route scan cannot
# see them. Gated by RULEMIND_ENV -- present in development, absent in production -- which is
# precisely why /api/v1/openapi.json (a real decorated route, found normally) exists.
_CONSTRUCTOR_ROUTES = {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/redoc")}

# A blockquote is a NOTE ABOUT THE PAST, not a claim about the API. API.md records the paths it
# used to name wrongly -- `/api/v1/approvals`, `/api/v1/api-keys` -- because a reader holding
# the old document needs to recognise them. Checking those would make the gate fail on the
# correction itself, and the obvious workaround (deleting the note) would throw away the part
# that helps someone who already integrated against the wrong path.


def _normalise(path: str) -> str:
    """`/api/v1/rules/:id` and `/api/v1/rules/{rule_id}` are the same route."""
    path = re.sub(r":[A-Za-z_]+", "{}", path)
    path = re.sub(r"\{[A-Za-z_]+\}", "{}", path)
    return path.rstrip("/") or "/"


def existing_routes(app_dir: str = APP) -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(dirpath, filename), encoding="utf-8") as handle:
                text = handle.read()
            for verb, path in _ROUTE.findall(text):
                found.add((verb.upper(), path))
    return found


def _claiming_text(doc: str) -> str:
    """The document minus its blockquotes -- see _CONSTRUCTOR_ROUTES above."""
    with open(doc, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith(">"))


def documented_routes(doc: str = DOC) -> List[Tuple[str, str]]:
    return [(verb, path) for verb, path in _CLAIM.findall(_claiming_text(doc))]


def documented_paths(doc: str = DOC) -> List[str]:
    """Bare backticked paths -- collections named without a verb."""
    text = _claiming_text(doc)
    verbed = {path for _verb, path in _CLAIM.findall(text)}
    return [path for path in _BARE.findall(text) if path not in verbed]


def main(argv: List[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    existing = existing_routes() | _CONSTRUCTOR_ROUTES
    existing_n = {(verb, _normalise(path)) for verb, path in existing}
    claimed = documented_routes()

    missing: List[Tuple[str, str]] = []
    placeholders = 0
    for verb, path in claimed:
        if _PLACEHOLDER.search(path):
            placeholders += 1
            continue
        if (verb, _normalise(path)) not in existing_n:
            missing.append((verb, path))

    # Bare paths: any verb reaching them counts, since the document names no verb.
    prefixes = {_normalise(path) for _verb, path in existing}
    bare_missing: List[str] = []
    bare_claimed = documented_paths()
    for path in bare_claimed:
        if _PLACEHOLDER.search(path):
            placeholders += 1
            continue
        normalised = _normalise(path)
        if not any(candidate == normalised or candidate.startswith(normalised + "/")
                   for candidate in prefixes):
            bare_missing.append(path)

    real = len(claimed) - placeholders
    print(f"endpoints the API exposes:   {len(existing)}")
    print(f"endpoints API.md names:      {real}"
          + (f" (+{placeholders} <placeholder> forms)" if placeholders else ""))
    print(f"…that do not exist:          {len(missing)}")
    if verbose:
        for verb, path in sorted(claimed):
            print(f"    {verb:6} {path}")

    if bare_claimed:
        print(f"collections API.md names:    {len(bare_claimed)}"
              f"  (…that do not exist: {len(bare_missing)})")

    if missing or bare_missing:
        print("\nAPI DOCS GATE FAILED\n")
        print("  API.md names endpoints that do not exist. For a production install this file")
        print("  is the first contract a person reads, and every one of these is a 404 for an")
        print("  integration team following the documentation:\n")
        for verb, path in missing:
            near = sorted({p for v, p in existing
                           if v == verb and path.rsplit("/", 1)[-1].strip(":{}") in p})
            hint = f"   (nearest: {near[0]})" if near else ""
            print(f"      {verb:6} {path}{hint}")
        if bare_missing:
            print("\n  And these collections are named with no verb and do not exist at all:\n")
            for path in bare_missing:
                print(f"      {path}")
        print("\n  Fix the path, or delete the section. The authenticated spec at")
        print("  GET /api/v1/openapi.json is the complete contract; API.md is the")
        print("  human introduction, and it only has to be TRUE, not exhaustive.")
        return 1
    print("\nAPI docs OK — every endpoint API.md names exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
