#!/usr/bin/env python3
"""Fail when `--ignore-vuln` in security.yml names an advisory pip-audit no longer reports.

Why this exists: 13 of the 24 ignore lines were dead. Every package they excused --
pyjwt, lxml, python-dotenv, lightgbm, and three of the cryptography advisories -- had already
been bumped past its fix in requirements.txt, but the lines stayed. pip-audit accepts an id it
never saw without complaint, so nothing said so.

A dead ignore is not merely clutter. It is a pre-armed suppression: the day someone pins pyjwt
back for a compatibility fix, seven advisories against the library that validates admin session
JWTs and OIDC assertions are silently excused again, and the audit still reports clean.

Run:  python scripts/check_audit_ignores.py [--workflow .github/workflows/security.yml]
Exit: 0 = every ignore is still live, 1 = dead ignores (listed), 2 = pip-audit could not run.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REQUIREMENTS = "apps/python-executor/requirements.txt"


def reported_ids(requirements: str) -> set:
    """Every advisory id pip-audit reports, including aliases (it ignores by any of them)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--requirement", requirements, "--format", "json"],
        capture_output=True, text=True, check=False)
    if not proc.stdout.strip():
        print("pip-audit produced no output:\n{0}".format(proc.stderr.strip()), file=sys.stderr)
        raise SystemExit(2)
    ids = set()
    for dep in json.loads(proc.stdout).get("dependencies", []):
        for vuln in dep.get("vulns", []):
            ids.add(vuln["id"])
            ids.update(vuln.get("aliases") or [])
    return ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default=".github/workflows/security.yml")
    parser.add_argument("--requirements", default=REQUIREMENTS)
    args = parser.parse_args()

    # Anchored to the start of a line: array entries each sit on their own. An unanchored
    # search also matched the words "--ignore-vuln lines" inside a step name and prose, and
    # reported the literal token "lines" as a dead advisory.
    listed = re.findall(r"^\s*--ignore-vuln (\S+)",
                        Path(args.workflow).read_text(encoding="utf-8"), re.MULTILINE)
    if not listed:
        print("no --ignore-vuln entries in {0}".format(args.workflow))
        return 0

    live = reported_ids(args.requirements)
    dead = [entry for entry in listed if entry not in live]
    if dead:
        print("{0} ignore line(s) name an advisory pip-audit no longer reports.".format(len(dead)))
        print("The pin has been fixed; delete the line so a regression is not pre-suppressed.\n")
        for entry in dead:
            print("  dead: {0}".format(entry))
        return 1
    print("all {0} ignore lines still name a live advisory".format(len(listed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
