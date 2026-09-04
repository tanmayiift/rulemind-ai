"""Regenerate the committed cross-engine SCORECARD conformance fixture.

    python -m simulation.gen_scorecards_spec              # write the fixture
    python -m simulation.gen_scorecards_spec --verify     # assert the committed file still
                                                          # matches the live implementation

Writes packages/shared/scorecards.spec.json.

Python is the recorded source of truth: every `expected` block below is produced by CALLING
app.logic.evaluate_scorecard, never by hand-computing what it "should" return. The point of the
corpus is to pin what the source of truth ACTUALLY does — including the parts nobody would guess.

Why this file exists
--------------------
There was no shared scorecard corpus, so the on-device engines were free to diverge silently, and
they did: ScorecardEvaluator.kt (42 lines) and scorecard_evaluator.dart (50 lines) implement
POINTS ONLY. They ignore scoring_method, weight, coefficient, woe_values, intercept, pdo,
target_score, target_odds, formula and metrics — all of which app/logic.py:633-768 implements. A
`scoring_method: "woe"` scorecard therefore returns a different number on-device than on the
server, and until this corpus existed no test anywhere caught it.

Corpus schema
-------------
The scorecard shape here is the Python/Kotlin/Dart shape — `bins` of factors, each with `ranges`
(and optionally `woe_values`) — NOT the TypeScript `factors`/`bands` shape in
packages/rule-engine/src/decisioning.ts, which is a different schema for a different surface. The
TS engine is therefore NOT a consumer of this file.

Note: app/logic.py's evaluate_scorecard has no band/grade concept at all (the TS one does), so no
case records a band. Do not add one without adding it to logic.py first.

Regenerate with:  python -m simulation.gen_scorecards_spec
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.logic import evaluate_scorecard  # noqa: E402

OUT = Path(__file__).resolve().parents[3] / "packages" / "shared" / "scorecards.spec.json"

SEED = 20260904
FLOAT_TOLERANCE = 1e-9


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def _normalize(value: Any) -> Any:
    """JSON-safe, version-stable form of a value the implementation returned.

    A metric whose formula fails yields {"error": "<message>"}. The message for a *syntax* error
    embeds CPython's own SyntaxError text, which is not a cross-engine (or even cross-version)
    contract, so every error value is normalized to {"error": true}: the corpus pins THAT the
    formula failed, not the wording. Everything else is recorded verbatim.
    """
    if isinstance(value, dict) and "error" in value:
        return {"error": True}
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def record(result: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one live evaluate_scorecard() result into the recorded expectation."""
    breakdown = result.get("breakdown", [])
    expected: Dict[str, Any] = {
        "score": result["score"],
        "scoringMethod": result["scoring_method"],
        "baseScore": result["base_score"],
        "maxScore": result["max_score"],
        "breakdownLength": len(breakdown),
        # Derived, not returned by the implementation: the sum the points path clamps.
        "totalWeightedPoints": sum(int(entry["weighted_points"]) for entry in breakdown),
        "resultKeys": sorted(result.keys()),
        "breakdown": [
            {
                "variableId": entry["variable_id"],
                "variableName": entry["variable_name"],
                "sourceId": entry["source_id"],
                "value": entry["value"],
                "activeRange": entry["active_range"],
                "points": entry["points"],
                "weightedPoints": entry["weighted_points"],
                "weight": entry["weight"],
                "woe": entry["woe"],
                "coefficient": entry["coefficient"],
            }
            for entry in breakdown
        ],
        "hasWoeDetails": "woe_details" in result,
        "woeDetails": result.get("woe_details"),
        "informationValue": result.get("information_value"),
        "hasMetrics": "metrics" in result,
        "metrics": _normalize(result["metrics"]) if "metrics" in result else None,
    }
    return expected


CASES: List[Dict[str, Any]] = []


