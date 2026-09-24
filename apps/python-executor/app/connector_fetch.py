"""Decision-time source retrieval for datasource connectors (parity P01).

WHY THIS EXISTS
---------------
``PolicyExecutor._execute_connector`` was eight lines that never opened a socket. When the
caller supplied no data for a source it substituted that connector's ``sample_payload`` and
returned ``{"status": "ok"}`` — for a source it had not contacted. A ``sample_payload`` is by
construction a healthy, *approving* example (the seeded bureau sample scores 756), so a policy
whose bureau integration was dead did not fail: it decided on demo data and reported success.
That is the same fail-open shape as ``_fail_closed_on_variable_errors`` and
``_SAMPLE_SEEDING_SOURCES``, one layer further out — the input was never acquired, and nothing
downstream can tell the difference between "the bureau said 756" and "nobody asked the bureau".

So this module does two things, and the second matters more than the first:

1. it actually retrieves the source, through the same SSRF-guarded egress path the action step
   uses, with the same auth assembly and the same redaction as ``connector_probe``; and
2. when retrieval fails it returns a FAILURE, never a payload. There is no path in this file
   that falls back to ``sample_payload``. The executor turns that failure into a review.

REUSE, NOT A SECOND IMPLEMENTATION
----------------------------------
URL assembly, credential attachment, secret redaction and transport classification all come
from ``connector_probe``. A divergence between what ``POST /connectors/{id}/test`` proves and
what actually happens at decision time is the worst outcome available here: the operator's
green tick would be evidence about a code path no decision ever takes.

OPT-IN, PER CONNECTOR
---------------------
Nothing fetches unless the connector's config says ``fetch_at_decision: true``. Every seeded
connector already carries a ``base_url``, so keying off "has an endpoint" would have switched
live HTTP on for every existing tenant at once. A connector without the flag behaves exactly as
it did before this module existed.

AUTHORING SURFACES DO NOT FETCH
-------------------------------
The executor withholds fetching from the test console, loop debug and simulation/backtest (see
``PolicyExecutor._NO_FETCH_SOURCES``). A 10,000-case backtest that fires 10,000 real bureau
pulls is a bill and a rate-limit incident, not a feature, and the surface for "does my connector
work" already exists: ``POST /api/v1/connectors/{id}/test``.

THE TWO MAPPINGS
----------------
``request_mapping`` and ``response_mapping`` have been columns the dashboard writes as free text
(ConnectorsPage.tsx) and that ZERO backend lines read. Their semantics are defined here, and
deliberately defined narrowly — as the two shapes this repo already has, so an author who can
write an action step or a rule condition already knows both:

``request_mapping`` — a JSON object whose string leaves are ``templates.resolve_template``
placeholders over the decision context, exactly like an action step's url/headers/bodyTemplate::

    {"method": "POST",
     "path": "/v2/score",
     "query": {"consent": "{{payload.custom.consent_id}}"},
     "headers": {"X-Correlation-Id": "{{execution_id}}"},
     "body": {"pan": "{{payload.custom.pan}}", "amount": "{{variables.loan_amount}}"}}

Absent → ``GET base_url`` with the connector's configured auth and headers, which is what the
probe already does. Every key is optional.

``response_mapping`` — a flat JSON object of ``source field -> JSONPath into the response``,
resolved with ``jsonpath.resolve_jsonpath``, the same resolver a poll condition uses::

    {"bureau_score": "$.data.score", "enquiries": "$.data.enquiries[0].count"}

Absent → the response body verbatim, provided it is a JSON object. The field names on the left
are the connector's ``schema_paths`` — the names its variables already read — so the mapping is
the whole adapter between a partner's payload shape and the source's declared shape.

MALFORMED MAPPINGS FAIL CLOSED
------------------------------
Free text means authors will get these wrong. Every malformed shape (unparseable JSON, a
non-object, a non-string JSONPath, an unsupported method) raises ``ConnectorMisconfigured``,
which ``fetch_source`` converts into a failed fetch — not an exception into the decision path,
and not a silent "ignore the mapping and use the body", which would hand the policy a payload
in a shape its variables cannot read while reporting success.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from fastapi import HTTPException

from . import circuit_breaker, client_certs, env
from .connector_probe import (
    ConnectorMisconfigured,
    SIMULATE_SCHEME,
    _auth_headers,
    _classify_transport_error,
    _classify_status,
    _display_url,
    _egress_is_resolution_failure,
    _probe_url,
    connector_secret_values,
    redact_connector_secrets,
)
from . import oauth2_client
from .egress import (
    EgressBlocked,
    build_pinned_request,
    clamp_cache_ttl_ms,
    clamp_retries,
    clamp_timeout_ms,
)
from .jsonpath import resolve_jsonpath
from .logic import json_dumps
from .sandbox import _BoundedCache
from .templates import resolve_template

__all__ = [
    "ConnectorMisconfigured",
    "FetchResult",
    "ProviderRefused",
    "SourceFetchFailed",
    "apply_response_mapping",
    "cache_ttl_ms",
    "fetch_enabled",
    "fetch_source",
    "on_fetch_failure",
    "reset_cache",
    "trace_entry",
]

# A decision is a synchronous request with a wall-clock budget, so a source fetch is held to a
# tighter ceiling than a fire-and-forget action. The action clamps still apply first; these
# narrow them, so raising RULEMIND_ACTION_MAX_TIMEOUT_MS cannot widen a decision-time fetch.
FETCH_MAX_TIMEOUT_MS = env.env_int("RULEMIND_CONNECTOR_FETCH_MAX_TIMEOUT_MS", 8000)
FETCH_MAX_RETRIES = env.env_int("RULEMIND_CONNECTOR_FETCH_MAX_RETRIES", 2)
# Response bodies are parsed, so an unbounded one is a memory amplifier on the hot path.
MAX_BODY_BYTES = env.env_int("RULEMIND_CONNECTOR_FETCH_MAX_BODY_BYTES", 1_048_576)
MAX_REASON_CHARS = 200

# What a failed fetch does to the decision. Same three the action step offers, and the DEFAULT
# is the safe one for the same reason `_poll_action` defaults to review_gate rather than
# `continue`: an absent source payload makes every variable of that source None, and logic.py
# returns False for every comparison against None — so "reject if fraud_score >= 90" simply
# does not reject. Continuing is a fail-open dressed as resilience; it stays available, but an
# author has to write it down.
ON_FAILURE_REVIEW = "review_gate"
ON_FAILURE_ABORT = "abort"
ON_FAILURE_CONTINUE = "continue"
_ON_FAILURE_VALUES = (ON_FAILURE_REVIEW, ON_FAILURE_ABORT, ON_FAILURE_CONTINUE)

_METHODS = ("GET", "POST", "PUT", "PATCH")

_TRUTHY = ("1", "true", "yes", "on", "y")


class SourceFetchFailed(HTTPException):
    """`on_fetch_failure: "abort"` — the decision did NOT run.

    Raised rather than returned with a degraded outcome, on the same argument as
    ``input_schema.InputValidationError``: a decision made without an input the policy declared
    it needs is not a conservative decision, it is a decision about different inputs. 502
    because the failure is upstream of RuleMind and a retry may well succeed; the detail names
    the source so a caller can tell "your bureau is down" from "your request was wrong".
    """

    def __init__(self, source_id: str, reason: str) -> None:
        self.source_id = source_id
        super().__init__(
            status_code=502,
            detail={
                "error": "source_fetch_failed",
                "message": "Data source '{0}' could not be retrieved: {1}".format(source_id, reason),
                "source": source_id,
            },
        )


# ── opt-in ─────────────────────────────────────────────────────────────────
def _config(connector: Any) -> Dict[str, Any]:
    if not isinstance(connector, dict):
        return {}
    config = connector.get("config")
    return config if isinstance(config, dict) else {}


def _truthy(value: Any) -> bool:
    """The dashboard writes connector config through free-text inputs, so `"true"` is at least
    as likely to arrive as `True`. Anything unrecognised is False — an unreadable opt-in flag
    must not switch live HTTP on."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return False


