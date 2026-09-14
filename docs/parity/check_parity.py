#!/usr/bin/env python3
"""Guard: the parity matrix must stay true, not merely present.

Three things rot silently and each has been wrong here:

1. An unmet feature loses its owning phase or E2E flow and nobody notices.
2. An `evidence` citation points at a file:line that no longer exists. Line numbers drift on
   every refactor, and a matrix whose whole promise is "every claim cites a file and line"
   is worth nothing once the lines are wrong -- it reads as verified and is not.
3. PARITY-MATRIX.md and parity.json disagree. They carry the same audit date and the same 22
   rows, but the table said present=2/partial=9 while the JSON said PRESENT=4/PARTIAL=7 --
   two features had been re-assessed in one and not the other. This script read only the JSON,
   so it could not see the disagreement it was supposed to prevent.

Run from the repo root: python docs/parity/check_parity.py
Exit: 0 = consistent, 1 = problems (all printed, not just the first).
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# Roots a citation may be written relative to. `apps/python-executor` is first because most
# citations are backend paths written without that prefix (`app/routers/authoring.py:97`).
SEARCH_ROOTS = ("apps/python-executor", "", "packages", "apps/web")

# `path/to/file.py:12` or `file.py:12,34,56` -- the multi-line form is used for CRUD row groups.
CITATION = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|ts|tsx|rs|kt|dart|yml|yaml|json|md|sh)):(\d+(?:,\d+)*)")

# Table rows read `| ✅ present | 2 |`; the JSON uses upper-case state names.
STATE_BY_SYMBOL = {"✅": "PRESENT", "🟡": "PARTIAL", "❌": "ABSENT", "⛔": "CUT"}


def resolve(path: str, last_dir: str) -> pathlib.Path | None:
    """Locate a cited file. A bare basename resolves against the directory of the previous
    citation in the same evidence string -- which is how they are written and read
    (`app/routers/authoring.py:42 ... authoring.py:97` means the same file)."""
    candidates = []
    if "/" not in path and last_dir:
        candidates.append(pathlib.Path(last_dir) / path)
    candidates.extend(pathlib.Path(root) / path if root else pathlib.Path(path)
                      for root in SEARCH_ROOTS)
    for candidate in candidates:
        absolute = ROOT / candidate
        if absolute.is_file():
            return absolute
    return None


def check_citations(feature: dict) -> list:
    problems = []
    evidence = feature.get("evidence") or ""
    last_dir = ""
    for path, lines in CITATION.findall(evidence):
        resolved = resolve(path, last_dir)
        if resolved is None:
            problems.append("{0}: cites {1}, which does not exist".format(feature["id"], path))
            continue
        last_dir = str(resolved.relative_to(ROOT).parent)
        total = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
        for line in lines.split(","):
            if int(line) > total:
                problems.append("{0}: cites {1}:{2} but the file has {3} lines".format(
                    feature["id"], path, line, total))
    return problems


def matrix_counts() -> dict:
    """The `| state | count |` summary table at the top of PARITY-MATRIX.md."""
    text = (HERE / "PARITY-MATRIX.md").read_text(encoding="utf-8")
    counts = {}
    for symbol, state in STATE_BY_SYMBOL.items():
        found = re.search(r"\|\s*" + symbol + r"\s*\w+\s*\|\s*(\d+)\s*\|", text)
        if found:
            counts[state] = int(found.group(1))
    return counts


def main() -> int:
    data = json.loads((HERE / "parity.json").read_text(encoding="utf-8"))
    problems = []

    for feature in data["features"]:
        if feature["state"] in ("ABSENT", "PARTIAL"):
            if not feature.get("phase"):
                problems.append("{0} ({1}): no owning phase".format(feature["id"], feature["name"]))
            if not feature.get("e2eFlow"):
                problems.append("{0} ({1}): no named E2E flow".format(feature["id"], feature["name"]))
        if not feature.get("evidence"):
            problems.append("{0}: no evidence cited".format(feature["id"]))
        else:
            problems.extend(check_citations(feature))

    counts = {}
    for feature in data["features"]:
        counts[feature["state"]] = counts.get(feature["state"], 0) + 1

    declared = matrix_counts()
    for state in sorted(set(counts) | set(declared)):
        if declared.get(state, 0) != counts.get(state, 0):
            problems.append(
                "PARITY-MATRIX.md says {0}={1}, parity.json has {2}. One was re-assessed and "
                "the other was not.".format(state, declared.get(state, 0), counts.get(state, 0)))

    if problems:
        print("parity matrix is inconsistent:")
        for problem in problems:
            print("  - {0}".format(problem))
        return 1

    print("parity matrix OK - {0} features, citations resolve, table agrees with JSON: {1}".format(
        len(data["features"]), ", ".join("{0}={1}".format(k, v) for k, v in sorted(counts.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
