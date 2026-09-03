"""Regenerate the committed large-policy conformance fixture.

    python -m simulation.gen_large_policy_spec

Writes packages/shared/large-policy.spec.json (the shared cross-engine fixture read
by the Python, Kotlin, and Dart conformance test arms).
"""
from __future__ import annotations

import json
from pathlib import Path

from simulation.harness import build_large_policy_spec

OUT = Path(__file__).resolve().parents[3] / "packages" / "shared" / "large-policy.spec.json"


def main() -> None:
    spec = build_large_policy_spec()
    OUT.write_text(json.dumps(spec))
    outcomes: dict = {}
    trues = []
    for case in spec["cases"]:
        outcomes[case["expectedOutcome"]] = outcomes.get(case["expectedOutcome"], 0) + 1
        trues.append(case["trueConditions"])
    print(f"wrote {OUT} ({OUT.stat().st_size/1e6:.2f} MB)")
    print(f"  conditions={spec['meta']['conditions']} cases={spec['meta']['cases']} outcomes={outcomes}")
    print(f"  trueConditions {min(trues)}-{max(trues)}; cases>=500 true: {sum(1 for t in trues if t >= 500)}")

    # The trace must be internally consistent with the count it supplements, on every case,
    # or a consumer that trusts one and not the other silently disagrees with itself.
    traced = spec["cases"] + spec["thresholdCases"]
    for case in traced:
        assert len(case["conditionTrace"]) == len(spec["conditionOrder"]) or \
            len(case["conditionTrace"]) == len(spec["thresholdConditionOrder"]), "trace length"
        assert case["conditionTrace"].count("1") == case["trueConditions"], "trace/count disagree"
    print(f"  conditionTrace: {len(traced)} cases, {spec['meta']['traceLength']}/"
          f"{spec['meta']['thresholdTraceLength']} bits, all consistent with trueConditions")


if __name__ == "__main__":
    main()