def fetch_enabled(connector: Any) -> bool:
    """Has this connector opted in to being retrieved at decision time?"""
    config = _config(connector)
    for key in ("fetch_at_decision", "fetchAtDecision", "fetch_on_decide"):
        if key in config:
            return _truthy(config[key])
    return False


def on_fetch_failure(connector: Any) -> str:
    """How a failed fetch lands on the decision. Unrecognised values fall back to the SAFE
    default rather than to `continue` — a typo in this field must not silently disarm the guard
    the field exists to configure."""
    config = _config(connector)
    for key in ("on_fetch_failure", "onFetchFailure"):
        raw = config.get(key)
        if isinstance(raw, str) and raw.strip() in _ON_FAILURE_VALUES:
            return raw.strip()
    return ON_FAILURE_REVIEW


# ── the provider contract: inputs, "200 but not an answer", documented errors ─────────────
# Three optional connector-config keys, added for the named-provider templates (plan 6.1) but
# generic -- any connector may use them:
#
#   required_inputs    [{"path": "payload.custom.ssn", "pattern": "^[0-9]{9}$"}]
#                      Checked BEFORE a token is requested or a socket opened. A decision
#                      missing the applicant's SSN fails as `missing_input` naming the field --
#                      not as an opaque 400 from the bureau, and not after spending a bureau
#                      pull on it. The reason names the PATH, never the value: these are PII.
#   response_checks    [{"path": "$.status", "in": ["completed"], "code": "...",
#                        "codes": {"processing": "..."}, "each": "status", "min_items": 1}]
#                      A provider's 200 that is not an answer -- a statement still processing,
#                      a bureau no-hit, a frozen file -- fails with a named code instead of
#                      being mapped into a payload of Nones. `response_did_not_match_mapping`
#                      only catches ALL fields missing; a no-hit that still carries its hit code
#                      would pass it with every score field None, and a None fed to a gate
#                      flips it silently.
#   error_codes        {"404": "setu_pan_not_found"}  -- a DOCUMENTED status to a stable code.
#   error_detail_path  "$.error.code" or a list of paths into the error body; the provider's own
#                      error text rides along in the reason, redacted like everything else.
_CONTRACT_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_BODY_CHARS = 4096
_DETAIL_CHARS = 120


