"""Predictive model evaluation + label-based backtesting.

RuleMind can *host* and *serve* models and run outcome-based A/B (champion/challenger),
but until now it had no way to answer "is this model any good?" against ground-truth
labels. This module fills that gap with the credit-risk metric suite plus multi-label
(propensity) and uplift (cross-sell) evaluation, and a temporal (vintage) backtest.

Design choices:
* **Pure numpy** — no scipy/sklearn import. The metrics are simple and self-contained,
  so evaluation works even where the heavy ML wheels are partially installed, and the
  math is transparent/auditable (this doubles as a model-validation artifact).
* **Deterministic** — bootstrap CIs take a fixed seed so a re-run reproduces the number.
* **No raw-data retention** — callers persist the returned metrics + summary, never the rows.

All array inputs accept python lists or numpy arrays. Degenerate inputs (single-class,
empty) return ``None`` for the affected metric rather than raising, so one bad slice never
aborts a whole evaluation.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np

Number = float

# Lists and numpy arrays are both accepted at runtime (_as_arrays normalizes either), but
# np.ndarray does not satisfy typing.Sequence in numpy's stubs -- so declaring `Sequence`
# made every internal array-passing call an error while the code was correct.
ArrayLike = Union[Sequence[Any], np.ndarray]


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _as_arrays(y_true: ArrayLike, y_score: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(y_score, dtype=float)
    if y.shape != s.shape:
        raise ValueError("y_true and y_score must have the same length")
    mask = ~(np.isnan(y) | np.isnan(s))
    return y[mask], s[mask]


def _rankdata_avg(a: np.ndarray) -> np.ndarray:
    """Average ranks (1-based), ties averaged — matches scipy.stats.rankdata('average')."""
    order = np.argsort(a, kind="mergesort")
    sorted_a = a[order]
    ranks_sorted = np.arange(1, len(a) + 1, dtype=float)
    j = 0
    n = len(a)
    while j < n:
        k = j
        while k + 1 < n and sorted_a[k + 1] == sorted_a[j]:
            k += 1
        if k > j:
            ranks_sorted[j : k + 1] = (ranks_sorted[j] + ranks_sorted[k]) / 2.0
        j = k + 1
    out = np.empty(n, dtype=float)
    out[order] = ranks_sorted
    return out


def _both_classes(y: np.ndarray) -> bool:
    return y.size > 0 and 0 < y.sum() < y.size


# ─────────────────────────────────────────────────────────────────────────────
# discrimination
# ─────────────────────────────────────────────────────────────────────────────
def roc_auc(y_true: ArrayLike, y_score: ArrayLike) -> Optional[Number]:
    """AUC via the Mann-Whitney U statistic (tie-aware). None if only one class present."""
    y, s = _as_arrays(y_true, y_score)
    if not _both_classes(y):
        return None
    ranks = _rankdata_avg(s)
    n_pos = float(y.sum())
    n_neg = float(y.size - n_pos)
    sum_ranks_pos = float(ranks[y == 1].sum())
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def gini_from_auc(auc: Optional[Number]) -> Optional[Number]:
    return None if auc is None else float(2.0 * auc - 1.0)


def ks_statistic(y_true: ArrayLike, y_score: ArrayLike) -> Optional[Number]:
    """Kolmogorov-Smirnov: max gap between cumulative good/bad score distributions."""
    y, s = _as_arrays(y_true, y_score)
    if not _both_classes(y):
        return None
    order = np.argsort(s, kind="mergesort")
    y_sorted = y[order]
    pos_total = y_sorted.sum()
    neg_total = y_sorted.size - pos_total
    cum_pos = np.cumsum(y_sorted) / pos_total
    cum_neg = np.cumsum(1 - y_sorted) / neg_total
    return float(np.max(np.abs(cum_pos - cum_neg)))


def pr_auc(y_true: ArrayLike, y_score: ArrayLike) -> Optional[Number]:
    """Average precision (area under the precision-recall curve). None if no positives."""
    y, s = _as_arrays(y_true, y_score)
    if y.sum() == 0:
        return None
    order = np.argsort(-s, kind="mergesort")
    y_sorted = y[order]
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(1 - y_sorted)
    precision = tp / np.maximum(tp + fp, 1e-12)
    recall = tp / y_sorted.sum()
    recall_prev = np.concatenate([[0.0], recall[:-1]])
    return float(np.sum((recall - recall_prev) * precision))


# ─────────────────────────────────────────────────────────────────────────────
# calibration
# ─────────────────────────────────────────────────────────────────────────────
def brier_score(y_true: ArrayLike, y_prob: ArrayLike) -> Optional[Number]:
    y, p = _as_arrays(y_true, y_prob)
    if y.size == 0:
        return None
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(y_true: ArrayLike, y_prob: ArrayLike, n_bins: int = 10) -> Optional[Number]:
    y, p = _as_arrays(y_true, y_prob)
    if y.size == 0:
        return None
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
    ece = 0.0
    for b in range(n_bins):
        m = idx == b
        cnt = int(m.sum())
        if cnt == 0:
            continue
        ece += (cnt / y.size) * abs(y[m].mean() - p[m].mean())
    return float(ece)


def calibration_curve(y_true: ArrayLike, y_prob: ArrayLike, n_bins: int = 10) -> List[Dict[str, Number]]:
    y, p = _as_arrays(y_true, y_prob)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
    points: List[Dict[str, Number]] = []
    for b in range(n_bins):
        m = idx == b
        if not m.any():
            continue
        points.append(
            {"mean_predicted": float(p[m].mean()), "fraction_positive": float(y[m].mean()), "count": int(m.sum())}
        )
    return points


# ─────────────────────────────────────────────────────────────────────────────
# rank-ordering (decile / score-band table) + stability (PSI)
# ─────────────────────────────────────────────────────────────────────────────
def decile_table(y_true: ArrayLike, y_score: ArrayLike, n_bands: int = 10) -> Dict[str, Any]:
    """Equal-count score bands (highest score = band 1) with observed event rate + WoE."""
    y, s = _as_arrays(y_true, y_score)
    if y.size == 0:
        return {"bands": [], "monotonic": None}
    order = np.argsort(-s, kind="mergesort")
    y_o, s_o = y[order], s[order]
    total_pos = max(float(y_o.sum()), 1e-12)
    total_neg = max(float(y_o.size - y_o.sum()), 1e-12)
    bands: List[Dict[str, Any]] = []
    for i, chunk in enumerate(np.array_split(np.arange(y_o.size), min(n_bands, y_o.size)), start=1):
        yc = y_o[chunk]
        events = float(yc.sum())
        non_events = float(yc.size - events)
        event_rate = float(yc.mean())
        dist_pos = events / total_pos
        dist_neg = non_events / total_neg
        # None, not a clamped sentinel. `max(dist, 1e-12)` turns a band with no events (or no
        # non-events) into log(1e12) ~= +-27.6, and the UI printed those straight into the "WoE"
        # column -- bands reading -26.33 and 25.79 on screen. Real weight-of-evidence lives in
        # roughly [-3, 3], so an analyst cannot tell a clamped infinity from an absurd real
        # value. Undefined is the honest answer, and the renderer can show it as such.
        if dist_pos <= 0.0 or dist_neg <= 0.0:
            woe: Optional[float] = None
        else:
            woe = float(np.log(dist_neg / dist_pos))
        bands.append(
            {
                "band": i,
                "count": int(yc.size),
                "min_score": float(s_o[chunk].min()),
                "max_score": float(s_o[chunk].max()),
                "avg_score": float(s_o[chunk].mean()),
                "events": int(events),
                "event_rate": event_rate,
                "woe": woe,
            }
        )
    rates = [b["event_rate"] for b in bands]
    counts = [b["count"] for b in bands]

    # STRICT whole-sequence monotonicity is the wrong test at realistic sample sizes, and this
    # used to be the only test. Measured false-fail rate on a PERFECTLY CALIBRATED model:
    #
    #     N=200 -> 90%      N=500 -> 70%      N=1000 -> 30%      N=4000 -> 2%
    #
    # That curve is not a modelling problem, it is binomial noise in each band's event rate
    # shrinking as N grows. A gate that fails 90% of good models at N=200 does not protect
    # anyone -- it trains people to override it, and finding 6 of this sweep was a UI that
    # hardcoded force=true, which is exactly what a gate that cries wolf produces.
    #
    # So an inversion counts only when it is larger than the noise in the two bands involved:
    # 2 standard errors of the difference between the two proportions. Below that the data does
    # not distinguish the bands at all, and calling it a rank-ordering failure is a claim the
    # sample cannot support.
    def _se(rate: float, n: int) -> float:
        if n <= 0:
            return float("inf")
        # Floor the variance so a band that happens to be all-events (p=0 or 1) does not report
        # ZERO noise and turn every neighbouring wobble into a hard failure.
        return math.sqrt(max(rate * (1.0 - rate), 0.25 / n) / n)

    def _ordered(descending: bool) -> bool:
        for i in range(len(rates) - 1):
            gap = rates[i] - rates[i + 1] if descending else rates[i + 1] - rates[i]
            if gap >= 0:
                continue
            tolerance = 2.0 * math.sqrt(
                _se(rates[i], counts[i]) ** 2 + _se(rates[i + 1], counts[i + 1]) ** 2)
            if abs(gap) > tolerance:
                return False
        return True

    total = int(sum(counts))
    if total < MONOTONIC_MIN_SAMPLES:
        # None, not False. The check is not failing here -- it is INAPPLICABLE, and the gate
        # renders None as "not applicable" rather than as a refusal. Reporting a confident
        # pass/fail on 3 rows (which this did, and the dashboard ships a 2-row placeholder
        # dataset) is worse than admitting the sample cannot answer.
        monotonic: Optional[bool] = None
    else:
        monotonic = bool(_ordered(True) or _ordered(False))
    return {
        "bands": bands,
        "monotonic": monotonic,
        "monotonic_samples": total,
        "monotonic_min_samples": MONOTONIC_MIN_SAMPLES,
        # The old strict answer, kept so a caller that wants it can still see it and so the
        # change in behaviour is visible rather than silent.
        "monotonic_strict": bool(
            all(rates[i] >= rates[i + 1] for i in range(len(rates) - 1))
            or all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))),
    }


def psi(expected: ArrayLike, actual: ArrayLike, n_bins: int = 10) -> Optional[Number]:
    """Population Stability Index between two score distributions. <0.1 stable, >0.25 shift."""
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    e = e[~np.isnan(e)]
    a = a[~np.isnan(a)]
    if e.size == 0 or a.size == 0:
        return None
    quantiles = np.quantile(e, np.linspace(0.0, 1.0, n_bins + 1))
    quantiles[0], quantiles[-1] = -np.inf, np.inf
    quantiles = np.unique(quantiles)
    if quantiles.size < 3:
        return None
    e_pct = np.histogram(e, quantiles)[0] / e.size
    a_pct = np.histogram(a, quantiles)[0] / a.size
    eps = 1e-6
    e_pct = np.clip(e_pct, eps, None)
    a_pct = np.clip(a_pct, eps, None)
    return float(np.sum((a_pct - e_pct) * np.log(a_pct / e_pct)))


# ─────────────────────────────────────────────────────────────────────────────
# confusion at threshold
# ─────────────────────────────────────────────────────────────────────────────
def confusion_at(y_true: ArrayLike, y_score: ArrayLike, threshold: float = 0.5) -> Dict[str, Number]:
    y, s = _as_arrays(y_true, y_score)
    pred = (s >= threshold).astype(float)
    tp = float(np.sum((pred == 1) & (y == 1)))
    fp = float(np.sum((pred == 1) & (y == 0)))
    tn = float(np.sum((pred == 0) & (y == 0)))
    fn = float(np.sum((pred == 0) & (y == 1)))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    acc = (tp + tn) / y.size if y.size else 0.0
    return {
        "threshold": float(threshold),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": float(precision), "recall": float(recall), "f1": float(f1), "accuracy": float(acc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# bootstrap confidence intervals
# ─────────────────────────────────────────────────────────────────────────────
def bootstrap_ci(
    y_true: ArrayLike, y_score: ArrayLike, metric_fn, n_resamples: int = 500, alpha: float = 0.05, seed: int = 0
) -> Optional[Dict[str, Number]]:
    y, s = _as_arrays(y_true, y_score)
    if y.size == 0:
        return None
    rng = np.random.default_rng(seed)
    vals: List[float] = []
    for _ in range(n_resamples):
        idx = rng.integers(0, y.size, y.size)
        v = metric_fn(y[idx], s[idx])
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return {
        "lo": float(np.percentile(vals, 100 * alpha / 2)),
        "hi": float(np.percentile(vals, 100 * (1 - alpha / 2))),
        "n": len(vals),
    }


# ─────────────────────────────────────────────────────────────────────────────
# multi-label (propensity) + uplift (cross-sell)
# ─────────────────────────────────────────────────────────────────────────────
def per_label_auc(Y_true: Sequence[Sequence], Y_score: Sequence[Sequence], labels: Sequence[str]) -> Dict[str, Any]:
    Yt = np.asarray(Y_true, dtype=float)
    Ys = np.asarray(Y_score, dtype=float)
    aucs: Dict[str, Optional[Number]] = {}
    for j, name in enumerate(labels):
        aucs[name] = roc_auc(Yt[:, j], Ys[:, j])
    valid = [v for v in aucs.values() if v is not None]
    return {"per_label_auc": aucs, "macro_auc": float(np.mean(valid)) if valid else None}


def mean_average_precision(Y_true: Sequence[Sequence], Y_score: Sequence[Sequence], labels: Sequence[str]) -> Optional[Number]:
    Yt = np.asarray(Y_true, dtype=float)
    Ys = np.asarray(Y_score, dtype=float)
    aps = [pr_auc(Yt[:, j], Ys[:, j]) for j in range(len(labels))]
    aps = [a for a in aps if a is not None]
    return float(np.mean(aps)) if aps else None  # type: ignore[arg-type]


def precision_at_k(Y_true: Sequence[Sequence], Y_score: Sequence[Sequence], k: int = 1) -> Optional[Number]:
    """Row-wise precision@k: of the top-k highest-scoring labels per row, fraction truly positive."""
    Yt = np.asarray(Y_true, dtype=float)
    Ys = np.asarray(Y_score, dtype=float)
    if Yt.size == 0:
        return None
    k = min(k, Ys.shape[1])
    topk = np.argsort(-Ys, axis=1)[:, :k]
    hits = np.take_along_axis(Yt, topk, axis=1).sum(axis=1)
    return float(np.mean(hits / k))


def qini(treatment: ArrayLike, outcome: ArrayLike, uplift_score: ArrayLike) -> Dict[str, Any]:
    """Qini coefficient + curve. treatment/outcome are 0/1; higher score = more expected uplift."""
    t = np.asarray(treatment, dtype=float)
    y = np.asarray(outcome, dtype=float)
    s = np.asarray(uplift_score, dtype=float)
    order = np.argsort(-s, kind="mergesort")
    t, y = t[order], y[order]
    cum_t = np.cumsum(t)
    cum_c = np.cumsum(1 - t)
    cum_yt = np.cumsum(y * t)
    cum_yc = np.cumsum(y * (1 - t))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(cum_c > 0, cum_t / np.where(cum_c == 0, 1, cum_c), 0.0)
    curve = cum_yt - cum_yc * ratio
    curve = np.nan_to_num(curve)
    n = curve.size
    x = np.arange(1, n + 1) / n
    area_model = float(np.trapezoid(curve, x)) if n > 1 else 0.0
    area_random = float(curve[-1] / 2.0) if n else 0.0
    return {
        "qini_coefficient": float(area_model - area_random),
        "n_treatment": int(t.sum()),
        "n_control": int((1 - t).sum()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# assemblers
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_binary(
    y_true: ArrayLike,
    y_score: ArrayLike,
    *,
    threshold: float = 0.5,
    bootstrap: bool = True,
    seed: int = 0,
) -> Dict[str, Any]:
    y, s = _as_arrays(y_true, y_score)
    auc = roc_auc(y, s)
    metrics: Dict[str, Any] = {
        "n": int(y.size),
        "base_rate": float(y.mean()) if y.size else None,
        "auc": auc,
        "gini": gini_from_auc(auc),
        "ks": ks_statistic(y, s),
        "pr_auc": pr_auc(y, s),
        "brier": brier_score(y, s),
        "ece": expected_calibration_error(y, s),
        "calibration_curve": calibration_curve(y, s),
        "decile_table": decile_table(y, s),
        "confusion": confusion_at(y, s, threshold),
    }
    if bootstrap and _both_classes(y):
        metrics["gini_ci"] = bootstrap_ci(y, s, lambda a, b: gini_from_auc(roc_auc(a, b)), seed=seed)
        metrics["auc_ci"] = bootstrap_ci(y, s, roc_auc, seed=seed)
    return metrics


def temporal_backtest(
    y_true: ArrayLike, y_score: ArrayLike, buckets: Sequence[str], *, threshold: float = 0.5
) -> List[Dict[str, Any]]:
    """Per-bucket (vintage) metric summary — the label-based backtest over cohorts."""
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(y_score, dtype=float)
    b = np.asarray([str(x) for x in buckets], dtype=object)
    out: List[Dict[str, Any]] = []
    # Materialise as a typed list first. np.ndarray.tolist() is typed as returning Any, so
    # sorted(set(...)) cannot resolve its comparison type-var and mypy rejects it -- the array
    # is dtype=object holding str, which the annotation states explicitly.
    bucket_keys: List[str] = [str(x) for x in b.tolist()]
    for key in sorted(set(bucket_keys)):
        m = b == key
        auc = roc_auc(y[m], s[m])
        out.append(
            {
                "bucket": key,
                "n": int(m.sum()),
                "base_rate": float(y[m].mean()) if m.any() else None,
                "auc": auc,
                "gini": gini_from_auc(auc),
                "ks": ks_statistic(y[m], s[m]),
            }
        )
    return out


def slice_metrics(
    y_true: ArrayLike, y_score: ArrayLike, segments: Sequence[str]
) -> List[Dict[str, Any]]:
    """Per-segment discrimination (fairness/robustness slice view)."""
    return temporal_backtest(y_true, y_score, segments)  # same shape, keyed by segment value


# Below this many scored rows the decile rank-ordering check reports None (not applicable)
# rather than a verdict. See decile_table for the measured false-fail curve that motivates it.
MONOTONIC_MIN_SAMPLES = 1000


DEFAULT_GATE = {
    "gini_min": 0.5,        # applied to the CI lower bound when available
    "ece_max": 0.05,
    "require_monotonic": True,
    "leakage_gini_max": 0.90,  # too-good-to-be-true guard
}


def gate(metrics: Dict[str, Any], thresholds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Turn a metrics dict into a pass/fail promotion verdict with human-readable reasons."""
    t = {**DEFAULT_GATE, **(thresholds or {})}
    checks: List[Dict[str, Any]] = []

    gini = metrics.get("gini")
    gini_lo = (metrics.get("gini_ci") or {}).get("lo", gini)
    if gini_lo is not None:
        checks.append(
            {"check": "gini_ci_lower>=min", "value": round(gini_lo, 4), "threshold": t["gini_min"],
             "passed": gini_lo >= t["gini_min"]}
        )
    if gini is not None:
        checks.append(
            {"check": "gini<=leakage_guard", "value": round(gini, 4), "threshold": t["leakage_gini_max"],
             "passed": gini <= t["leakage_gini_max"]}
        )
    ece = metrics.get("ece")
    if ece is not None:
        checks.append({"check": "ece<=max", "value": round(ece, 4), "threshold": t["ece_max"], "passed": ece <= t["ece_max"]})
    if t.get("require_monotonic"):
        decile = metrics.get("decile_table") or {}
        mono = decile.get("monotonic")
        if mono is None:
            # NOT APPLICABLE, so it must not sink the gate. A None here means either no bands at
            # all or a sample too small for the check to mean anything; in both cases the honest
            # verdict is "this check did not run", and `passed: True` with a stated reason keeps
            # it from silently counting as a refusal.
            checks.append({
                "check": "decile_monotonic",
                "value": None,
                "threshold": True,
                "passed": True,
                "not_applicable": True,
                "reason": "only {0} scored rows; the rank-ordering check needs {1}".format(
                    decile.get("monotonic_samples", 0),
                    decile.get("monotonic_min_samples", MONOTONIC_MIN_SAMPLES)),
            })
        else:
            checks.append({"check": "decile_monotonic", "value": mono,
                           "threshold": True, "passed": bool(mono)})

    passed = all(c["passed"] for c in checks) if checks else False
    return {"passed": passed, "status": "pass" if passed else "fail", "checks": checks}


