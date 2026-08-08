"""Policy backtesting — preview a candidate bundle's aggregate impact on real traffic.

Completes the safe-change-management trilogy already in the product:
  * policy **diff**  — what decision *logic* changed (``app/policy_diff.py``)
  * policy **backtest** (here) — the *aggregate outcome impact* of that change over real
    historical decisions, before you promote
  * decision **replay** — drill into how one specific past decision would differ

``backtest_policy`` replays a sample of a policy's recorded decisions through a target
compiled bundle (the latest, or a chosen historical version) using each decision's stored
inputs, then reports how many outcomes would change and the full from→to transition matrix.
It reuses the same ``core.engine.decide`` + stored-``computed_variables`` path as replay, so
it isolates rule/policy-logic changes and is unaffected by payload redaction.

Use-case agnostic: outcomes are the engine's generic verdicts, nothing domain-specific.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from .storage import Storage


def backtest_policy(
    storage: Storage,
    policy_id: str,
    tenant_id: Optional[str] = None,
    bundle_version: Optional[int] = None,
    sample: int = 200,
    max_changed_examples: int = 50,
    full: bool = False,
    page_size: int = 2000,
) -> Dict[str, Any]:
    """Replay historical decisions for ``policy_id`` through the target bundle and summarise the
    outcome impact vs what was originally recorded.

    Two modes:
      * **sample** (default): the ``sample`` most-recent decisions (fast, representative; <=2000).
      * **full** (``full=True``): the ENTIRE decision population for this policy, streamed one page
        at a time via ``storage.iter_policy_decisions`` so memory stays bounded to ``page_size``
        rows regardless of how many millions of decisions exist. Slower but exhaustive.

    Both modes replay through the same deterministic ``core.engine.decide``, so results are
    reproducible to the exact outcome. Raises ValueError when the target bundle is missing."""
    from .core.engine import decide as core_decide

    if bundle_version is not None:
        bundle = storage.get_bundle(bundle_version, tenant_id=tenant_id)
        if not bundle:
            raise ValueError("Bundle version {0} not found.".format(bundle_version))
    else:
        bundle = storage.latest_bundle(tenant_id=tenant_id)
        if not bundle:
            raise ValueError("No compiled bundle to backtest against.")

    # full mode streams the whole population (memory-bounded generator); sample mode loads <=2000.
    if full:
        decisions = storage.iter_policy_decisions(policy_id, tenant_id=tenant_id, page_size=page_size)
    else:
        decisions = storage.sample_policy_decisions(policy_id, tenant_id=tenant_id, limit=sample)
    content = bundle["content"]

    total = 0
    changed = 0
    errors = 0
    transitions: Counter = Counter()  # (original, replayed) -> count
    original_counts: Counter = Counter()
    replayed_counts: Counter = Counter()
    changed_examples: List[Dict[str, Any]] = []

    for decision in decisions:
        total += 1
        original = decision.get("outcome")
        original_counts[original] += 1
        try:
            replayed = core_decide(
                content,
                decision.get("payload") or {},
                {
                    "policy_id": policy_id,
                    "variables": decision.get("computed_variables") or {},
                    "strict_validation": False,
                },
            )
            replayed_outcome = replayed.get("outcome")
        except Exception:
            # A candidate that errors on a historical input is itself a signal — count it,
            # don't let one bad row abort the backtest.
            errors += 1
            replayed_outcome = "error"
        replayed_counts[replayed_outcome] += 1
        transitions[(original, replayed_outcome)] += 1
        if original != replayed_outcome:
            changed += 1
            if len(changed_examples) < max_changed_examples:
                changed_examples.append({
                    "decision_id": decision.get("id"),
                    "from": original,
                    "to": replayed_outcome,
                    "created_at": decision.get("created_at"),
                })

    return {
        "policy_id": policy_id,
        "bundle_version": bundle.get("version"),
        "mode": "full" if full else "sample",
        "scanned": total,
        "sample": total,
        "changed": changed,
        "change_rate_pct": round((changed / total) * 100, 2) if total else 0.0,
        "errors": errors,
        "transition_matrix": [
            {"from": frm, "to": to, "count": count}
            for (frm, to), count in sorted(transitions.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ],
        "original_distribution": dict(original_counts),
        "replayed_distribution": dict(replayed_counts),
        "changed_examples": changed_examples,
    }
