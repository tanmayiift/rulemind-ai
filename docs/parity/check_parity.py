#!/usr/bin/env python3
"""Guard: every unmet parity feature must have an owning phase and a named E2E flow.

Without this the roadmap rots silently -- a gap loses its owner and nobody notices.
Run from the repo root: python docs/parity/check_parity.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
data = json.loads((HERE / "parity.json").read_text())

problems = []
for f in data["features"]:
    if f["state"] in ("ABSENT", "PARTIAL"):
        if not f.get("phase"):
            problems.append(f"{f['id']} ({f['name']}): no owning phase")
        if not f.get("e2eFlow"):
            problems.append(f"{f['id']} ({f['name']}): no named E2E flow")
    if not f.get("evidence"):
        problems.append(f"{f['id']}: no evidence cited")

if problems:
    print("parity.json is incomplete:")
    for p_ in problems:
        print(f"  - {p_}")
    sys.exit(1)

counts = {}
for f in data["features"]:
    counts[f["state"]] = counts.get(f["state"], 0) + 1
print(f"parity.json OK - {len(data['features'])} features: " +
      ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
