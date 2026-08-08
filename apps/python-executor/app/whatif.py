"""What-if KPI simulation — replay historical decisions through a candidate bundle and compute
arbitrary, caller-defined KPIs on both the baseline (what was actually decided) and the candidate
(what would be decided now), so a policy author can ask "if I ship this, what happens to MY metrics?"

Chunked / streamed: decisions are pulled one page at a time via ``storage.iter_policy_decisions``
(full mode) so memory stays bounded regardless of population — a 1M-decision what-if is O(page),
not O(population). Reuses the same deterministic ``core.engine.decide`` as backtest/replay.

A KPI is a small declarative spec (no code exec — safe to accept from the API):

    {"name": "approval_rate", "type": "outcome_rate", "outcome": "approve"}
    {"name": "avg_score",     "type": "avg",          "field": "score"}
    {"name": "high_value",    "type": "count_where",  "field": "amount", "op": ">=", "value": 50000}

Supported types: outcome_rate, outcome_count, count, avg, sum, min, max, count_where.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .logic import compare as _compare


class _Acc:
    """Streaming accumulator for one KPI over one side (baseline or candidate)."""

    def __init__(self, spec: Dict[str, Any]):
        self.spec = spec
        self.type = spec.get("type")
        self.n = 0            # total rows seen
        self.hits = 0         # rows matching (outcome_rate / outcome_count / count_where)
        self.total = 0.0      # running sum (avg/sum)
        self.count_num = 0    # rows with a numeric field (avg denominator)
        self.mn: Optional[float] = None
        self.mx: Optional[float] = None

    def _num(self, variables: Dict[str, Any]) -> Optional[float]:
        raw = (variables or {}).get(self.spec.get("field"))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def observe(self, outcome: str, variables: Dict[str, Any]) -> None:
        self.n += 1
        t = self.type
        if t in ("outcome_rate", "outcome_count"):
            if outcome == self.spec.get("outcome"):
                self.hits += 1
        elif t == "count":
            self.hits += 1
        elif t == "count_where":
            v = (variables or {}).get(self.spec.get("field"))
            if _compare(v, self.spec.get("op", "=="), self.spec.get("value"), self.spec.get("value2")):
                self.hits += 1
        elif t in ("avg", "sum", "min", "max"):
            v = self._num(variables)
            if v is not None:
                self.count_num += 1
                self.total += v
                self.mn = v if self.mn is None else min(self.mn, v)
                self.mx = v if self.mx is None else max(self.mx, v)

    def value(self) -> Optional[float]:
        t = self.type
        if t == "outcome_rate":
            return round(self.hits / self.n, 6) if self.n else 0.0
        if t in ("outcome_count", "count", "count_where"):
            return float(self.hits)
        if t == "sum":
            return round(self.total, 6)
        if t == "avg":
            return round(self.total / self.count_num, 6) if self.count_num else None
        if t == "min":
            return self.mn
        if t == "max":
            return self.mx
        return None


def _iter_decisions(storage: Any, policy_id: str, tenant_id: Optional[str], full: bool, sample: int, page_size: int) -> Iterable[Dict[str, Any]]:
    if full:
        return storage.iter_policy_decisions(policy_id, tenant_id=tenant_id, page_size=page_size)
    return storage.sample_policy_decisions(policy_id, tenant_id=tenant_id, limit=sample)


def simulate_kpis(
    storage: Any,
    policy_id: str,
    kpis: List[Dict[str, Any]],
    tenant_id: Optional[str] = None,
    bundle_version: Optional[int] = None,
    full: bool = False,
    sample: int = 200,
    page_size: int = 2000,
) -> Dict[str, Any]:
    """Replay decisions and compute each KPI on baseline (recorded outcome) vs candidate (replayed).

    Returns {policy_id, mode, scanned, kpis:[{name, baseline, candidate, delta}]}. Raises ValueError
    when the candidate bundle is missing."""
    from .core.engine import decide as core_decide

    if not kpis:
        raise ValueError("At least one KPI spec is required.")
    if bundle_version is not None:
        bundle = storage.get_bundle(bundle_version, tenant_id=tenant_id)
        if not bundle:
            raise ValueError("Bundle version {0} not found.".format(bundle_version))
    else:
        bundle = storage.latest_bundle(tenant_id=tenant_id)
        if not bundle:
            raise ValueError("No compiled bundle to simulate against.")
    content = bundle["content"]

    baseline = [_Acc(k) for k in kpis]
    candidate = [_Acc(k) for k in kpis]
    scanned = 0

    for decision in _iter_decisions(storage, policy_id, tenant_id, full, sample, page_size):
        scanned += 1
        variables = decision.get("computed_variables") or {}
        original = decision.get("outcome")
        try:
            replayed = core_decide(content, decision.get("payload") or {},
                                   {"policy_id": policy_id, "variables": variables, "strict_validation": False})
            replayed_outcome = replayed.get("outcome")
        except Exception:
            replayed_outcome = "error"
        for acc in baseline:
            acc.observe(original, variables)
        for acc in candidate:
            acc.observe(replayed_outcome, variables)

    results = []
    for spec, b_acc, c_acc in zip(kpis, baseline, candidate):
        b, c = b_acc.value(), c_acc.value()
        delta = round(c - b, 6) if (isinstance(b, (int, float)) and isinstance(c, (int, float))) else None
        results.append({"name": spec.get("name"), "type": spec.get("type"),
                        "baseline": b, "candidate": c, "delta": delta})

    return {
        "policy_id": policy_id,
        "bundle_version": bundle.get("version"),
        "mode": "full" if full else "sample",
        "scanned": scanned,
        "kpis": results,
    }