# ─────────────────────────────────────────────────────────────────────────────
# top-level dispatcher (parses rows + config → full evaluation result)
# ─────────────────────────────────────────────────────────────────────────────
def _bucket(value: Any, freq: str) -> str:
    s = str(value)
    if freq == "month" and len(s) >= 7:
        return s[:7]      # YYYY-MM
    if freq == "year" and len(s) >= 4:
        return s[:4]
    if freq == "day" and len(s) >= 10:
        return s[:10]
    return s


MAX_ROWS = 200_000


def run_evaluation(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """Parse ``rows`` per ``config`` and return {task, dataset_summary, metrics, segments,
    temporal, gate_status, gate_result}. Pure — no persistence, no raw-row retention."""
    if not rows:
        raise ValueError("no rows to evaluate")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"dataset exceeds row cap ({len(rows)} > {MAX_ROWS})")

    task = config.get("task", "binary")
    thresholds = config.get("thresholds")
    threshold = float(config.get("threshold", 0.5))
    summary: Dict[str, Any] = {"rows": len(rows), "task": task}

    if task == "binary":
        score_col = config.get("score_col", "score")
        label_col = config.get("label_col", "label")
        y = [float(r[label_col]) for r in rows]
        s = [float(r[score_col]) for r in rows]
        metrics = evaluate_binary(y, s, threshold=threshold)
        summary["base_rate"] = metrics.get("base_rate")

        segments: Dict[str, Any] = {}
        seg_col = config.get("segment_col")
        if seg_col:
            segments = {"by": seg_col, "slices": slice_metrics(y, s, [str(r.get(seg_col) or "") for r in rows])}

        temporal: Dict[str, Any] = {}
        date_col = config.get("date_col")
        if date_col:
            freq = config.get("date_freq", "month")
            buckets = [_bucket(r.get(date_col), freq) for r in rows]
            temporal = {"by": date_col, "freq": freq, "buckets": temporal_backtest(y, s, buckets, threshold=threshold)}

        gate_result = gate(metrics, thresholds)
        return {
            "task": task, "dataset_summary": summary, "metrics": metrics,
            "segments": segments, "temporal": temporal,
            "gate_status": gate_result["status"], "gate_result": gate_result,
        }

    if task == "multilabel":
        label_cols = config["label_cols"]
        score_cols = config["score_cols"]
        Yt = [[float(r[c]) for c in label_cols] for r in rows]
        Ys = [[float(r[c]) for c in score_cols] for r in rows]
        auc = per_label_auc(Yt, Ys, label_cols)
        metrics = {
            "n": len(rows),
            **auc,
            "mean_average_precision": mean_average_precision(Yt, Ys, label_cols),
            "precision_at_1": precision_at_k(Yt, Ys, 1),
            "precision_at_3": precision_at_k(Yt, Ys, min(3, len(label_cols))),
        }
        return {
            "task": task, "dataset_summary": summary, "metrics": metrics,
            "segments": {}, "temporal": {}, "gate_status": "unknown", "gate_result": {},
        }

    if task == "uplift":
        metrics = qini(
            [float(r[config.get("treatment_col", "treatment")]) for r in rows],
            [float(r[config.get("label_col", "label")]) for r in rows],
            [float(r[config.get("score_col", "score")]) for r in rows],
        )
        return {
            "task": task, "dataset_summary": summary, "metrics": metrics,
            "segments": {}, "temporal": {}, "gate_status": "unknown", "gate_result": {},
        }

    raise ValueError(f"unknown task: {task}")


def score_rows_with_model(model_blob: bytes, rows: List[Dict[str, Any]], features: List[str], label_col: str) -> List[Dict[str, Any]]:
    """Run a hosted model over feature rows → [{score, label}] for binary evaluation.

    One batch through the isolated worker (model_executor.execute_model_batch), chunked there.
    This used to call execute_model once PER ROW -- a process round trip and a full model
    deserialize for every account -- so an evaluation over a real portfolio could not finish."""
    from .model_executor import execute_model_batch  # lazy: keeps app boot free of the ML stack

    batch = execute_model_batch(model_blob, [{f: r.get(f) for f in features} for r in rows], features=features)
    if batch.get("error"):
        raise ValueError(f"model scoring failed: {batch['error']}")
    scored: List[Dict[str, Any]] = []
    for r, prediction, prob in zip(rows, batch["predictions"], batch["probabilities"], strict=True):
        if isinstance(prob, (list, tuple)) and prob:
            score = float(prob[-1])          # positive-class probability (last column)
        elif prob is not None and not isinstance(prob, (list, tuple)):
            score = float(prob)
        else:
            score = float(prediction)        # raw-prediction fallback
        scored.append({"score": score, "label": float(r[label_col])})
    return scored
