"""Predictors: a named, monitored number per decision (parity P20).

WHAT "PREDICTOR" MEANT BEFORE. Only a synonym. `_PREDICTOR_SYSTEM` in app/ai.py prompts the model
to turn a plain-English description into a SCORECARD -- there was no entity, no stored values and
no report, so nothing could answer "has this score's population moved since we approved the
policy?", which is the question the word exists for in a credit shop.

WHAT A PREDICTOR IS HERE. A pointer at a number a decision already produces, plus a baseline. It
deliberately does not COMPUTE anything: the scorecard, model and variable machinery already do
that, and a predictor that recomputed would be a second implementation to keep in agreement --
the divergence class this codebase has been paying down all cycle. It names an existing number so
that number can be recorded over time and compared against its own past.

    source_type "scorecard"  -> the decision's scorecard_result score
    source_type "variable"   -> one entry of computed_variables
    source_type "outcome"    -> the decision outcome, as a number, so approval RATE is monitorable
                                the same way a score is

EXTRACTION FAILS QUIETLY AND ON PURPOSE, which is the opposite of the connector-fetch rule and
for the same reason inverted: a decision NEEDS its connector data, and merely IS observed by a
predictor. A policy that stopped emitting a scorecard should show up as a gap in a monitoring
chart, never as a failed or delayed decision.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("rulemind.predictors")

SOURCE_SCORECARD = "scorecard"
SOURCE_VARIABLE = "variable"
SOURCE_OUTCOME = "outcome"
SOURCE_TYPES = (SOURCE_SCORECARD, SOURCE_VARIABLE, SOURCE_OUTCOME)

# The outcome encoded as a number, so approval rate drifts through the same PSI machinery as a
# score does. The values are ordinal on purpose -- reject < review < approve -- so a population
# sliding from approve toward reject moves the distribution in a consistent direction rather than
# scattering it.
_OUTCOME_VALUES = {"reject": 0.0, "review": 1.0, "approve": 2.0, "pending": 1.0}


class PredictorInvalid(ValueError):
    """A predictor that cannot be stored as written -- rejected at authoring time rather than
    saved as a row that silently records nothing."""


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_type = str(payload.get("source_type") or "").strip()
    if source_type not in SOURCE_TYPES:
        raise PredictorInvalid(
            "source_type must be one of: {0}".format(", ".join(SOURCE_TYPES)))
    if source_type in (SOURCE_SCORECARD, SOURCE_VARIABLE) and not str(payload.get("source_id") or "").strip():
        raise PredictorInvalid(
            "source_id is required for a {0} predictor -- without it there is nothing to "
            "read from a decision.".format(source_type))
    return payload


def _as_float(value: Any) -> Optional[float]:
    """None for anything not genuinely numeric.

    bool is excluded explicitly: `isinstance(True, int)` is True in Python, so a boolean variable
    would otherwise be recorded as 1.0 and read as a score. A flag is not a distribution, and
    silently binning one produces a chart that looks like data.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def extract(predictor: Dict[str, Any], decision: Dict[str, Any]) -> Optional[float]:
    """The predictor's value for this decision, or None if this decision does not carry one.

    None is an ordinary answer, not an error: a policy that does not run the named scorecard
    simply has no value to record, and a monitoring series with a gap is more honest than one
    padded with zeros -- a zero would drag the distribution toward a value nothing produced.
    """
    source_type = str(predictor.get("source_type") or "")
    source_id = str(predictor.get("source_id") or "")

    if source_type == SOURCE_OUTCOME:
        return _OUTCOME_VALUES.get(str(decision.get("outcome") or "").lower())

    if source_type == SOURCE_VARIABLE:
        variables = decision.get("computed_variables")
        if not isinstance(variables, dict):
            return None
        return _as_float(variables.get(source_id))

    if source_type == SOURCE_SCORECARD:
        result = decision.get("scorecard_result")
        if not isinstance(result, dict):
            return None
        # Two shapes are in the wild: a single scorecard's result, and a map keyed by scorecard
        # id when a policy ran several. Both are read rather than assuming one, because assuming
        # produces a predictor that silently records nothing on half the policies.
        if source_id and isinstance(result.get(source_id), dict):
            return _as_float(result[source_id].get("score"))
        if not source_id or result.get("scorecard_id") in (source_id, None):
            return _as_float(result.get("score"))
        return None

    return None


def on_decision(storage: Any, decision: Dict[str, Any]) -> None:
    """Registered with `storage.register_decision_listener`.

    Same hook as outbound webhooks, and for the same reason: `add_decision` is the one place a
    Decision row is written, so this cannot miss the fast path, the executor path, or any of the
    three other entry points. Recording a monitoring value at a handler instead is how a
    monitoring series ends up covering only whichever code path someone remembered.

    Everything here is swallowed. A predictor is an observer; it must never be able to fail a
    decision that has already been returned to a caller.
    """
    tenant_id = str(decision.get("tenant_id") or "")
    if not tenant_id:
        return
    try:
        active = storage.active_predictors(tenant_id)
    except Exception:  # pragma: no cover - a lookup failure must not touch the decision
        logger.warning("could not load predictors for %s", tenant_id, exc_info=True)
        return
    if not active:
        return
    rows = []
    for predictor in active:
        value = extract(predictor, decision)
        if value is None:
            continue
        rows.append({"predictor_id": predictor["id"], "value": value,
                     "decision_id": decision.get("id")})
    if not rows:
        return
    try:
        storage.record_predictor_values(tenant_id, rows)
    except Exception:  # pragma: no cover
        logger.warning("could not record predictor values for %s", tenant_id, exc_info=True)
        return
    _fan_out_to_archive(tenant_id, rows)


def _fan_out_to_archive(tenant_id: str, rows: List[Dict[str, Any]]) -> None:
    """Copy the observations to the configured cold store, if there is one (P10 + P20).

    A FAN-OUT, NOT A MOVE, and that is the difference from decision archiving. A decision is
    archived when it falls OUT of the retention window and is then purged from the hot database.
    A predictor value is copied out as it is produced and stays where it is, because the hot copy
    is what the drift report reads -- the cold copy is for the long-horizon analysis a primary
    database is the wrong shape for.

    Best effort, after the primary write and never in front of it. The sink is optional
    (DECISION_ARCHIVE_SINK), its client is a lazy optional import that is NOT in the shipped
    image, and this runs on the decision path. A cold store that is unreachable, unconfigured or
    uninstalled must cost the decision nothing.
    """
    try:
        from .archiver import archiving_enabled, get_archiver

        if not archiving_enabled():
            return
        get_archiver().write_predictor_values([
            {**row, "tenant_id": tenant_id, "created_at": _now_iso()} for row in rows])
    except Exception:  # pragma: no cover - the cold copy is never worth a decision
        logger.warning("could not archive predictor values for %s", tenant_id, exc_info=True)


def _now_iso() -> str:
    from .logic import now_iso

    return now_iso()