def case(
    name: str,
    group: str,
    scorecard: Dict[str, Any],
    variables: Dict[str, Any],
    note: str = "",
    variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Evaluate one scorecard with the live Python core and append the recorded case."""
    result = evaluate_scorecard(scorecard, variables, variable_lookup)
    entry: Dict[str, Any] = {
        "name": name,
        "group": group,
        "scorecard": scorecard,
        "variables": variables,
        "expected": record(result),
    }
    if variable_lookup:
        entry["variableLookup"] = variable_lookup
    if note:
        entry["note"] = note
    CASES.append(entry)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _points_card(**overrides: Any) -> Dict[str, Any]:
    card = {
        "base_score": 300,
        "max_score": 900,
        "bins": [
            {
                "variable_id": "bureau_score",
                "ranges": [
                    {"min": 300, "max": 579, "points": -60},
                    {"min": 580, "max": 669, "points": 0},
                    {"min": 670, "max": 739, "points": 60},
                    {"min": 740, "max": 900, "points": 140},
                ],
            },
            {
                "variable_id": "dti_ratio",
                "ranges": [
                    {"min": 0, "max": 0.2, "points": 80},
                    {"min": 0.2, "max": 0.4, "points": 30},
                    {"min": 0.4, "max": 1.0, "points": -40},
                ],
            },
            {
                "variable_id": "delinquencies_12m",
                "ranges": [
                    {"min": 0, "max": 0, "points": 50},
                    {"min": 1, "max": 2, "points": -20},
                    {"min": 3, "max": 99, "points": -90},
                ],
            },
        ],
    }
    card.update(overrides)
    return card


def _one_factor(**range_overrides: Any) -> Dict[str, Any]:
    return {
        "base_score": 300,
        "max_score": 900,
        "bins": [{"variable_id": "a", "ranges": [dict({"min": 0, "max": 10, "points": 50}, **range_overrides)]}],
    }


def _woe_card(**overrides: Any) -> Dict[str, Any]:
    card = {
        "scoring_method": "woe",
        "base_score": 300,
        "max_score": 900,
        "intercept": -0.5,
        "pdo": 20,
        "target_score": 600,
        "target_odds": 50,
        "bins": [
            {
                "variable_id": "bureau_score",
                "coefficient": 0.72,
                "woe_values": [
                    {"min": 300, "max": 579, "woe": -1.15, "iv": 0.24, "event_rate": 0.31, "non_event_rate": 0.69},
                    {"min": 580, "max": 669, "woe": -0.20, "iv": 0.03, "event_rate": 0.12, "non_event_rate": 0.88},
                    {"min": 670, "max": 739, "woe": 0.44, "iv": 0.05, "event_rate": 0.06, "non_event_rate": 0.94},
                    {"min": 740, "max": 900, "woe": 1.28, "iv": 0.19, "event_rate": 0.02, "non_event_rate": 0.98},
                ],
            },
            {
                "variable_id": "dti_ratio",
                "coefficient": 0.55,
                "woe_values": [
                    {"min": 0, "max": 0.2, "woe": 0.82, "iv": 0.11},
                    {"min": 0.2, "max": 0.4, "woe": 0.10, "iv": 0.01},
                    {"min": 0.4, "max": 1.0, "woe": -0.95, "iv": 0.16},
                ],
            },
            {
                "variable_id": "inquiries_6m",
                "coefficient": 0.31,
                "woe_values": [
                    {"min": 0, "max": 1, "woe": 0.61, "iv": 0.07},
                    {"min": 2, "max": 4, "woe": -0.14, "iv": 0.02},
                    {"min": 5, "max": 99, "woe": -0.88, "iv": 0.13},
                ],
            },
        ],
    }
    card.update(overrides)
    return card


def _formula_card(formula: Optional[str] = None, **overrides: Any) -> Dict[str, Any]:
    card: Dict[str, Any] = {
        "scoring_method": "formula",
        "base_score": 300,
        "max_score": 900,
        "bins": [
            {"variable_id": "a", "ranges": [{"min": 0, "max": 10, "points": 25}]},
            {"variable_id": "b", "ranges": [{"min": 0, "max": 100, "points": 40}]},
        ],
    }
    if formula is not None:
        card["formula"] = formula
    card.update(overrides)
    return card


# --------------------------------------------------------------------------- #
# points
# --------------------------------------------------------------------------- #
def build_points_cases() -> None:
    g = "points"
    case("points-typical-strong", g, _points_card(), {"bureau_score": 780, "dti_ratio": 0.15, "delinquencies_12m": 0})
    case("points-typical-thin", g, _points_card(), {"bureau_score": 640, "dti_ratio": 0.35, "delinquencies_12m": 1})
    case("points-typical-weak", g, _points_card(), {"bureau_score": 520, "dti_ratio": 0.62, "delinquencies_12m": 4})
    case("points-method-omitted-defaults-to-points", g, _one_factor(), {"a": 5},
         "scoring_method absent -> the else branch (points).")
    case("points-method-explicit", g, dict(_one_factor(), scoring_method="points"), {"a": 5})
    case("points-method-unknown-falls-back-to-points", g, dict(_one_factor(), scoring_method="nonsense"), {"a": 5},
         "An unrecognised scoring_method scores as points BUT is echoed verbatim in scoring_method.")

    case("points-lower-boundary-inclusive", g, _one_factor(), {"a": 0}, "min is inclusive.")
    case("points-upper-boundary-inclusive", g, _one_factor(), {"a": 10}, "max is inclusive (TS uses a half-open range).")
    case("points-below-every-bin", g, _one_factor(min=10, max=20), {"a": 1}, "No range matches -> 0 points, active_range null.")
    case("points-above-every-bin", g, _one_factor(min=10, max=20), {"a": 99})
    case("points-overlapping-ranges-first-wins", g, {
        "base_score": 0, "max_score": 900,
        "bins": [{"variable_id": "a", "ranges": [{"min": 0, "max": 100, "points": 10}, {"min": 0, "max": 10, "points": 99}]}],
    }, {"a": 5}, "Range scan breaks on the FIRST match, in list order — not the narrowest.")

    case("points-missing-variable-defaults-to-zero", g, _one_factor(), {},
         "A MISSING variable becomes 0 and can therefore MATCH a bin containing 0 and earn points.")
    case("points-missing-variable-no-zero-bin", g, _one_factor(min=10, max=20), {},
         "Missing -> 0, which matches nothing here.")
    case("points-null-value-matches-nothing", g, _one_factor(), {"a": None},
         "An explicit null is NOT the missing-variable default: float(None) raises, so no range "
         "matches and the raw null is kept in the breakdown. Kotlin/Dart coerce null to 0.0 and "
         "would match [0,10] here — a real divergence.")
    case("points-non-numeric-string-matches-nothing", g, _one_factor(), {"a": "abc"},
         "float('abc') raises -> no range matches, raw string kept. Kotlin/Dart coerce to 0.0 and match.")
    case("points-numeric-string-matches", g, _one_factor(), {"a": "5"}, "Numeric strings are coerced by float().")
    case("points-numeric-string-float", g, _one_factor(), {"a": "5.5"})
    case("points-bool-true-is-one", g, _one_factor(), {"a": True}, "float(True) == 1.0.")
    case("points-bool-false-is-zero", g, _one_factor(), {"a": False})

    case("points-range-without-min-or-max", g, {
        "base_score": 300, "max_score": 900, "bins": [{"variable_id": "a", "ranges": [{"points": 50}]}],
    }, {"a": 0}, "A range with NO min/max defaults BOTH to 0 — it matches only the value 0. "
                 "Kotlin/Dart substitute -inf/+inf for a null bound, so it matches everything there.")
    case("points-range-without-min-or-max-nonzero-value", g, {
        "base_score": 300, "max_score": 900, "bins": [{"variable_id": "a", "ranges": [{"points": 50}]}],
    }, {"a": 5})
    case("points-range-with-null-min", g, {
        "base_score": 300, "max_score": 900, "bins": [{"variable_id": "a", "ranges": [{"min": None, "max": 10, "points": 50}]}],
    }, {"a": 5}, "float(None) raises inside the scan -> the range is SKIPPED, not treated as open-ended.")
    case("points-range-with-string-bounds", g, {
        "base_score": 300, "max_score": 900, "bins": [{"variable_id": "a", "ranges": [{"min": "0", "max": "10", "points": 25}]}],
    }, {"a": 5})

    case("points-float-points-truncated", g, _one_factor(points=7.9), {"a": 5}, "int() truncates toward zero: 7.9 -> 7.")
    case("points-negative-float-points-truncated", g, _one_factor(points=-7.9), {"a": 5}, "-7.9 -> -7, toward zero.")
    case("points-string-points", g, _one_factor(points="25"), {"a": 5})
    case("points-factor-without-ranges-key", g, {"base_score": 300, "max_score": 900, "bins": [{"variable_id": "a"}]}, {"a": 5})
    case("points-factor-with-empty-ranges", g, {"base_score": 300, "max_score": 900, "bins": [{"variable_id": "a", "ranges": []}]}, {"a": 5})
    case("points-factor-without-variable-id", g, {
        "base_score": 300, "max_score": 900, "bins": [{"ranges": [{"min": 0, "max": 10, "points": 40}]}],
    }, {"a": 5}, "variable_id absent -> lookup key None -> value defaults to 0 and matches [0,10].")

    case("points-empty-bins", g, {"base_score": 300, "max_score": 900, "bins": []}, {},
         "Empty factor list: score is base_score, breakdown empty.")
    case("points-bins-key-absent", g, {"base_score": 425, "max_score": 900}, {})
    case("points-clamped-to-max-score", g, {
        "base_score": 800, "max_score": 900, "bins": [{"variable_id": "a", "ranges": [{"min": 0, "max": 10, "points": 500}]}],
    }, {"a": 5}, "score = clamp(base + total, 0, max_score).")
    case("points-clamped-to-zero", g, {
        "base_score": 100, "max_score": 900, "bins": [{"variable_id": "a", "ranges": [{"min": 0, "max": 10, "points": -500}]}],
    }, {"a": 5}, "The lower clamp is 0, NOT base_score.")
    case("points-base-score-float-truncated", g, {"base_score": 300.9, "max_score": 900.9, "bins": []}, {},
         "int() on base_score/max_score truncates.")
    case("points-base-score-string", g, {"base_score": "300", "max_score": "900", "bins": []}, {})
    case("points-base-score-defaults", g, {"bins": [{"variable_id": "a", "ranges": [{"min": 0, "max": 10, "points": 25}]}]}, {"a": 5},
         "base_score defaults to 300 and max_score to 900.")
    case("points-with-variable-lookup", g, _one_factor(), {"a": 5},
         "variable_lookup fills variable_name/source_id; without it both fall back to the id / null.",
         variable_lookup={"a": {"name": "Applicant Age", "source_id": "src_kyc"}})


# --------------------------------------------------------------------------- #
# weighted points
# --------------------------------------------------------------------------- #
def build_weighted_cases() -> None:
    g = "weighted"
    case("weighted-double", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": 2.0, "ranges": [{"min": 0, "max": 10, "points": 40}]}],
    }, {"a": 5})
    case("weighted-half-truncates", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": 0.5, "ranges": [{"min": 0, "max": 10, "points": 3}]}],
    }, {"a": 5}, "weighted_points = int(points * weight): 1.5 -> 1. TRUNCATION, not rounding.")
    case("weighted-half-negative-truncates-toward-zero", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": 0.5, "ranges": [{"min": 0, "max": 10, "points": -3}]}],
    }, {"a": 5}, "-1.5 -> -1 (toward zero), NOT -2. floor() would give -2.")
    case("weighted-1point5-truncates", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": 1.5, "ranges": [{"min": 0, "max": 10, "points": 7}]}],
    }, {"a": 5}, "10.5 -> 10.")
    case("weighted-zero", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": 0, "ranges": [{"min": 0, "max": 10, "points": 77}]}],
    }, {"a": 5}, "points is still recorded; only weighted_points is zeroed.")
    case("weighted-string-weight", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": "1.5", "ranges": [{"min": 0, "max": 10, "points": 7}]}],
    }, {"a": 5})
    case("weighted-negative-weight", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": -1.0, "ranges": [{"min": 0, "max": 10, "points": 40}]}],
    }, {"a": 5})
    # Negative weights were covered by exactly ONE case, and they are where Python's int()
    # (truncate toward zero) and a naive Math.floor port disagree: int(-3.5) is -3, floor is -4.
    # A single case can pass by luck on a value that happens to be whole.
    case("weighted-negative-fractional-truncates-toward-zero", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": -0.5, "ranges": [{"min": 0, "max": 10, "points": 7}]}],
    }, {"a": 5}, "int(7 * -0.5) = int(-3.5) = -3, not -4. Math.floor gives -4.")
    case("weighted-negative-weight-negative-points", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": -1.5, "ranges": [{"min": 0, "max": 10, "points": -5}]}],
    }, {"a": 5}, "int(-5 * -1.5) = int(7.5) = 7. Two negatives, so the sign flips back.")
    case("weighted-negative-drives-score-below-zero", g, {
        "base_score": 100, "max_score": 900,
        "bins": [{"variable_id": "a", "weight": -1.0, "ranges": [{"min": 0, "max": 10, "points": 400}]}],
    }, {"a": 5}, "100 - 400 clamps at 0, the LOWER bound. Only max_score clamping was ported.")
    case("weighted-per-factor-truncation-not-sum-then-truncate", g, {
        "base_score": 300, "max_score": 900,
        "bins": [
            {"variable_id": "a", "weight": 0.5, "ranges": [{"min": 0, "max": 10, "points": 3}]},
            {"variable_id": "b", "weight": 0.5, "ranges": [{"min": 0, "max": 10, "points": 3}]},
            {"variable_id": "c", "weight": 0.5, "ranges": [{"min": 0, "max": 10, "points": 3}]},
        ],
    }, {"a": 5, "b": 5, "c": 5},
       "Each factor is truncated INDEPENDENTLY: 3*int(1.5) = 3, not int(3*1.5) = 4. An engine that "
       "sums in floating point and rounds once at the end gets a different score.")
    case("weighted-mixed-weights-realistic", g, _points_card(bins=[
        dict(_points_card()["bins"][0], weight=1.5),
        dict(_points_card()["bins"][1], weight=0.75),
        dict(_points_card()["bins"][2], weight=2.0),
    ]), {"bureau_score": 700, "dti_ratio": 0.31, "delinquencies_12m": 1})
    case("weighted-clamped-to-max", g, {
        "base_score": 500, "max_score": 700,
        "bins": [{"variable_id": "a", "weight": 10.0, "ranges": [{"min": 0, "max": 10, "points": 100}]}],
    }, {"a": 5})


# --------------------------------------------------------------------------- #
# WoE
# --------------------------------------------------------------------------- #
def build_woe_cases() -> None:
    g = "woe"
    case("woe-typical-strong", g, _woe_card(), {"bureau_score": 780, "dti_ratio": 0.12, "inquiries_6m": 0})
    case("woe-typical-mid", g, _woe_card(), {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3})
    case("woe-typical-weak", g, _woe_card(), {"bureau_score": 540, "dti_ratio": 0.55, "inquiries_6m": 7})
    case("woe-single-factor", g, {
        "scoring_method": "woe", "max_score": 900, "intercept": -0.5, "pdo": 20,
        "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 0.7,
                  "woe_values": [{"min": 0, "max": 10, "woe": 1.2, "iv": 0.1,
                                  "event_rate": 0.2, "non_event_rate": 0.8}]}],
    }, {"a": 5}, "score = round(offset + (pdo/ln2) * (intercept + sum(coef*woe))), "
                 "offset = target_score - (pdo/ln2)*ln(target_odds).")
    case("woe-defaults-for-every-parameter", g, {"scoring_method": "woe", "bins": []}, {},
         "intercept 0, pdo 20, target_score 600, target_odds 50 — an empty WoE card still scores 487.")
    case("woe-no-range-matches-omits-woe-keys", g, _woe_card(), {"bureau_score": 1500, "dti_ratio": 9, "inquiries_6m": -1},
         "woe_details and information_value are ONLY present when at least one WoE range matched. "
         "A woe scorecard can therefore return NEITHER key.")
    case("woe-partial-match", g, _woe_card(), {"bureau_score": 700},
         "Unmatched factors contribute woe 0.0 and are absent from woe_details, but STILL appear in breakdown.")
    case("woe-missing-variable-defaults-to-zero", g, {
        "scoring_method": "woe", "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [{"min": 0, "max": 10, "woe": 0.5, "iv": 0.02}]}],
    }, {})
    case("woe-null-value-matches-nothing", g, {
        "scoring_method": "woe", "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [{"min": 0, "max": 10, "woe": 0.5, "iv": 0.02}]}],
    }, {"a": None})
    case("woe-boundary-max-inclusive-first-wins", g, {
        "scoring_method": "woe", "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [
            {"min": 0, "max": 10, "woe": 0.5, "iv": 0.02},
            {"min": 10, "max": 20, "woe": -0.5, "iv": 0.03},
        ]}],
    }, {"a": 10}, "WoE bounds are CLOSED on both ends (min <= v <= max) and the first match wins, so "
                  "v == 10 takes the FIRST bin. TypeScript uses >= min && < max and takes the second.")
    case("woe-overlapping-ranges-first-wins", g, {
        "scoring_method": "woe", "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [
            {"min": 0, "max": 100, "woe": 0.1, "iv": 0.01},
            {"min": 0, "max": 10, "woe": 2.0, "iv": 0.5},
        ]}],
    }, {"a": 5})
    case("woe-missing-iv-key-defaults-zero", g, {
        "scoring_method": "woe", "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [{"min": 0, "max": 10, "woe": 0.5}]}],
    }, {"a": 5}, "iv absent -> 0.0, but information_value is still emitted (as 0.0).")
    case("woe-iv-accumulates-across-factors", g, _woe_card(), {"bureau_score": 350, "dti_ratio": 0.9, "inquiries_6m": 9},
         "information_value is the SUM of matched-range iv over all factors.")
    case("woe-missing-coefficient-defaults-zero", g, {
        "scoring_method": "woe", "intercept": 0.4, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "woe_values": [{"min": 0, "max": 10, "woe": 9.9, "iv": 0.4}]}],
    }, {"a": 5}, "coefficient absent -> 0.0, so a matched WoE contributes NOTHING to the score, "
                 "yet still appears in woe_details and still adds its iv.")

    case("woe-pdo-zero-uses-factor-one", g, _woe_card(pdo=0), {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3},
         "pdo <= 0 -> the scaling factor becomes 1, NOT pdo/ln(2). TS always divides.")
    case("woe-pdo-negative-uses-factor-one", g, _woe_card(pdo=-20), {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3})
    case("woe-target-odds-zero-drops-log-term", g, _woe_card(target_odds=0), {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3},
         "target_odds <= 0 -> offset = target_score, the ln(target_odds) term is dropped entirely. "
         "TS computes Math.log(0) = -Infinity here.")
    case("woe-target-odds-negative-drops-log-term", g, _woe_card(target_odds=-50), {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3})
    case("woe-target-odds-one-log-is-zero", g, _woe_card(target_odds=1), {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3})
    case("woe-pdo-40", g, _woe_card(pdo=40), {"bureau_score": 780, "dti_ratio": 0.12, "inquiries_6m": 0})
    case("woe-target-score-850", g, _woe_card(target_score=850, target_odds=20), {"bureau_score": 780, "dti_ratio": 0.12, "inquiries_6m": 0})
    case("woe-string-parameters", g, _woe_card(intercept="-0.5", pdo="20", target_score="600", target_odds="50"),
         {"bureau_score": 700, "dti_ratio": 0.3, "inquiries_6m": 3}, "float() coerces string parameters.")

    # Banker's rounding: with pdo <= 0 the factor is exactly 1 and with target_odds <= 0 the offset
    # is exactly target_score, so raw_score = target_score + intercept — an exact .5 is reachable.
    for intercept, tag in [(0.5, "half-down-to-even"), (1.5, "half-up-to-even"),
                           (2.5, "half-down-to-even-again"), (-0.5, "negative-half-to-even"),
                           (-1.5, "negative-half-away")]:
        case(f"woe-rounding-{tag}-{str(intercept).replace('.', 'p').replace('-', 'neg')}", g, {
            "scoring_method": "woe", "max_score": 900, "pdo": 0, "target_odds": 0,
            "target_score": 600, "intercept": intercept, "bins": [],
        }, {}, "Python's round() is BANKER'S rounding (half to EVEN). Kotlin roundToInt() is half-up "
               "and Dart num.round() is half-away-from-zero; all three disagree on an exact .5.")

    case("woe-clamped-to-max-score", g, {
        "scoring_method": "woe", "max_score": 700, "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 50.0, "woe_values": [{"min": 0, "max": 10, "woe": 5.0, "iv": 0.9}]}],
    }, {"a": 5}, "The WoE path clamps to [0, max_score] too — max_score is not a points-only concept.")
    case("woe-clamped-to-zero", g, {
        "scoring_method": "woe", "max_score": 900, "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 50.0, "woe_values": [{"min": 0, "max": 10, "woe": -5.0, "iv": 0.9}]}],
    }, {"a": 5})
    case("woe-factor-also-has-points-ranges", g, {
        "scoring_method": "woe", "max_score": 900, "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "weight": 2.0,
                  "ranges": [{"min": 0, "max": 10, "points": 40}],
                  "woe_values": [{"min": 0, "max": 10, "woe": 0.5, "iv": 0.02}]}],
    }, {"a": 5}, "Points are still computed and recorded in the breakdown under the woe method — they "
                 "just do not reach the score.")
    case("woe-empty-bins-scores-from-intercept", g, {
        "scoring_method": "woe", "max_score": 900, "intercept": 1.25, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [],
    }, {})
    case("woe-with-variable-lookup", g, {
        "scoring_method": "woe", "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [{"min": 0, "max": 10, "woe": 0.5, "iv": 0.02}]}],
    }, {"a": 5}, "woe_details carries variable_name from the lookup.",
       variable_lookup={"a": {"name": "Applicant Age", "source_id": "src_kyc"}})

    # The surprise: WoE aggregation runs for EVERY scoring method.
    case("points-method-still-emits-woe-details", "points-woe-crossover", {
        "scoring_method": "points", "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "coefficient": 1.5,
                  "ranges": [{"min": 0, "max": 10, "points": 20}],
                  "woe_values": [{"min": 0, "max": 10, "woe": 0.8, "iv": 0.05}]}],
    }, {"a": 5}, "The WoE scan runs for EVERY scoring_method, so a POINTS scorecard that carries "
                 "woe_values returns woe_details and information_value alongside a points score.")
    case("formula-method-still-emits-woe-details", "points-woe-crossover", {
        "scoring_method": "formula", "base_score": 300, "max_score": 900,
        "formula": "base_score + total_points",
        "bins": [{"variable_id": "a", "coefficient": 1.5,
                  "ranges": [{"min": 0, "max": 10, "points": 20}],
                  "woe_values": [{"min": 0, "max": 10, "woe": 0.8, "iv": 0.05}]}],
    }, {"a": 5})


# --------------------------------------------------------------------------- #
# formula
# --------------------------------------------------------------------------- #
def build_formula_cases() -> None:
    g = "formula"
    case("formula-base-plus-points", g, _formula_card("base_score + total_points"), {"a": 5, "b": 50})
    case("formula-omitted-uses-default", g, _formula_card(), {"a": 5, "b": 50},
         "formula absent -> the default 'base_score + total_points'.")
    case("formula-scaled-points", g, _formula_card("base_score + total_points * 2"), {"a": 5, "b": 50})
    case("formula-uses-max-score", g, _formula_card("max_score - 100"), {"a": 5, "b": 50})
    case("formula-uses-total-woe-score", g, {
        "scoring_method": "formula", "base_score": 300, "max_score": 900,
        "formula": "base_score + total_woe_score * 100",
        "bins": [{"variable_id": "a", "coefficient": 2.0, "woe_values": [{"min": 0, "max": 10, "woe": 0.75, "iv": 0.1}]}],
    }, {"a": 5}, "total_woe_score IS in the formula context.")
    case("formula-uses-variable-id-as-name", g, _formula_card("a * 10 + b"), {"a": 5, "b": 50},
         "Every breakdown entry's variable_id is bound to its RAW value in the formula scope.")
    case("formula-excel-min-max", g, _formula_card("MIN(MAX(base_score + total_points, 0), 850)"), {"a": 5, "b": 50})
    case("formula-excel-if", g, _formula_card("IF(total_points > 10, 800, 400)"), {"a": 5, "b": 50})
    case("formula-excel-sum-list", g, _formula_card("SUM([a, b, total_points])"), {"a": 5, "b": 50},
         "List literals are allowed so Excel aggregates can be called.")
    case("formula-builtin-round", g, _formula_card("round(base_score + total_points * 1.5)"), {"a": 5, "b": 50})
    case("formula-ternary", g, _formula_card("800 if total_points > 10 else 400"), {"a": 5, "b": 50})
    case("formula-boolean-result-is-one", g, _formula_card("total_points > 0"), {"a": 5, "b": 50},
         "isinstance(True, int) is True in Python, so a boolean formula scores 1 (or 0) — it does NOT "
         "fall back to base_score.")
    case("formula-boolean-false-is-zero", g, _formula_card("total_points > 9999"), {"a": 5, "b": 50})
    case("formula-float-truncated-not-rounded", g, _formula_card("300.9"), {"a": 5, "b": 50}, "int(300.9) -> 300.")
    case("formula-negative-float-truncated-then-clamped", g, _formula_card("0 - 300.9"), {"a": 5, "b": 50})
    case("formula-clamped-to-max", g, _formula_card("99999"), {"a": 5, "b": 50})
    case("formula-clamped-to-zero", g, _formula_card("0 - 500"), {"a": 5, "b": 50})

    case("formula-unknown-name-falls-back-to-base-score", g, _formula_card("nope + 1"), {"a": 5, "b": 50},
         "A FormulaError yields {'error': ...}, which is not int/float, so score = clamp(base_score).")
    case("formula-syntax-error-falls-back", g, _formula_card("1 +"), {"a": 5, "b": 50})
    case("formula-string-result-falls-back", g, _formula_card("'hello'"), {"a": 5, "b": 50},
         "A non-numeric result falls back to base_score.")
    case("formula-score-name-not-in-context", g, _formula_card("score"), {"a": 5, "b": 50},
         "`score` is NOT in the formula context (it is being computed) — only the METRIC context has it.")
    case("formula-total-iv-not-in-context", g, _formula_card("total_iv"), {"a": 5, "b": 50},
         "`total_iv` is NOT in the formula context either, though it IS in the metric context. "
         "The two contexts are deliberately different.")
    case("formula-attribute-access-rejected", g, _formula_card("base_score.real"), {"a": 5, "b": 50},
         "safe_formula (the allowlisting AST evaluator that replaced eval) refuses attribute access.")
    case("formula-lambda-rejected", g, _formula_card("(lambda: 1)()"), {"a": 5, "b": 50})
    case("formula-subscript-rejected", g, _formula_card("variables['a']"), {"a": 5, "b": 50},
         "`variables` IS bound in the context, but safe_formula allows no Subscript node, so indexing "
         "it is refused and the score falls back to base_score. This is a BEHAVIOUR CHANGE from the "
         "old eval()-based evaluator, where variables['a'] worked.")
    case("formula-unknown-function-rejected", g, _formula_card("NOPE(1)"), {"a": 5, "b": 50})
    # The error path had one case. A formula that RAISES must fall back to base_score -- not to
    # 0, and not out of the function -- and the ways it can raise are different code paths:
    # a runtime error inside a legal expression, a parse failure, and a name that resolves to
    # nothing. A port that returns 0 on any of them scores every applicant at the floor.
    case("formula-division-by-zero-falls-back-to-base", g, _formula_card("total_points / 0"),
         {"a": 5, "b": 50}, "ZeroDivisionError -> base_score, not 0 and not a raise.")
    case("formula-syntax-error-falls-back-to-base", g, _formula_card("base_score +"),
         {"a": 5, "b": 50}, "unparseable -> base_score.")
    case("formula-undefined-name-falls-back-to-base", g, _formula_card("no_such_variable + 1"),
         {"a": 5, "b": 50}, "unbound name -> base_score.")
    case("formula-negative-result-clamps-to-zero", g, _formula_card("0 - 500"),
         {"a": 5, "b": 50}, "the LOWER clamp, which several ports omitted entirely.")
    case("formula-empty-bins", g, {
        "scoring_method": "formula", "base_score": 300, "max_score": 900, "formula": "base_score + 55", "bins": [],
    }, {})


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def build_metric_cases() -> None:
    g = "metrics"
    base_bins = [{"variable_id": "a", "ranges": [{"min": 0, "max": 10, "points": 25}]}]
    case("metrics-single", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins,
        "metrics": [{"name": "utilisation", "formula": "score / max_score", "unit": "ratio", "category": "risk"}],
    }, {"a": 5})
    case("metrics-multiple", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins,
        "metrics": [
            {"name": "headroom", "formula": "max_score - score", "unit": "points", "category": "risk"},
            {"name": "lift", "formula": "score - base_score", "unit": "points", "category": "financial"},
            {"name": "pts", "formula": "total_points", "unit": "points", "category": "financial"},
        ],
    }, {"a": 5})
    case("metrics-defaults", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins, "metrics": [{}],
    }, {"a": 5}, "name defaults to 'unnamed', formula to '0', unit to '' and category to 'financial'.")
    case("metrics-duplicate-name-last-wins", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins,
        "metrics": [{"name": "x", "formula": "1"}, {"name": "x", "formula": "2"}],
    }, {"a": 5}, "metrics is a dict keyed by name, so a duplicate name silently overwrites.")
    case("metrics-empty-list-omits-key", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins, "metrics": [],
    }, {"a": 5}, "An empty metrics list means NO metrics key in the result at all.")
    case("metrics-uses-score", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins, "metrics": [{"name": "s", "formula": "score"}],
    }, {"a": 5}, "The metric context DOES bind `score` (the already-computed one).")
    case("metrics-uses-total-iv", g, {
        "base_score": 300, "max_score": 900,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "ranges": [{"min": 0, "max": 10, "points": 25}],
                  "woe_values": [{"min": 0, "max": 10, "woe": 0.5, "iv": 0.33}]}],
        "metrics": [{"name": "iv", "formula": "total_iv"}],
    }, {"a": 5}, "The metric context also binds total_iv, which the formula context does not.")
    case("metrics-uses-variable-id", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins, "metrics": [{"name": "v", "formula": "a * 2"}],
    }, {"a": 5})
    case("metrics-error-value", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins,
        "metrics": [{"name": "bad", "formula": "nope + 1"}, {"name": "good", "formula": "score"}],
    }, {"a": 5}, "A failing metric does NOT abort the scorecard: its value becomes an error object and "
                 "the sibling metrics still resolve.")
    case("metrics-on-woe-scorecard", g, {
        "scoring_method": "woe", "max_score": 900, "intercept": 0, "pdo": 20, "target_score": 600, "target_odds": 50,
        "bins": [{"variable_id": "a", "coefficient": 1.0, "woe_values": [{"min": 0, "max": 10, "woe": 0.5, "iv": 0.02}]}],
        "metrics": [{"name": "s", "formula": "score"}, {"name": "iv", "formula": "total_iv"}],
    }, {"a": 5}, "Metrics run for every scoring method, over the already-clamped score.")
    case("metrics-on-formula-scorecard", g, dict(
        _formula_card("base_score + total_points"),
        metrics=[{"name": "s", "formula": "score / 2"}],
    ), {"a": 5, "b": 50})
    case("metrics-boolean-value", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins,
        "metrics": [{"name": "eligible", "formula": "score >= 320", "unit": "bool", "category": "eligibility"}],
    }, {"a": 5})
    case("metrics-string-value", g, {
        "base_score": 300, "max_score": 900, "bins": base_bins,
        "metrics": [{"name": "tier", "formula": "IF(score > 320, 'A', 'B')", "unit": "", "category": "risk"}],
    }, {"a": 5}, "A metric value is whatever the formula returns — it is not coerced to a number.")


# --------------------------------------------------------------------------- #
# Randomised bulk coverage
# --------------------------------------------------------------------------- #
_BULK_VALUES: List[Any] = [0, 1, 5, 9, 10, 11, 250, 580, 669, 670, 739, 740, 900,
                           0.0, 0.19, 0.2, 0.4, 0.75, "5", "740", "n/a", True, False, None]


def build_bulk_cases(count: int = 45) -> None:
    """A seeded sweep so no engine passes the corpus by special-casing the named cases."""
    rng = random.Random(SEED)
    methods = ["points", "points", "weighted", "woe", "woe", "formula"]
    formulas = ["base_score + total_points", "base_score + total_points * 2",
                "MIN(base_score + total_points, 800)", "IF(total_points > 0, 700, 350)",
                "max_score - total_points", "round(base_score + total_points / 3)"]
    for i in range(count):
        method = methods[i % len(methods)]
        num_factors = rng.randint(1, 4)
        bins: List[Dict[str, Any]] = []
        variables: Dict[str, Any] = {}
        for f in range(num_factors):
            var = f"v{f}"
            factor: Dict[str, Any] = {"variable_id": var}
            lo = rng.choice([0, 0, 10, 300, 580])
            width = rng.choice([9, 50, 89, 160])
            factor["ranges"] = [
                {"min": lo, "max": lo + width, "points": rng.choice([-90, -40, -20, 0, 15, 25, 50, 60, 140])},
                {"min": lo + width + 1, "max": lo + width + 400, "points": rng.choice([-30, 0, 20, 80])},
            ]
            if method == "weighted":
                factor["weight"] = rng.choice([0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0])
            if method in ("woe", "formula"):
                factor["coefficient"] = round(rng.uniform(-1.5, 1.5), 3)
                factor["woe_values"] = [
                    {"min": lo, "max": lo + width, "woe": round(rng.uniform(-1.5, 1.5), 3), "iv": round(rng.uniform(0, 0.3), 3)},
                    {"min": lo + width + 1, "max": lo + width + 400, "woe": round(rng.uniform(-1.5, 1.5), 3), "iv": round(rng.uniform(0, 0.3), 3)},
                ]
            bins.append(factor)
            value = rng.choice(_BULK_VALUES)
            if value is not None or rng.random() < 0.5:  # sometimes omit the variable entirely
                variables[var] = value

        card: Dict[str, Any] = {
            "base_score": rng.choice([0, 100, 300, 500]),
            "max_score": rng.choice([700, 850, 900, 1000]),
            "bins": bins,
        }
        if method == "woe":
            card["scoring_method"] = "woe"
            card["intercept"] = round(rng.uniform(-1.0, 1.0), 3)
            card["pdo"] = rng.choice([0, 10, 20, 40])
            card["target_score"] = rng.choice([500, 600, 700])
            card["target_odds"] = rng.choice([0, 1, 20, 50])
        elif method == "formula":
            card["scoring_method"] = "formula"
            card["formula"] = rng.choice(formulas)
        elif method == "weighted":
            card["scoring_method"] = "points"
        if rng.random() < 0.35:
            card["metrics"] = [{"name": "headroom", "formula": "max_score - score", "unit": "points", "category": "risk"}]
        case(f"bulk-{method}-{i:03d}", f"bulk-{method}", card, variables)


# --------------------------------------------------------------------------- #
# Spec assembly
# --------------------------------------------------------------------------- #
_COMMENT = (
    "Cross-engine SCORECARD conformance. The Python core (app/logic.py evaluate_scorecard) is the "
    "source of truth; every `expected` block was produced by CALLING it, not by hand. Arms that "
    "consume this file MUST reproduce `expected.score` exactly for every case, and (where they "
    "implement the feature) the breakdown, woe_details, information_value and metrics. "
    "Scorecard shape is the Python/Kotlin/Dart shape (`bins` of factors with `ranges` and optional "
    "`woe_values`), NOT the TypeScript `factors`/`bands` shape in packages/rule-engine. "
    "evaluate_scorecard has no band/grade concept, so no case records one. `score` is always an "
    "integer, so it is an EXACT assertion; floats (woe, contribution, information_value, metric "
    "values) compare within meta.floatTolerance. A metric whose formula fails is recorded as "
    "{\"error\": true} because the real message embeds CPython's SyntaxError text. "
    "Regenerate with: python -m simulation.gen_scorecards_spec"
)

_CONTRACT = [
    "score: exact integer match, required of every engine.",
    "scoringMethod: echoed verbatim from the input, including unrecognised values.",
    "breakdown[].points: int(range.points), truncated toward zero.",
    "breakdown[].weightedPoints: int(points * weight) — truncated PER FACTOR, never summed first.",
    "Range/WoE bounds are closed on both ends (min <= v <= max) and the FIRST match wins.",
    "A bound that is absent defaults to 0; a bound that is null (or any non-numeric) SKIPS the range.",
    "A missing variable becomes 0; an explicit null or a non-numeric string matches no range at all.",
    "woe score = clamp(round(offset + factor * (intercept + sum(coef*woe))), 0, max_score), "
    "factor = pdo/ln(2) when pdo > 0 else 1, offset = target_score - factor*ln(target_odds) when "
    "target_odds > 0 else target_score. round() is banker's rounding (half to even).",
    "formula score = clamp(int(result), 0, max_score); a non-numeric result falls back to base_score. "
    "bool IS numeric here (int(True) == 1).",
    "woe_details/information_value appear whenever ANY WoE range matched — for EVERY scoring method, "
    "not only 'woe' — and are absent entirely when none matched.",
    "metrics appear only when the metrics list is non-empty; the metric context binds score and "
    "total_iv, the formula context binds neither.",
]


def build_scorecards_spec() -> Dict[str, Any]:
    CASES.clear()
    build_points_cases()
    build_weighted_cases()
    build_woe_cases()
    build_formula_cases()
    build_metric_cases()
    build_bulk_cases()

    by_group: Dict[str, int] = {}
    by_method: Dict[str, int] = {}
    for entry in CASES:
        by_group[entry["group"]] = by_group.get(entry["group"], 0) + 1
        method = entry["expected"]["scoringMethod"]
        by_method[method] = by_method.get(method, 0) + 1

    return {
        "_comment": _COMMENT,
        "version": 1,
        "meta": {
            "cases": len(CASES),
            "casesByGroup": dict(sorted(by_group.items())),
            "casesByScoringMethod": dict(sorted(by_method.items())),
            "floatTolerance": FLOAT_TOLERANCE,
            "seed": SEED,
            "source": "apps/python-executor/app/logic.py evaluate_scorecard",
            "generator": "apps/python-executor/simulation/gen_scorecards_spec.py",
            "consumers": [
                "apps/python-executor/tests/test_scorecard_conformance.py",
                "packages/sdk-android/rulemind-core/src/test/kotlin/com/rulemind/core/ScorecardConformanceTest.kt (to add)",
                "packages/sdk-flutter/test/scorecard_conformance_test.dart (to add)",
            ],
            "contract": _CONTRACT,
        },
        "cases": CASES,
    }


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _values_match(recorded: Any, live: Any) -> bool:
    if isinstance(recorded, bool) or isinstance(live, bool):
        return recorded is live
    if isinstance(recorded, (int, float)) and isinstance(live, (int, float)):
        return math.isclose(float(recorded), float(live), rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE)
    if isinstance(recorded, dict) and isinstance(live, dict):
        if recorded.get("error") is True:
            return "error" in live
        return set(recorded) == set(live) and all(_values_match(recorded[k], live[k]) for k in recorded)
    if isinstance(recorded, list) and isinstance(live, list):
        return len(recorded) == len(live) and all(_values_match(a, b) for a, b in zip(recorded, live))
    return recorded == live


def verify(spec: Dict[str, Any]) -> List[str]:
    """Re-run every recorded case against the LIVE implementation. Returns failure messages."""
    failures: List[str] = []
    for entry in spec["cases"]:
        live = evaluate_scorecard(entry["scorecard"], entry["variables"], entry.get("variableLookup"))
        expected = entry["expected"]
        actual = record(live)
        if actual["score"] != expected["score"]:
            failures.append(f"{entry['name']}: score {expected['score']} -> {actual['score']}")
            continue
        for field in ("scoringMethod", "baseScore", "maxScore", "breakdownLength",
                      "totalWeightedPoints", "resultKeys", "hasWoeDetails", "hasMetrics"):
            if actual[field] != expected[field]:
                failures.append(f"{entry['name']}: {field} {expected[field]!r} -> {actual[field]!r}")
        for field in ("breakdown", "woeDetails", "informationValue", "metrics"):
            if not _values_match(expected[field], actual[field]):
                failures.append(f"{entry['name']}: {field} drifted")
    return failures


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="assert the committed fixture still matches the live app/logic.py, and "
                             "that regeneration is byte-identical; write nothing")
    args = parser.parse_args()

    spec = build_scorecards_spec()
    # Compact + sorted keys, matching the other committed corpora (operators/large-policy are both
    # single-line). sort_keys makes regeneration byte-stable regardless of construction order.
    payload = json.dumps(spec, sort_keys=True, separators=(",", ":"))

    if args.verify:
        if not OUT.exists():
            print(f"FAIL: {OUT} does not exist — run without --verify first")
            return 1
        committed = json.loads(OUT.read_text())
        failures = verify(committed)
        if OUT.read_text() != payload:
            failures.append("regenerated output differs from the committed file (stale or non-deterministic)")
        if failures:
            print(f"FAIL: {len(failures)} drift(s) against the live implementation")
            for message in failures:
                print(f"  - {message}")
            return 1
        print(f"OK: {len(committed['cases'])} cases still match app/logic.py; file is byte-identical")
        return 0

    OUT.write_text(payload)
    meta = spec["meta"]
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"  cases={meta['cases']}")
    print(f"  by group:  {meta['casesByGroup']}")
    print(f"  by method: {meta['casesByScoringMethod']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
