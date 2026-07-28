"""RuleMind stateless decision core.

A pure, DB-free evaluation engine: `decide(bundle, payload, context)` is a
deterministic function of its inputs with no storage, network, or global state.
The same core runs on Kubernetes pods (the primary deployment), serverless, edge,
and is mirrored by the mobile SDKs. Connector fetches and outbound actions are the
host's responsibility — the core evaluates over already-resolved inputs.

This module MUST NOT import from `app.storage`, `app.db`, `app.models`, or any
other stateful/IO layer. It depends only on the pure primitives in `app.logic`
and `app.experiments`.
"""

from .engine import (
    DecisionResult,
    InputValidationError,
    decide,
    validate_input,
)

__all__ = ["DecisionResult", "InputValidationError", "decide", "validate_input"]
