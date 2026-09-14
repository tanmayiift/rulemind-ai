"""Nested-key validation for decision input payloads (parity P06).

WHY THIS EXISTS, and which direction the hole failed.

The decision payload is source-keyed and deeply nested — `{"bureau": {"scores": [{"scoreName":
...}]}}`. Nothing checked those nested keys. A caller who misspelled one, sent a string where the
policy expects a number, or dropped a required leaf got HTTP 200 and a decision: the key simply
resolved to `None`.

`None` is the worst possible substitute, because `logic.py` returns False for EVERY comparison
against it. So a typo does not error, it silently re-phrases the rule:

    "reject if bureau.scores[0].score >= 900"  -> False -> NOT rejected   (fails OPEN)
    "approve if bureau.scores[0].score >= 700" -> False -> not approved   (fails closed)

Which way a typo breaks is decided by how the author happened to phrase the gate — the same
failure mode Tier 1.2 fixed for a variable that FAILED to compute. This is the adjacent hole:
there the input was computed and errored, here it was never checked in the first place.

WHAT THE SCHEMA IS. Connectors already declare their nested shape, in two halves:

  * `schema_paths` — the declared path DSL: `summary.avgBalance`, `scores[].scoreName`. These are
    the fields the policy actually reads, so they are treated as REQUIRED.
  * `sample_payload` — a complete example, which supplies the TYPE of every path plus the
    known-but-optional keys that `schema_paths` does not list.

A connector that declares neither (the seeded `custom` "any JSON" source) has no schema and is
skipped rather than guessed at.

WHAT IS AND IS NOT REQUIRED, and why that follows from the /decide contract. `/decide` takes two
intentionally distinct payload shapes (see its docstring):

  * a source-keyed dict is AUTHORITATIVE for that source — fields you omit are absent, not
    defaulted. Missing a required path there is a real error.
  * a flat top-level field is a per-field OVERRIDE on top of the source's sample — nothing can be
    "missing", because everything unspecified keeps its sample value. So required-ness is only
    enforced on the authoritative form; flat fields are checked for spelling and type only.

GATING. Off by default, per tenant and per policy under `engine_config.input_validation`,
following the same pattern as `engine_config.shadow_map` / `fail_open_on_variable_error`. A hard
schema check switched on globally would reject every existing integration on its first request.

    "input_validation": {
        "mode": "warn",                     # off (default) | warn | enforce
        "unknown_keys": "warn",             # ignore | warn (default) | reject
        "policies": {"policy_x": {"mode": "enforce"}}
    }

`mode: warn` records every issue (error event + decision trace) and lets the decision proceed —
the migration step that tells an operator what enforcement would reject before it rejects it.
`mode: enforce` refuses the decision.

ON HTTPException. Core modules here are otherwise framework-free, and the precedent for a
decision-path exception that needs a specific status is `EvaluationBudgetExceeded` + an
`@app.exception_handler` in main.py. This one carries its own status instead, because the
alternative is a 500 for a caller's typo — the exact anti-pattern `model_executor.INVALID_INPUT`
exists to avoid ("a malformed request is not a server fault, and a 5xx on it pages on-call").
Moving it to a main.py handler is a mechanical change if the coupling is unwanted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import HTTPException

# Mirrors model_executor.INVALID_INPUT: errors the CALLER caused, answered 422 rather than 500.
# Duplicated rather than imported so this module stays free of the model runtime's imports.
INVALID_INPUT = "invalid_input"

ENGINE_CONFIG_KEY = "input_validation"

UNKNOWN_KEY = "unknown_key"
WRONG_TYPE = "wrong_type"
MISSING_REQUIRED = "missing_required"

# A `schema_paths` segment: an identifier, optionally an array marker. Anything else (the seeded
# custom source's literal "(user-defined)" placeholder) is not a path and is skipped, rather than
# declaring a required field with a punctuation name that no payload can ever satisfy.
_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*(\[\])?$")


@dataclass(frozen=True)
class InputIssue:
    """One problem with one path. Issues are collected, never raised one at a time —
    a caller fixing a payload one field per round trip is a bad API.

    `message` names the offending path and the expected/received TYPE, never the value. These
    issues are echoed to the caller and written to an error event, and a decision payload is
    exactly where the PII lives (see storage's pii_redact_keys); a validator that quoted the bad
    value would leak it into a log that was never encrypted for it.
    """

    kind: str
    path: str
    message: str
    source_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "message": self.message, "source": self.source_id}


class InputValidationError(HTTPException):
    """Enforced validation failed: the decision did NOT run.

    Deliberately raised rather than returned with a degraded outcome. Half-validated input is not
    a decision — the whole point is that the unchecked keys were becoming None and quietly
    flipping gates, so inventing an outcome from them is the bug, not the fallback.

    Carries EVERY issue found, not only the blocking ones, each flagged with whether it is what
    refused the request. A caller who fixes the two blockers and is then rejected for the unknown
    key nobody mentioned has been sent round the loop for nothing.
    """

    def __init__(
        self,
        issues: Iterable[InputIssue],
        policy_id: str = "",
        blocking: Optional[Iterable[InputIssue]] = None,
    ) -> None:
        self.issues: List[InputIssue] = list(issues)
        self.blocking: List[InputIssue] = list(blocking) if blocking is not None else list(self.issues)
        self.policy_id = policy_id
        blocking_set = set(self.blocking)
        paths = ", ".join(issue.path for issue in self.blocking[:5])
        if len(self.blocking) > 5:
            paths += ", ... (+{0} more)".format(len(self.blocking) - 5)
        summary = "Input payload rejected: {0} of {1} problem(s) block the decision — {2}".format(
            len(self.blocking), len(self.issues), paths)
        super().__init__(
            status_code=422,
            detail={
                "error": INVALID_INPUT,
                "message": summary,
                "policy_id": policy_id,
                "issues": [
                    dict(issue.to_dict(), blocking=issue in blocking_set) for issue in self.issues
                ],
            },
        )


# ── Schema ─────────────────────────────────────────────────────────────────


@dataclass
class SchemaNode:
    """One node of a source's declared input shape.

    `kinds` is the set of JSON types observed for this node in the sample (empty = unconstrained,
    so no type check). `required` is set only by `schema_paths` — the fields the connector says
    the policy reads.
    """

    kinds: Set[str] = field(default_factory=set)
    children: Dict[str, "SchemaNode"] = field(default_factory=dict)
    item: Optional["SchemaNode"] = None
    required: bool = False


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    # bool before number: in Python `isinstance(True, int)` is True, and a boolean silently
    # accepted where a score belongs is exactly the kind of thing this module exists to catch.
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _absorb_sample(node: SchemaNode, value: Any) -> None:
    """Learn structure and types from a connector's sample payload. Never marks anything
    required — a sample documents what the shape looks like, not what a caller must send."""
    kind = _kind(value)
    if kind == "null" or kind == "unknown":
        return
    node.kinds.add(kind)
    if kind == "object":
        for key, child_value in value.items():
            _absorb_sample(node.children.setdefault(str(key), SchemaNode()), child_value)
    elif kind == "array":
        if node.item is None:
            node.item = SchemaNode()
        # Every element merges into one item node, so an array whose objects carry different
        # optional keys (the seeded bureau `accounts`) yields their union rather than flagging
        # each variant against the first element.
        for element in value:
            _absorb_sample(node.item, element)


def _declare_path(root: SchemaNode, raw_path: str) -> None:
    """Declare one `schema_paths` entry (`scores[].scoreName`) as required."""
    segments = [segment for segment in str(raw_path).split(".") if segment != ""]
    if not segments or not all(_SEGMENT_RE.match(segment) for segment in segments):
        return
    node = root
    for index, segment in enumerate(segments):
        is_array = segment.endswith("[]")
        name = segment[:-2] if is_array else segment
        child = node.children.setdefault(name, SchemaNode())
        child.required = True
        if is_array:
            child.kinds.add("array")
            if child.item is None:
                child.item = SchemaNode()
            node = child.item
        else:
            node = child
        if index < len(segments) - 1:
            node.kinds.add("object")


def build_source_schema(connector: Dict[str, Any]) -> Optional[SchemaNode]:
    """The declared shape of one source, or None when it declares none (so validation skips it
    rather than inventing a schema the tenant never agreed to)."""
    root = SchemaNode(kinds={"object"})
    for raw_path in connector.get("schema_paths") or []:
        _declare_path(root, str(raw_path))
    sample = connector.get("sample_payload") or {}
    if isinstance(sample, dict):
        _absorb_sample(root, sample)
    return root if root.children else None


def declared_top_level_fields(connector: Dict[str, Any]) -> Set[str]:
    """Field names a FLAT top-level payload field can legitimately name.

    Matches how `PolicyExecutor._payloads_by_source` actually applies a flat override: it copies
    `flat[name]` onto a source when `name` appears verbatim in that source's `schema_paths`. Top
    level sample keys count too — they are real field names by any reading.
    """
    names = {str(path) for path in (connector.get("schema_paths") or [])}
    sample = connector.get("sample_payload") or {}
    if isinstance(sample, dict):
        names.update(str(key) for key in sample)
    return names


# ── Settings ───────────────────────────────────────────────────────────────

# Only the permissive spellings need enumerating: everything else lands on the strict value, so
# "enforce", "reject", "strict", "true" and any typo of them all mean the same thing.
_OFF_ALIASES = {"off", "none", "disabled", "false", "0", "no", ""}
_WARN_ALIASES = {"warn", "warning", "report", "audit"}
_IGNORE_ALIASES = {"ignore", "allow", "off", "none", "false", "0", "no"}


def _normalize(value: Any, off_aliases: Set[str], off_value: str, strict_value: str) -> str:
    """Coerce a configured mode to a known value.

    Anything unrecognised resolves to the STRICTEST setting, not to the permissive one. An
    operator who wrote `"enfroce"` into this field intended validation; silently reading a typo
    as "disabled" would reproduce, in the configuration, the exact fail-open this module exists
    to close.
    """
    text = str(value).strip().lower()
    if text in off_aliases:
        return off_value
    if text in _WARN_ALIASES:
        return "warn"
    return strict_value


@dataclass(frozen=True)
class ValidationSettings:
    mode: str = "off"  # off | warn | enforce
    unknown_keys: str = "warn"  # ignore | warn | reject

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def blocks(self, issue: InputIssue) -> bool:
        """Whether this issue refuses the decision.

        A wrong type or a missing required leaf always does under `enforce` — both mean a gate
        would read None. An unknown key is likelier a harmless extra field than a typo, so it is
        the one kind an operator can downgrade.
        """
        if self.mode != "enforce":
            return False
        if issue.kind == UNKNOWN_KEY:
            return self.unknown_keys == "reject"
        return True


def resolve_settings(engine_config: Optional[Dict[str, Any]], policy_id: str = "") -> ValidationSettings:
    """Per-tenant settings with a per-policy override layered on top.

    Per-policy exists so one strict integration can be enforced without switching the whole
    workspace on: the tenant block is the default, `policies[<id>]` wins for that policy.
    """
    raw: Any = (engine_config or {}).get(ENGINE_CONFIG_KEY)
    if raw is None:
        return ValidationSettings()
    if isinstance(raw, bool):
        raw = {"mode": "enforce" if raw else "off"}
    elif isinstance(raw, str):
        raw = {"mode": raw}
    elif not isinstance(raw, dict):
        return ValidationSettings()

    merged: Dict[str, Any] = {key: value for key, value in raw.items() if key != "policies"}
    override = (raw.get("policies") or {}).get(policy_id) if isinstance(raw.get("policies"), dict) else None
    if isinstance(override, (str, bool)):
        override = {"mode": override}
    if isinstance(override, dict):
        merged.update(override)

    mode = _normalize(merged.get("mode", "off"), _OFF_ALIASES, "off", "enforce")
    unknown = _normalize(merged.get("unknown_keys", "warn"), _IGNORE_ALIASES, "ignore", "reject")
    return ValidationSettings(mode=mode, unknown_keys=unknown)


# ── Validation ─────────────────────────────────────────────────────────────


def _join(path: str, key: str) -> str:
    return "{0}.{1}".format(path, key) if path else str(key)


def _validate_node(
    node: SchemaNode,
    value: Any,
    path: str,
    source_id: str,
    issues: List[InputIssue],
    authoritative: bool,
    unknown_keys: str,
) -> None:
    kind = _kind(value)
    if kind == "null":
        # An explicit null on a declared field is the dangerous case wearing a different hat: the
        # key is spelled right, so nothing else would ever notice, and the gate still reads None.
        if authoritative and node.required:
            issues.append(InputIssue(
                MISSING_REQUIRED, path,
                "Required field is present but null; a null input silently changes gate outcomes.",
                source_id))
        return
    if node.kinds and kind not in node.kinds:
        issues.append(InputIssue(
            WRONG_TYPE, path,
            "Expected {0}, got {1}.".format(" or ".join(sorted(node.kinds)), kind),
            source_id))
        # Do not descend: every child path under a wrongly-typed node would be noise about a
        # structure the caller did not send.
        return
    if kind == "object" and isinstance(value, dict):
        _validate_object(node, value, path, source_id, issues, authoritative, unknown_keys)
    elif kind == "array" and isinstance(value, list) and node.item is not None:
        for index, element in enumerate(value):
            _validate_node(
                node.item, element, "{0}[{1}]".format(path, index), source_id, issues,
                authoritative, unknown_keys)


def _validate_object(
    node: SchemaNode,
    value: Dict[str, Any],
    path: str,
    source_id: str,
    issues: List[InputIssue],
    authoritative: bool,
    unknown_keys: str,
) -> None:
    if not node.children:
        # Unconstrained object (nothing declared, empty sample). Nothing to check, and guessing
        # here would reject valid free-form payloads.
        return
    for key, child_value in value.items():
        child = node.children.get(str(key))
        if child is None:
            if unknown_keys != "ignore":
                issues.append(InputIssue(
                    UNKNOWN_KEY, _join(path, str(key)),
                    "Unknown key — no such field in this source's schema (likely a typo).",
                    source_id))
            continue
        _validate_node(child, child_value, _join(path, str(key)), source_id, issues, authoritative, unknown_keys)
    if not authoritative:
        return
    for key, child in node.children.items():
        if child.required and key not in value:
            issues.append(InputIssue(
                MISSING_REQUIRED, _join(path, key),
                "Required field is missing; it would resolve to null, and null compares False "
                "against every operator.",
                source_id))


def validate_payload(
    payload: Dict[str, Any],
    connectors: Dict[str, Dict[str, Any]],
    settings: ValidationSettings,
) -> List[InputIssue]:
    """Every problem in one pass, each with its full nested path.

    Splits the payload exactly the way `PolicyExecutor._payloads_by_source` does, because
    validating a different split from the one that will actually be evaluated is worse than not
    validating at all.
    """
    if not settings.enabled or not isinstance(payload, dict):
        return []
    schemas: Dict[str, SchemaNode] = {}
    for connector_id, connector in connectors.items():
        schema = build_source_schema(connector)
        if schema is not None:
            schemas[connector_id] = schema

    issues: List[InputIssue] = []
    flat_fields: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in connectors and isinstance(value, dict):
            schema = schemas.get(key)
            if schema is not None:
                # Authoritative: omitted fields are absent, not defaulted, so required applies.
                _validate_object(schema, value, str(key), str(key), issues, True, settings.unknown_keys)
        else:
            flat_fields[key] = value

    _validate_flat(flat_fields, connectors, schemas, issues, settings)
    # Stable order so a caller diffing two responses sees the same list, and so the first five
    # named in the error message are not arbitrary.
    issues.sort(key=lambda issue: (issue.path, issue.kind))
    return issues


def _validate_flat(
    flat_fields: Dict[str, Any],
    connectors: Dict[str, Dict[str, Any]],
    schemas: Dict[str, SchemaNode],
    issues: List[InputIssue],
    settings: ValidationSettings,
) -> None:
    """Flat top-level fields: spelling and type only.

    Never required — a flat field is an override on top of each source's sample, so nothing it
    omits is missing. A flat field no source declares reaches no source's variables at all: it is
    silently inert today, which is precisely the typo worth naming.
    """
    if not flat_fields:
        return
    declared: Dict[str, Set[str]] = {}
    for connector_id, connector in connectors.items():
        for name in declared_top_level_fields(connector):
            declared.setdefault(name, set()).add(connector_id)
    for key, value in flat_fields.items():
        owners = declared.get(str(key))
        if not owners:
            if settings.unknown_keys != "ignore":
                issues.append(InputIssue(
                    UNKNOWN_KEY, str(key),
                    "Unknown top-level field — no source declares it, so it reaches no variable.",
                    ""))
            continue
        for connector_id in sorted(owners):
            node = schemas.get(connector_id)
            child = node.children.get(str(key)) if node else None
            if child is None or not child.kinds:
                continue
            kind = _kind(value)
            if kind != "null" and kind not in child.kinds:
                issues.append(InputIssue(
                    WRONG_TYPE, str(key),
                    "Expected {0}, got {1}.".format(" or ".join(sorted(child.kinds)), kind),
                    connector_id))
            break