class ProviderRefused(Exception):
    """The provider answered, and the answer is not one a decision can be made on. Distinct from
    ConnectorMisconfigured (the author's mistake) and from a transport failure (the provider's
    health): the provider is healthy, so it does not count against the circuit breaker."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _contract_code(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _CONTRACT_CODE.match(value):
        raise ConnectorMisconfigured(
            "malformed_provider_contract",
            "{0} must be a lower_snake_case code, got {1!r}.".format(where, value))
    return value


def _as_list(raw: Any, field_name: str) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ConnectorMisconfigured(
                "malformed_provider_contract", "{0} is not valid JSON: {1}".format(field_name, exc)) from exc
    if not isinstance(raw, list):
        raise ConnectorMisconfigured(
            "malformed_provider_contract", "{0} must be a JSON array.".format(field_name))
    return raw


def check_required_inputs(config: Dict[str, Any], view: Dict[str, Any]) -> None:
    """Raise ProviderRefused(missing_input | invalid_input) for the first input that is absent or
    malformed. Malformed specs raise ConnectorMisconfigured."""
    for position, spec in enumerate(_as_list(config.get("required_inputs"), "required_inputs")):
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not spec["path"].strip():
            raise ConnectorMisconfigured(
                "malformed_provider_contract",
                "required_inputs[{0}] must be an object with a string path.".format(position))
        path = spec["path"].strip()
        expression = path if path.startswith("$.") else "$." + path.lstrip(".")
        value = resolve_jsonpath(expression, view)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ProviderRefused("missing_input", "Required input {0} is missing.".format(path))
        pattern = spec.get("pattern")
        if pattern is None:
            continue
        try:
            compiled = re.compile(str(pattern))
        except re.error as exc:
            raise ConnectorMisconfigured(
                "malformed_provider_contract",
                "required_inputs[{0}].pattern is not a valid regex: {1}".format(position, exc)) from exc
        if isinstance(value, (dict, list)) or not compiled.fullmatch(str(value).strip()):
            raise ProviderRefused(
                "invalid_input", "Input {0} does not match {1}.".format(path, pattern))


def _body_path(path: str) -> str:
    """The same path normalisation response_mapping uses, so both keys read paths alike."""
    expression = path.strip()
    if not expression.startswith("$."):
        expression = "$." + expression.lstrip(".")
    if not expression.startswith("$.body") and not expression.startswith("$.response"):
        expression = "$.body." + expression[2:]
    return expression


def _short(value: Any) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= 60 else text[:57] + "..."


class _Refusal:
    """Raise ProviderRefused for one check: the code mapped from the value found, else the
    check's default code; the check's own reason when it has one."""

    def __init__(self, code: str, codes: Dict[str, Any], custom: Any) -> None:
        self.code, self.codes, self.custom = code, codes, custom

    def __call__(self, found: Any, default_reason: str) -> None:
        mapped = self.codes.get(str(found), self.code) if found is not None else self.code
        raise ProviderRefused(mapped, str(self.custom) if self.custom else default_reason)


def apply_response_checks(raw_checks: Any, body: Any) -> None:
    """Raise ProviderRefused when the parsed body is not an answer, per `response_checks`."""
    view = {"body": body, "response": body}
    for position, check in enumerate(_as_list(raw_checks, "response_checks")):
        where = "response_checks[{0}]".format(position)
        if not isinstance(check, dict) or not isinstance(check.get("path"), str) or not check["path"].strip():
            raise ConnectorMisconfigured(
                "malformed_provider_contract", "{0} must be an object with a string path.".format(where))
        code = _contract_code(check.get("code"), where + ".code")
        codes = check.get("codes") or {}
        if not isinstance(codes, dict):
            raise ConnectorMisconfigured("malformed_provider_contract", where + ".codes must be an object.")
        for key, mapped in codes.items():
            _contract_code(mapped, "{0}.codes[{1!r}]".format(where, key))
        allowed = check.get("in")
        if allowed is not None and not isinstance(allowed, list):
            raise ConnectorMisconfigured("malformed_provider_contract", where + ".in must be an array.")
        allowed_text = None if allowed is None else {str(item) for item in allowed}
        path = check["path"].strip()
        value = resolve_jsonpath(_body_path(path), view)
        custom = check.get("reason")
        refuse = _Refusal(code, codes, custom)

        min_items = check.get("min_items")
        if min_items is not None:
            if not isinstance(value, list) or len(value) < int(min_items):
                refuse(None, "{0} has fewer than {1} item(s).".format(path, int(min_items)))
        each = check.get("each")
        if each is not None:
            if not isinstance(value, list) or not value:
                refuse(None, "{0} is not a non-empty array.".format(path))
            for item in value:
                found = item.get(str(each)) if isinstance(item, dict) else None
                if found is None or (allowed_text is not None and str(found) not in allowed_text):
                    refuse(found, "{0}[].{1} is {2}, expected one of {3}.".format(
                        path, each, _short(found), sorted(allowed_text or [])))
            continue
        if allowed_text is not None:
            if value is None or isinstance(value, (dict, list)) or str(value) not in allowed_text:
                refuse(value, "{0} is {1}, expected one of {2}.".format(
                    path, _short(value), sorted(allowed_text)))
        elif min_items is None and value is None:
            refuse(None, "{0} is absent.".format(path))


def _provider_error(config: Dict[str, Any], status: Optional[int], body: str) -> Tuple[Optional[str], str]:
    """(code, provider detail) for a non-2xx response, per error_codes / error_detail_path."""
    table = config.get("error_codes") or {}
    if not isinstance(table, dict):
        raise ConnectorMisconfigured("malformed_provider_contract", "error_codes must be an object.")
    code = None
    if status is not None and str(status) in table:
        code = _contract_code(table[str(status)], "error_codes[{0!r}]".format(str(status)))
    raw_paths = config.get("error_detail_path")
    paths = [raw_paths] if isinstance(raw_paths, str) else (raw_paths or [])
    detail = ""
    if paths and body:
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if parsed is not None:
            view = {"body": parsed, "response": parsed}
            found = [resolve_jsonpath(_body_path(str(p)), view) for p in paths if isinstance(p, str)]
            detail = " / ".join(str(v) for v in found if v not in (None, "", [], {}))[:_DETAIL_CHARS]
    return code, detail


# ── mapping parsing ────────────────────────────────────────────────────────
def _as_object(raw: Any, field_name: str) -> Dict[str, Any]:
    """A mapping is a JSON object. Accept it already-parsed or as a JSON string, because the
    dashboard's connector editor stores both fields as free text."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ConnectorMisconfigured(
                "malformed_{0}".format(field_name),
                "{0} is not valid JSON: {1}".format(field_name, exc),
            ) from exc
        if not isinstance(parsed, dict):
            raise ConnectorMisconfigured(
                "malformed_{0}".format(field_name),
                "{0} must be a JSON object, got {1}.".format(field_name, type(parsed).__name__),
            )
        return parsed
    raise ConnectorMisconfigured(
        "malformed_{0}".format(field_name),
        "{0} must be a JSON object or a JSON string, got {1}.".format(field_name, type(raw).__name__),
    )


