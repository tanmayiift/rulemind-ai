#!/usr/bin/env python3
"""GATE: every setting the backend reads must be reachable and visible to an operator.

The defect this exists to stop is not a missing flag. It is the SHAPE of a missing flag, and
it had 84 instances at once.

Measured 2026-09-24, before this gate: the backend reads **111** environment variables.
`docker-compose.yml`, the Helm chart and `k8s/` together could deliver **27**. **84 were
settable by nothing** -- and 68 of those were documented in `.env.example`, so an operator
could read about a setting they had no way to apply. Among them:

  DECISION_ENCRYPT_AT_REST   encryption at rest for decisions could not be turned on
  AUDIT_ENCRYPT_AT_REST      nor for the audit log
  LOG_LEVEL                  verbosity could not be raised during an incident
  RATE_LIMIT_*               the limiter could not be configured or disabled
  CIRCUIT_BREAKER            connector circuit breaking was untunable
  SANDBOX_FALLBACK_MODE      the isolation posture for untrusted customer Python
  RULEMIND_SIGNING_PRIVATE_KEY   without it, bundle signing breaks above one replica

For a self-hosted product the Helm chart IS the configuration API. A customer who can reach
16% of the software's settings does not have a configurable product; they have a fixed one
with a long changelog. PROMETHEUS_MULTIPROC_DIR, MODEL_MEMORY_MB, RULEMIND_SELF_SERVE_SIGNUP,
DECISION_ARCHIVE_SINK and FAST_DECIDE were never five unrelated bugs -- they were five samples
from this population, each found by tripping over it.

So this asserts three things, and nothing else:

  1. REACHABLE  -- an operator can set it: a named key in an artifact, or the documented
                   passthrough (`config:` / `secretConfig:` in the chart, `env_file` in
                   compose).  The passthrough's own existence is checked, because a contract
                   that can silently disappear is the failure mode this repo keeps finding.
  2. VISIBLE    -- it is written down where an operator looks: `.env.example` (the catalogue)
                   and, unless internal, `values.yaml` (the chart's interface).
  3. HONEST     -- every INTERNAL allow-list entry still corresponds to a variable the code
                   reads, so the list shrinks visibly instead of quietly accumulating.

Run:  python scripts/check_config_surface.py [-v]
Exit: 0 = held, 1 = violations (printed).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_surface import ROOT, build  # noqa: E402

# Genuinely internal: read by the code, but NOT an operator's setting. Each carries its reason,
# because "internal" is the sentence that would otherwise absorb the whole list again.
INTERNAL: Dict[str, str] = {
    "PYTEST_CURRENT_TEST":
        "set by pytest itself; security_config treats its presence as a test run. Not a setting.",
    "RULEMIND_TEST_PG_SCHEMA_PER_PATH":
        "test harness only -- gives each test path its own Postgres schema. Setting it on a "
        "real deployment would scatter the schema, not configure anything.",
    "RULEMIND_SKIP_DOTENV":
        "bootstrap escape hatch read in app/__init__.py before anything else, to stop a "
        "developer's .env leaking into a test run. Not a deployment setting.",
    "prometheus_multiproc_dir":
        "prometheus_client's OWN legacy lowercase alias, which it reads itself. Ours is the "
        "uppercase PROMETHEUS_MULTIPROC_DIR, delivered by both compose and the chart.",
}

# The passthrough contract.  If any of these disappears, every 'reachable' verdict below is
# vacuous -- so the file and the marker are asserted, not assumed.
PASSTHROUGH = [
    ("helm/rulemind/templates/configmap.yaml", "range $key, $value := .Values.config",
     "the chart's ConfigMap passthrough"),
    ("helm/rulemind/templates/secret.yaml", "range $key, $value := .Values.secretConfig",
     "the chart's Secret passthrough"),
    ("docker-compose.yml", "env_file:", "compose's env_file passthrough"),
]


def _values_yaml(root: str) -> str:
    return open(os.path.join(root, "helm", "rulemind", "values.yaml"), encoding="utf-8").read()


def _listed_in_values(name: str, values: str) -> bool:
    """Named in values.yaml -- set, or commented with its in-code default. Both count: the
    point is that an operator reading the chart's interface LEARNS THE SETTING EXISTS."""
    return bool(re.search(r"(?m)^\s*#?\s*" + re.escape(name) + r"\s*:", values))


def main(argv: List[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    settings = build(ROOT)
    values = _values_yaml(ROOT)
    failures: List[str] = []

    # 0. The passthrough must exist, or nothing below means anything.
    for relpath, marker, label in PASSTHROUGH:
        path = os.path.join(ROOT, relpath)
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if marker not in text:
            failures.append(
                f"{relpath}: {label} is GONE (looked for {marker!r}).\n"
                f"    Without it every setting below becomes undeliverable again, and this "
                f"gate would pass anyway -- which is the exact class of check this repo keeps "
                f"finding. Restore it.")

    named: Set[str] = set()
    invisible: List[str] = []
    undocumented: List[str] = []

    for name in sorted(settings):
        entry = settings[name]
        if name in INTERNAL:
            continue
        if entry.delivered_by:
            named.add(name)
        # VISIBLE, part 1: the operator catalogue.
        if not entry.documented:
            undocumented.append(f"{name}  <- {entry.readers[0]}")
        # VISIBLE, part 2: the chart's own interface. A variable rendered by a named chart key
        # is visible through that key; everything else must appear in values.yaml.
        if not entry.delivered_by and not _listed_in_values(name, values):
            invisible.append(f"{name}  <- {entry.readers[0]}")

    if undocumented:
        failures.append(
            f"{len(undocumented)} setting(s) the backend reads are NOT in .env.example.\n"
            f"    .env.example is the operator's catalogue; a setting missing from it is a "
            f"setting nobody knows exists. Add it with its in-code default, or add it to "
            f"INTERNAL in this file with the reason.\n      "
            + "\n      ".join(undocumented))

    if invisible:
        failures.append(
            f"{len(invisible)} setting(s) appear in NO deployment artifact and NO chart value.\n"
            f"    A self-hosted customer configures this product through helm/rulemind/"
            f"values.yaml. List them under `config:` (commented, with the in-code default, is "
            f"fine -- the point is that they are discoverable), or under `secretConfig:` if the "
            f"VALUE is a secret.\n      "
            + "\n      ".join(invisible))

    # 3. HONEST: an allow-list entry for a variable nothing reads any more is dead weight, and
    #    dead weight is how an allow-list turns into a place things go to stop being counted.
    stale = sorted(set(INTERNAL) - set(settings))
    if stale:
        failures.append(
            "INTERNAL names a setting the code no longer reads: " + ", ".join(stale) +
            "\n    Delete the entry. The list has to shrink when the code does.")

    total = len(settings)
    print(f"settings the backend reads:            {total}")
    print(f"  named by an artifact (opinionated):  {len(named)}")
    print(f"  reachable via the passthrough:       {total - len(named) - len(INTERNAL)}")
    print(f"  internal (allow-listed, not settings): {len(INTERNAL)}")
    if verbose:
        for name, reason in sorted(INTERNAL.items()):
            print(f"    {name}: {reason}")

    if failures:
        print("\nCONFIG SURFACE GATE FAILED\n")
        for failure in failures:
            print("  " + failure + "\n")
        return 1
    print("\nconfig surface OK — every setting the code reads is reachable and documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
