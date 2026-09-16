"""Population Stability Index — the drift metric for parity row P20.

PSI, not KL divergence or a KS test, because this is a credit decisioning product and PSI is what
a credit risk team already reads. Its thresholds are conventional and shared across the industry,
so a number produced here means the same thing in a model risk committee as it does in any other
scorecard monitoring pack:

    PSI < 0.10   stable
    0.10 - 0.25  moderate shift, investigate
    > 0.25       significant shift, the population has moved

    PSI = sum over bins of  (actual% - expected%) * ln(actual% / expected%)

WHY THE ZERO HANDLING IS THE WHOLE IMPLEMENTATION. The formula divides by the expected proportion
and takes a log of the ratio, so a bin that is empty in either period is a division by zero or a
log of zero, and the naive fix -- skipping empty bins -- is WRONG in the direction that matters:
a bin that was 20% of the baseline and is now 0% is the strongest possible drift signal, and
dropping it reports stability. So empty bins are floored at a small epsilon rather than skipped,
which is the standard treatment and keeps such a bin contributing a large term.

Bins come from the BASELINE, not from the combined data. Recomputing edges per comparison would
make each period's PSI incomparable with the last, which defeats the point of tracking it over
time.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

# Floor for an empty bin's proportion. 1e-6 rather than something larger because it has to be
# small enough that a vanished bin produces a visibly large term, and large enough that the log
# does not overwhelm the sum with a single empty bin. This is the conventional choice.
EPSILON = 1e-6

STABLE = "stable"
MODERATE = "moderate"
SIGNIFICANT = "significant"


def classify(psi: float) -> str:
    """The industry-conventional bands. Kept as a function so the thresholds live in one place
    rather than being re-typed at each call site."""
    if psi < 0.10:
        return STABLE
    if psi < 0.25:
        return MODERATE
    return SIGNIFICANT


def quantile_edges(values: Sequence[float], bins: int = 10) -> List[float]:
    """Interior bin edges from the BASELINE's quantiles.

    Quantile bins rather than equal-width: a credit score distribution is not uniform, and
    equal-width bins put almost every observation in two or three of them, which makes PSI
    dominated by rounding in the tails.

    Duplicate edges are collapsed. A baseline where 60% of values are the same number (a default,
    a floor, a capped score) produces repeated quantiles, and a bin with zero width would take
    every one of those observations into whichever side a boundary comparison happened to fall.
    """
    ordered = sorted(float(v) for v in values if v is not None)
    if len(ordered) < 2:
        return []
    edges: List[float] = []
    for index in range(1, max(2, bins)):
        position = (len(ordered) - 1) * index / max(2, bins)
        low = ordered[int(math.floor(position))]
        high = ordered[int(math.ceil(position))]
        edge = low + (high - low) * (position - math.floor(position))
        if not edges or edge > edges[-1]:
            edges.append(edge)
    return edges


def bucket(values: Sequence[float], edges: Sequence[float]) -> List[int]:
    """Counts per bin. len(edges) + 1 bins, so the tails are open on both sides -- a value below
    the baseline's minimum or above its maximum still lands somewhere, and a shifted population
    is exactly when that happens."""
    counts = [0] * (len(edges) + 1)
    for value in values:
        if value is None:
            continue
        placed = len(edges)
        for index, edge in enumerate(edges):
            if float(value) <= edge:
                placed = index
                break
        counts[placed] += 1
    return counts


def _proportions(counts: Sequence[int]) -> List[float]:
    total = sum(counts)
    if total <= 0:
        return [0.0] * len(counts)
    return [count / total for count in counts]


def psi(baseline: Sequence[float], current: Sequence[float], bins: int = 10) -> Dict[str, Any]:
    """PSI of `current` against `baseline`, with the per-bin contributions.

    The contributions are returned, not just the total: "PSI is 0.31" tells an operator the
    population moved, and the per-bin table is what tells them WHERE -- which is the difference
    between a number in a dashboard and something someone can act on.
    """
    baseline_values = [float(v) for v in baseline if v is not None]
    current_values = [float(v) for v in current if v is not None]
    if len(baseline_values) < 2 or not current_values:
        # Not an error and not zero: zero would read as "stable", and there is no evidence for
        # that. Undecidable is its own answer.
        return {"psi": None, "classification": "insufficient_data", "bins": [],
                "baseline_count": len(baseline_values), "current_count": len(current_values)}

    edges = quantile_edges(baseline_values, bins)
    baseline_counts = bucket(baseline_values, edges)
    current_counts = bucket(current_values, edges)
    baseline_props = _proportions(baseline_counts)
    current_props = _proportions(current_counts)

    total = 0.0
    detail: List[Dict[str, Any]] = []
    # strict=True: a length mismatch means the two histograms disagree on bin count, so PSI
    # would be computed over a truncated tail -- a SMALLER, healthier-looking drift than
    # reality. Failing loudly beats under-reporting drift.
    for index, (expected, actual) in enumerate(zip(baseline_props, current_props, strict=True)):
        # Floored, never skipped: a bin that was 20% of the baseline and is now empty is the
        # strongest drift signal there is, and skipping it would report stability.
        safe_expected = max(expected, EPSILON)
        safe_actual = max(actual, EPSILON)
        contribution = (safe_actual - safe_expected) * math.log(safe_actual / safe_expected)
        total += contribution
        detail.append({
            "bin": index,
            "lower": None if index == 0 else edges[index - 1],
            "upper": None if index == len(edges) else edges[index],
            "baseline_pct": round(expected * 100, 4),
            "current_pct": round(actual * 100, 4),
            "contribution": round(contribution, 6),
        })

    return {
        "psi": round(total, 6),
        "classification": classify(total),
        "bins": detail,
        "baseline_count": len(baseline_values),
        "current_count": len(current_values),
    }