def _join_path(base: str, path: str) -> str:
    """Append `path` to `base`'s path. Not urljoin: urljoin("https://h/v2/", "/score") throws
    away the "/v2" prefix, which is exactly the mistake an author makes when they write a
    leading slash — and it would silently point a bureau pull at the wrong endpoint."""
    if not path:
        return base
    parsed = urlparse(base)
    left = parsed.path.rstrip("/")
    right = path if path.startswith("/") else "/" + path
    return urlunparse(parsed._replace(path=left + right))


def _with_query(url: str, query: Dict[str, Any]) -> str:
    if not query:
        return url
    parsed = urlparse(url)
    extra = urlencode({str(key): "" if value is None else str(value) for key, value in query.items()})
    merged = "{0}&{1}".format(parsed.query, extra) if parsed.query else extra
    return urlunparse(parsed._replace(query=merged))


# ── the resolved request ───────────────────────────────────────────────────
@dataclass
class _Request:
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[Any]
    timeout_ms: int
    retries: int
    fingerprint: str
    display_url: str
    secrets: Set[str] = field(default_factory=set)
    # The client identity to present, when the partner requires mutual TLS. Built once per
    # (tenant, connector, bundle fingerprint) in client_certs; None for the ordinary case.
    ssl_context: Any = None


def _build_request(connector: Dict[str, Any], view: Dict[str, Any],
                   tenant_id: str = "", access_token: Optional[str] = None) -> _Request:
    """Turn the connector's config + request_mapping into one concrete HTTP request.

    Raises ConnectorMisconfigured for anything an author must fix; the caller turns that into a
    failed fetch. Nothing here opens a socket.
    """
    config = _config(connector)
    resolved: Set[str] = set()
    mapping = _as_object(config.get("request_mapping"), "request_mapping")

    # Reuses the probe's endpoint resolution, including its "no base_url or webhook_url"
    # ConnectorMisconfigured — the two surfaces must agree on what "configured" means.
    url = _probe_url(config, view, resolved)
    path = mapping.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ConnectorMisconfigured(
                "malformed_request_mapping", "request_mapping.path must be a string.")
        url = _join_path(url, str(resolve_template(path, view, resolved)))
    query = mapping.get("query")
    if query is not None:
        if not isinstance(query, dict):
            raise ConnectorMisconfigured(
                "malformed_request_mapping", "request_mapping.query must be a JSON object.")
        url = _with_query(url, resolve_template(query, view, resolved))

    body = mapping.get("body")
    if body is not None:
        body = resolve_template(body, view, resolved)
    raw_method = mapping.get("method") or config.get("fetch_method")
    # Default GET, or POST when the author wrote a body — an author who supplies a body and no
    # method has plainly not asked for a GET, and a GET would drop it without saying so.
    method = str(raw_method or ("POST" if body is not None else "GET")).upper()
    if method not in _METHODS:
        raise ConnectorMisconfigured(
            "malformed_request_mapping",
            "request_mapping.method must be one of {0}, got {1}.".format(", ".join(_METHODS), method))
    if method == "GET":
        body = None

    headers: Dict[str, str] = {}
    for source in (config.get("headers"), mapping.get("headers")):
        if source is None:
            continue
        if not isinstance(source, dict):
            raise ConnectorMisconfigured(
                "malformed_request_mapping", "headers must be a JSON object.")
        headers.update({str(k): str(resolve_template(v, view, resolved)) for k, v in source.items()})

    body_bytes = b"" if body is None else json_dumps(body).encode("utf-8")
    if body_bytes:
        headers.setdefault("Content-Type", "application/json")
    headers.update(_auth_headers(config, body_bytes))
    if access_token:
        # Fetched by the async caller from the connector's token endpoint (oauth2_client) --
        # never the client secret, which only ever goes to the token endpoint.
        headers["Authorization"] = "Bearer {0}".format(access_token)
    headers.setdefault("User-Agent", "RuleMind-ConnectorFetch/1")

    timeout_ms = min(
        clamp_timeout_ms(config.get("timeout_ms", config.get("timeoutMs", 3000))), FETCH_MAX_TIMEOUT_MS)
    retries = min(clamp_retries(config.get("retries", 0)), FETCH_MAX_RETRIES)

    return _Request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_ms=timeout_ms,
        retries=retries,
        fingerprint=_fingerprint(method, url, headers, body),
        display_url=_display_url(url),
        # The access token is a secret too: it is not a config KEY, so connector_secret_values
        # cannot know it, and without adding it here it would be echoed into traces and errors.
        secrets=resolved | connector_secret_values(connector) | ({access_token} if access_token else set()),
        # Built here rather than in _send so that a bad bundle surfaces as a configuration
        # failure on THIS connector -- with a code an operator can act on -- instead of as an
        # opaque TLS handshake error attributed to the partner.
        ssl_context=client_certs.context_for(connector, tenant_id),
    )


def _fingerprint(method: str, url: str, headers: Dict[str, str], body: Any) -> str:
    """Content-address the *resolved* request.

    This is what makes the cache safe to key on. A connector-scoped cache alone would serve
    applicant A's bureau report to applicant B — the URL and body carry the subject, so two
    decisions about different people produce different fingerprints and cannot collide. Headers
    are included because a rotated credential is a different request, not the same one.
    """
    material = json_dumps({
        "method": method,
        "url": url,
        "headers": {key: headers[key] for key in sorted(headers)},
        "body": body,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ── response mapping ───────────────────────────────────────────────────────
def _parse_body(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ConnectorMisconfigured(
            "unparseable_response", "Response body is not JSON: {0}".format(exc)) from exc


def apply_response_mapping(raw_mapping: Any, body_text: str, checks: Any = None) -> Dict[str, Any]:
    """Turn the downstream's response into the source payload the variables read.

    Raises ConnectorMisconfigured (→ a failed fetch → fail closed) rather than returning a
    partial payload, because a partial payload is indistinguishable downstream from a complete
    one: the missing fields are None, and a None fed to a gate flips it silently.
    """
    mapping = _as_object(raw_mapping, "response_mapping")
    body = _parse_body(body_text)
    # Before the mapping, on the raw body: a "no hit" must fail as a no-hit, not be mapped into
    # a payload whose score fields are None.
    apply_response_checks(checks, body)
    if not mapping:
        # Identity: the partner already speaks the source's shape. Anything but an object is
        # refused — a list or a scalar cannot BE a source payload, and coercing one would put
        # the variables in front of a shape they were never written against.
        if not isinstance(body, dict):
            raise ConnectorMisconfigured(
                "unmapped_response_not_an_object",
                "Response is a {0}, not a JSON object, and no response_mapping was configured "
                "to reshape it.".format(type(body).__name__))
        if not body:
            # The same rule the mapped path applies below, and it has to apply here too. `{}` is
            # a 200 that carried no data -- a partner's "no record found", a stub endpoint, an
            # emptied error envelope. Returning it makes every variable of this source None, and
            # a None fed to a gate flips it silently. That is the exact fail-open this module
            # exists to close, and it was reachable through the one path that did not check.
            raise ConnectorMisconfigured(
                "empty_response_body",
                "Response was an empty JSON object, so the source has no data. If the partner "
                "signals 'no record' this way, map it explicitly with a response_mapping.")
        return copy.deepcopy(body)

    view = {"body": body, "response": body}
    payload: Dict[str, Any] = {}
    for field_name, path in mapping.items():
        if not isinstance(path, str) or not path.strip():
            raise ConnectorMisconfigured(
                "malformed_response_mapping",
                "response_mapping['{0}'] must be a JSONPath string.".format(field_name))
        # `$.body.score` is the explicit form; a bare `body.score` and a bare `score` are both
        # accepted, because the rule-condition builder emits paths without the `$.` prefix and
        # an author should not have to know which surface they are on.
        payload[str(field_name)] = resolve_jsonpath(_body_path(path), view)

    if payload and all(value is None for value in payload.values()):
        # Every mapped field missing means the mapping does not describe this response — a
        # changed API version, a wrapped error body, the wrong endpoint. Returning it would be
        # a payload of Nones, which is the fail-open this whole module exists to close.
        raise ConnectorMisconfigured(
            "response_did_not_match_mapping",
            "None of the {0} mapped field(s) were found in the response.".format(len(payload)))
    return payload


# ── cache ──────────────────────────────────────────────────────────────────
# Tenant-scoped AND connector-scoped AND request-scoped. The tenant segment is the one that is
# a breach rather than a bug if it is missing: connector ids are seeded identically for every
# tenant ("bureau", "bank", "kyc"), so a cache keyed by connector id alone would hand tenant B
# the bureau response RuleMind pulled for tenant A.
_CACHE_CAPACITY = env.env_int("RULEMIND_CONNECTOR_FETCH_CACHE_ENTRIES", 512)
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = _BoundedCache(_CACHE_CAPACITY)
_CACHE_LOCK = threading.Lock()


def _cache_key(tenant_id: str, connector_id: str, fingerprint: str) -> str:
    # NUL-joined, then hashed: a separator that cannot occur in an id keeps ("a\x00b", "c") from
    # colliding with ("a", "b\x00c"), which with tenant ids in the first position is exactly the
    # collision that would cross a tenant boundary.
    material = "\x00".join((tenant_id, connector_id, fingerprint))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            _CACHE.pop(key, None)
            return None
        # Deep-copied out as well as in: a variable that mutates its source payload would
        # otherwise poison every later decision that hits this entry.
        return copy.deepcopy(payload)


def _cache_put(key: str, payload: Dict[str, Any], ttl_ms: int) -> None:
    if ttl_ms <= 0:
        return
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + ttl_ms / 1000.0, copy.deepcopy(payload))


def reset_cache() -> None:
    """Test hook: drop every cached response so cases cannot bleed into each other."""
    with _CACHE_LOCK:
        _CACHE.clear()


def cache_ttl_ms(connector: Any) -> int:
    """Per-connector TTL, clamped by the platform ceiling.

    Defaults to 0 — no caching — and that is the sane default, not a placeholder. A cached
    bureau or KYC response means a decision was made on data RuleMind did not pull for it, and
    whether that is acceptable is a compliance question with a different answer per source and
    per tenant. Retries and same-subject bursts are what a short TTL is for, and an author who
    wants that says so.
    """
    config = _config(connector)
    for key in ("cache_ttl_ms", "cacheTtlMs", "fetch_cache_ttl_ms"):
        if key in config:
            return clamp_cache_ttl_ms(config[key])
    return 0


# ── transport ──────────────────────────────────────────────────────────────
class _TransportFailed(Exception):
    """The request was attempted (or refused before it left) and did not produce a usable body."""

    def __init__(self, outcome: str, reason: str, status: Optional[int] = None, blocked: bool = False,
                 body: str = "") -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.status = status
        self.blocked = blocked
        # The error response body, bounded, for error_detail_path. Never surfaced whole.
        self.body = body


def _retryable_status(status: int) -> bool:
    """5xx, 408 and 429 can succeed on a second attempt; every other non-2xx cannot."""
    return status >= 500 or status in (408, 429)


async def _send(request: _Request, deadline: Optional[float]) -> Tuple[int, str]:
    """The single HTTP seam: egress guard, connect, retry. Tests stub THIS.

    Loopback is refused by `build_pinned_request` ("127.0.0.1 resolves to non-public address"),
    so a test cannot point a fetch at a local server; stubbing here is the seam, exactly as
    tests/test_action_polling.py stubs `_fire_action_request`.
    """
    # SSRF guard on a path that attaches stored credentials to an author-supplied URL, with
    # the connection pinned to the vetted IP so a DNS rebind cannot swing the target.
    #
    # UNDER THE BUDGET, AND OFF THE EVENT LOOP. This used to be a bare call, and it is the
    # expensive half of a fetch, not the cheap one: build_pinned_request -> egress.vet_url ->
    # socket.getaddrinfo is a BLOCKING resolve that takes no timeout of its own. Running it
    # before the deadline was consulted meant the clamp below bounded only the connect, so a
    # decision's wall clock tracked resolver latency 1:1 with no ceiling -- measured at 4013ms
    # against a 300ms budget -- and with the stock resolver config (timeout:5, attempts:2) a
    # single unresponsive nameserver costs 10-40s, breaching even the default 10s budget on one
    # connector. Because it is synchronous it also blocked the event loop, so it stalled every
    # other request in the worker, not just this decision.
    #
    # asyncio.wait_for cannot kill the thread -- getaddrinfo runs to completion in the executor
    # either way -- but it returns control to the loop and bounds the DECISION, which is what
    # the budget is a promise about.
    resolve_timeout = _attempt_timeout_s(request.timeout_ms, deadline)
    if resolve_timeout is None:
        raise _TransportFailed("timeout", "evaluation budget exhausted before the fetch could run")
    try:
        pinned = await asyncio.wait_for(
            asyncio.to_thread(build_pinned_request, request.url), resolve_timeout)
    except asyncio.TimeoutError as expired:
        raise _TransportFailed(
            "timeout",
            "name resolution did not finish within the remaining evaluation budget "
            "({0:.0f}ms)".format(resolve_timeout * 1000)) from expired
    except EgressBlocked as blocked:
        outcome = "dns_failure" if _egress_is_resolution_failure(blocked) else "blocked_by_egress_policy"
        raise _TransportFailed(
            outcome, str(blocked), blocked=not _egress_is_resolution_failure(blocked)) from blocked

    headers = dict(request.headers)
    headers["Host"] = pinned.host_header
    extensions = {"sni_hostname": pinned.sni_hostname}
    content = None if request.body is None else json_dumps(request.body).encode("utf-8")

    last: _TransportFailed = _TransportFailed("unreachable", "request was never attempted")
    # follow_redirects=False is load-bearing: chasing a 3xx connects to a host that never went
    # through vet_url, undoing the pinning above.
    # `verify` carries the client identity as well as the trust store: httpx takes one SSLContext
    # for both, and client_certs.context_for starts from ssl.create_default_context(), so server
    # verification is unchanged when a client certificate is present. Passing the context does
    # NOT weaken anything -- that is worth stating, because `verify=<object>` reads like an
    # override of the default verification and is not one.
    client_kwargs: Dict[str, Any] = {
        "timeout": request.timeout_ms / 1000.0, "follow_redirects": False}
    if request.ssl_context is not None:
        client_kwargs["verify"] = request.ssl_context
    async with httpx.AsyncClient(**client_kwargs) as client:
        for attempt in range(request.retries + 1):
            # Per-attempt clamp, not just a per-request one: with `retries` the step's ceiling
            # would otherwise be (retries + 1) x timeout regardless of the evaluation budget.
            timeout = _attempt_timeout_s(request.timeout_ms, deadline)
            if timeout is None:
                raise _TransportFailed("timeout", "evaluation budget exhausted before the fetch could run")
            try:
                response = await client.request(
                    request.method, pinned.url, headers=headers, content=content,
                    extensions=extensions, timeout=timeout,
                )
            except Exception as error:  # noqa: BLE001 — every transport failure is a report
                outcome, reason = _classify_transport_error(error)
                last = _TransportFailed(outcome, reason)
            else:
                ok, outcome, reason = _classify_status(response.status_code)
                if ok:
                    return response.status_code, response.text[:MAX_BODY_BYTES]
                last = _TransportFailed(outcome, reason, status=response.status_code,
                                        body=response.text[:_ERROR_BODY_CHARS])
                if not _retryable_status(response.status_code):
                    # A 4xx is the provider's answer about THIS request; sending it again gets
                    # the same answer. For a bureau it is worse than useless -- each retry of a
                    # rejected pull can be another inquiry on the consumer's file.
                    break
            if attempt >= request.retries or _attempt_timeout_s(request.timeout_ms, deadline) is None:
                break
    raise last


def _attempt_timeout_s(timeout_ms: int, deadline: Optional[float]) -> Optional[float]:
    """This attempt's timeout, clamped to what is left of the evaluation budget.

    None means there is no budget left to start another attempt — the caller must not begin
    work it has no time to finish, which is the same rule `_poll_action` follows before each
    poll and `_compute_variables` follows before each variable.
    """
    if deadline is None:
        return timeout_ms / 1000.0
    remaining_ms = int((deadline - time.perf_counter()) * 1000)
    if remaining_ms <= 0:
        return None
    return max(1, min(timeout_ms, remaining_ms)) / 1000.0


# ── the fetch ──────────────────────────────────────────────────────────────
FetchResult = Dict[str, Any]


def _failure(connector: Dict[str, Any], source_id: str, outcome: str, reason: str,
             started: float, secrets: Optional[Set[str]] = None, **extra: Any) -> FetchResult:
    """A failed fetch. Note what is NOT in here: a payload. There is no code path in this module
    that answers a failure with `sample_payload`.

    REDACTION COVERS THE WHOLE RESULT, not just `reason`, and takes the RESOLVED secret values.
    Both halves were wrong before and the comment here asserted otherwise, which is worse than
    silence -- it told the next reader the leak was already handled.

    `redact_connector_secrets(x, connector)` alone masks this connector's own secret-NAMED config
    keys. It does not mask a value that came from somewhere else, and `{{secrets.*}}` resolves
    against a namespace flattened across every connector in the tenant
    (fast_decide._build_tenant_secrets), so a bureau connector's request_mapping can legitimately
    resolve the payments connector's token -- a value this connector has never heard of. The
    `extra` parameter at connector_probe.redact_connector_secrets exists for exactly this, and
    `_Request.secrets` already holds `resolved | connector_secret_values(connector)`. It was
    computed and never read.

    And the leak was never confined to `error`. `url` rides in `**extra` carrying the query
    string, into ctx.source_fetches -> the persisted execution, into add_error_event's message
    and details, into the step trace, and into the 502 body when on_fetch_failure is "abort".
    Redacting one field left the other four open, so the redaction is applied to the assembled
    result instead of to one string.
    """
    result: FetchResult = {
        "source": source_id,
        "status": "failed",
        "outcome": outcome,
        "error": str(reason)[:MAX_REASON_CHARS],
        "payload": None,
        "cached": False,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }
    result.update(extra)
    return dict(redact_connector_secrets(result, connector, extra=secrets or None))


async def fetch_source(
    connector: Dict[str, Any],
    view: Dict[str, Any],
    tenant_id: str,
    deadline: Optional[float] = None,
) -> FetchResult:
    """Retrieve one source's payload. Never raises; a failure is a result with no payload.

    `connector` must be the include_secrets=True form — a fetch cannot authenticate with
    ``••••••••``. `view` is the executor's context view (payload / variables / secrets / …),
    which is what the request_mapping templates resolve against.
    """
    source_id = str(connector.get("id") or "")
    started = time.perf_counter()

    # OAuth2 client credentials: obtain (or reuse) an access token BEFORE building the request.
    # Here, in the async layer, via a thread -- the token request is blocking network I/O and
    # _build_request is synchronous, so doing it there would stall the event loop for every
    # decision on a cold cache. A token failure fails the fetch CLOSED with its own code, the
    # same as any other reason a connector cannot supply its data.
    config = _config(connector)

    # Inputs first: before a token is requested and before a socket opens. A decision that
    # lacks the applicant's SSN must not spend a bureau pull finding that out.
    try:
        check_required_inputs(config, view)
    except ProviderRefused as refused:
        return _failure(connector, source_id, refused.code, refused.reason, started, input_rejected=True)
    except ConnectorMisconfigured as bad:
        return _failure(connector, source_id, bad.code, bad.reason, started, misconfigured=True)

    token_key = oauth2_client.cache_key(config, tenant_id, source_id)
    access_token: Optional[str] = None
    if oauth2_client.uses_token_endpoint(config) and not str(
            config.get("base_url") or "").startswith(SIMULATE_SCHEME):
        try:
            access_token = await asyncio.to_thread(oauth2_client.access_token, config, token_key)
        except oauth2_client.OAuth2Error as failure:
            return _failure(connector, source_id, failure.code, failure.reason, started,
                            misconfigured=failure.code in ("oauth2_client_credentials_missing",
                                                           "oauth2_bad_auth_method"))

    try:
        request = _build_request(connector, view, tenant_id, access_token=access_token)
    except ConnectorMisconfigured as bad:
        # An author error, not a downstream one — but it still fails closed. A connector that
        # says "fetch me" and cannot be turned into a request has not supplied its data.
        return _failure(connector, source_id, bad.code, bad.reason, started, misconfigured=True)

    if request.url.startswith(SIMULATE_SCHEME):
        # The seeded travel/loan/sme connectors are simulation stubs; the action path
        # short-circuits them the same way. `fetched` false says plainly that no socket opened,
        # and the sample is the *declared* content of a simulate endpoint rather than a
        # substitution for a real one that failed.
        return {
            "source": source_id, "status": "ok", "outcome": "simulated", "fetched": False,
            "payload": copy.deepcopy(connector.get("sample_payload") or {}),
            "cached": False, "latency_ms": 0, "url": request.display_url, "method": request.method,
        }

    ttl_ms = cache_ttl_ms(connector)
    key = _cache_key(tenant_id, source_id, request.fingerprint)
    if ttl_ms > 0:
        hit = _cache_get(key)
        if hit is not None:
            return {
                "source": source_id, "status": "ok", "outcome": "cached", "fetched": False,
                "payload": hit, "cached": True, "latency_ms": 0,
                "url": request.display_url, "method": request.method,
            }

    # Circuit breaker, keyed by TENANT + connector. `_fire_action_request` keys its breaker by
    # bare connector id; that is safe there only by accident, and here it would be a
    # cross-tenant coupling — connector ids are seeded identically for every tenant, so tenant
    # A's dead bureau would short-circuit tenant B's healthy one.
    breaker = None
    if circuit_breaker.enabled():
        breaker = circuit_breaker.get_breaker("connector_fetch:{0}:{1}".format(tenant_id, source_id))
        try:
            breaker.allow()
        except circuit_breaker.CircuitOpenError as open_err:
            return _failure(connector, source_id, "circuit_open", str(open_err), started,
                            circuit_open=True, url=request.display_url, method=request.method)

    try:
        try:
            status, text = await _send(request, deadline)
        except _TransportFailed as rejected:
            if rejected.status != 401 or not access_token:
                raise
            # The provider rejected a token we believed was valid -- revoked or rotated before
            # its stated expiry. Drop it and try ONCE with a fresh one. Without this the
            # connector fails every decision until the cached expiry runs out; with more than
            # one retry a genuinely broken client would hammer the token endpoint. A 401 on the
            # retry falls through to the normal failure handling below.
            #
            # (In the exception path, not after the send: _send raises _TransportFailed for any
            # non-2xx, so a `status == 401` check on its return value never fires. The first
            # version of this did exactly that, and the test that serves a revoked token caught
            # it -- one request went out, not two.)
            oauth2_client.invalidate(token_key)
            try:
                access_token = await asyncio.to_thread(oauth2_client.access_token, config, token_key)
            except oauth2_client.OAuth2Error as failure:
                return _failure(connector, source_id, failure.code, failure.reason, started)
            request = _build_request(connector, view, tenant_id, access_token=access_token)
            status, text = await _send(request, deadline)
    except _TransportFailed as failed:
        # A budget that ran out is OUR clock, not the downstream's health. Counting it would let
        # a tenant with a tight evaluation_budget_ms trip the breaker on a partner that answered
        # every request correctly -- measured at state=open after five decisions against an
        # endpoint returning 200 -- and the breaker then withholds a healthy integration from
        # every OTHER decision in the workspace. `blocked` is excluded for the same reason: an
        # egress refusal never reached the partner.
        charge_to_downstream = failed.outcome != "timeout" or "budget" not in failed.reason
        # Nor a non-retryable 4xx: that is the partner answering about THIS request -- a PAN that
        # does not exist, a malformed applicant record. Counting it let five applicants with a
        # typo in their PAN open the breaker on a healthy KYC provider and send every other
        # applicant in the workspace to review for the cooldown.
        if failed.status is not None and not _retryable_status(failed.status):
            charge_to_downstream = False
        if breaker is not None and charge_to_downstream and not failed.blocked:
            breaker.record_failure()
        try:
            mapped, detail = _provider_error(config, failed.status, failed.body)
        except ConnectorMisconfigured as bad:
            return _failure(connector, source_id, bad.code, bad.reason, started, misconfigured=True,
                            secrets=request.secrets, status_code=failed.status,
                            url=request.display_url, method=request.method)
        reason = failed.reason + (" -- provider: " + detail if detail else "")
        return _failure(connector, source_id, mapped or failed.outcome, reason, started,
                        secrets=request.secrets,
                        status_code=failed.status, blocked=failed.blocked,
                        url=request.display_url, method=request.method)

    try:
        payload = apply_response_mapping(config.get("response_mapping"), text,
                                         checks=config.get("response_checks"))
    except ProviderRefused as refused:
        # The partner is healthy and said something definite -- "no hit", "still processing".
        # Not a breaker failure, not a misconfiguration, and never a payload.
        if breaker is not None:
            breaker.record_success()
        return _failure(connector, source_id, refused.code, refused.reason, started,
                        secrets=request.secrets, status_code=status,
                        url=request.display_url, method=request.method)
    except ConnectorMisconfigured as bad:
        # The transport worked, so this does NOT count against the breaker: the downstream is
        # healthy and the connector's mapping is wrong. Tripping on it would take a working
        # endpoint out of service for every tenant decision until the cooldown elapsed.
        if breaker is not None:
            breaker.record_success()
        return _failure(connector, source_id, bad.code, bad.reason, started,
                        status_code=status, url=request.display_url, method=request.method)

    if breaker is not None:
        breaker.record_success()
    _cache_put(key, payload, ttl_ms)
    return {
        "source": source_id, "status": "ok", "outcome": "fetched", "fetched": True,
        "payload": payload, "cached": False, "status_code": status,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "url": request.display_url, "method": request.method,
    }


def trace_entry(result: FetchResult, connector: Dict[str, Any],
                secrets: Optional[Set[str]] = None) -> Dict[str, Any]:
    """The redacted, trace-safe view of a fetch, for the step trace and the decision record.

    The retrieved payload itself is reduced to its key names. It is the customer's bureau/bank
    data; the decision record already carries it once, through `redact_payload` with the
    tenant's PII keys applied, and copying it into the trace as well would put an unredacted
    second copy beside it.
    """
    entry = {key: value for key, value in result.items() if key != "payload"}
    payload = result.get("payload")
    if isinstance(payload, dict):
        entry["payload_keys"] = sorted(payload.keys())
    return dict(redact_connector_secrets(entry, connector, extra=secrets or None))
