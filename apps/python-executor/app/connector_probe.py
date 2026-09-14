"""Real connection probing for datasource connectors (parity P01).

WHY THIS EXISTS
---------------
``POST /api/v1/connectors/{id}/test`` has existed since the first authoring release, and until
now it opened no socket. It echoed the connector's stored ``schema_paths`` and ``sample_payload``
and returned ``passed: true`` — always, for every connector, including one whose base_url was a
typo and whose API key had been rotated away months earlier. The dashboard's "Test connection"
button was therefore worse than absent: an operator about to cut a bureau/bank/GST integration
over to prod pressed it, saw a green result, and took that as evidence the credentials worked.
It was evidence of nothing except that the row existed in the database.

WHAT A PROBE MUST NOT BECOME
----------------------------
This endpoint needs only the AUTHOR capability (``rbac._AUTHOR_PREFIXES`` lists
``/api/v1/connectors``), so a ``policy_maker`` can drive it, and it fetches the connector with
``include_secrets=True``. Making it fetch an author-controlled URL with stored credentials
attached is precisely the shape of the SSRF-plus-exfiltration hole that ``app/egress.py`` was
written to close on the action path. So:

* every probe goes through ``egress.build_pinned_request`` — scheme check, DNS resolution,
  private/loopback/link-local range check on *every* resolved address, and the connection pinned
  to the vetted IP so a DNS rebind between check and connect cannot swing the target;
* redirects are never followed (a 302 to 169.254.169.254 would bypass the pinning);
* nothing resolved from ``{{secrets.*}}`` — and no value of a secret-marked config key — may
  appear anywhere in the response, which is enforced by running the whole response body through
  ``executor._redact_resolved_secrets`` plus ``storage.mask_secret_values``;
* the downstream's response BODY is never echoed. A misconfigured partner endpoint that reflects
  the Authorization header into an error message would otherwise hand the credential straight
  back through this endpoint.

TWO DIFFERENT FACTS, TWO DIFFERENT STATUS CODES
-----------------------------------------------
"Your connector is misconfigured" and "your bureau is down" are not the same finding and an
operator needs to be able to tell them apart without reading prose:

* misconfigured → ``ConnectorMisconfigured``, which the router turns into **422**. No endpoint
  configured, a non-http(s) scheme, a URL with no host, or a target the egress policy refuses
  (link-local, loopback, off-allowlist). These are all fixed by editing the connector.
* the probe ran and the downstream misbehaved → **200** with ``passed: false`` and an outcome of
  ``auth_failed`` / ``http_error`` / ``timeout`` / ``tls_failure`` / ``dns_failure`` /
  ``unreachable``. Nothing to fix in RuleMind; the fix is at the other end (or in the credential).

DNS failure is deliberately on the second list even though ``egress`` reports it through the same
``EgressBlocked`` exception as a policy refusal — a name that does not resolve right now is a
downstream/DNS fact, not a policy violation. See ``_egress_is_resolution_failure``.
"""
from __future__ import annotations

import ssl
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

import httpx

from .auth import hmac_signature
from .egress import EgressBlocked, build_pinned_request, clamp_retries, clamp_timeout_ms
from .executor import _redact_resolved_secrets
from .logic import json_dumps, now_iso
from .storage import SECRET_FIELD_MARKERS, mask_secret_values
from .templates import resolve_template

__all__ = [
    "ConnectorMisconfigured",
    "SIMULATE_SCHEME",
    "probe_connector",
    "redact_connector_secrets",
]

# Outcomes. Closed set — the dashboard and any operator runbook key off these strings.
OUTCOME_REACHABLE = "reachable"
OUTCOME_AUTH_FAILED = "auth_failed"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_TLS_FAILURE = "tls_failure"
OUTCOME_DNS_FAILURE = "dns_failure"
OUTCOME_UNREACHABLE = "unreachable"
OUTCOME_SIMULATED = "simulated"

SIMULATE_SCHEME = "rulemind://simulate/"

# A probe is a synchronous UI request, so its budget is tighter than an action's. The action
# clamps (30s x 5 retries) still apply first — these narrow them further rather than replacing
# them, so raising RULEMIND_ACTION_MAX_TIMEOUT_MS cannot widen a probe past 2 x 10s.
PROBE_MAX_TIMEOUT_MS = 10_000
PROBE_MAX_RETRIES = 1
# str(exc) from httpx/ssl can be long and can carry the full URL. Truncated after redaction.
MAX_REASON_CHARS = 200


class ConnectorMisconfigured(Exception):
    """The connector cannot be probed as configured — a 4xx fact, not a downstream failure.

    Carries a stable ``code`` so the dashboard can branch without parsing prose.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ── secrets ────────────────────────────────────────────────────────────────
def _is_secret_key(key: Any) -> bool:
    return any(marker in str(key).lower() for marker in SECRET_FIELD_MARKERS)


def connector_secret_values(connector: Dict[str, Any]) -> Set[str]:
    """Every stored secret STRING for this connector, for value-based redaction.

    Value-based, not key-based, on purpose: ``mask_secret_values`` masks a field called
    ``api_key``, but it cannot see the same value pasted into ``base_url``'s query string or
    reflected inside an exception message. Both matter here.
    """
    config = connector.get("config")
    values: Set[str] = set()
    if not isinstance(config, dict):
        return values
    for key, value in config.items():
        if _is_secret_key(key) and isinstance(value, str) and value:
            values.add(value)
    return values


def _secret_view(connector: Dict[str, Any]) -> Dict[str, Any]:
    """``{{secrets.x}}`` resolution context, matching ``fast_decide._build_tenant_secrets``:
    a flat map of the connector's secret-marked config keys under a ``secrets`` root."""
    config = connector.get("config")
    if not isinstance(config, dict):
        return {"secrets": {}}
    return {"secrets": {key: value for key, value in config.items() if _is_secret_key(key)}}


def redact_connector_secrets(value: Any, connector: Dict[str, Any], extra: Optional[Set[str]] = None) -> Any:
    """Scrub a response body of this connector's secrets, by value and then by key name.

    Applied to the WHOLE response rather than field by field, so a secret that lands somewhere
    unanticipated (a new field, a nested exception string) is still caught.
    """
    secrets = connector_secret_values(connector) | (extra or set())
    return mask_secret_values(_redact_resolved_secrets(value, secrets))


# ── URL + auth assembly ────────────────────────────────────────────────────
def _first_str(config: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _probe_url(config: Dict[str, Any], view: Dict[str, Any], resolved: Set[str]) -> str:
    raw = _first_str(config, "base_url", "webhook_url")
    if not raw:
        raise ConnectorMisconfigured(
            "no_endpoint_configured",
            "Connector has no base_url or webhook_url, so there is no endpoint to test.",
        )
    url = resolve_template(raw, view, resolved)
    return url if isinstance(url, str) else str(url)


def _auth_headers(config: Dict[str, Any], body: bytes) -> Dict[str, str]:
    """Attach the credential the connector says it uses.

    A probe that omits the credential proves less than nothing: every authenticating endpoint
    would answer 401 and the operator would learn only that the URL resolves.
    """
    auth_type = str(config.get("auth_type") or "api_key").lower()
    headers: Dict[str, str] = {}
    if auth_type == "api_key":
        key = _first_str(config, "api_key", "apikey", "token")
        if key:
            headers[_first_str(config, "api_key_header") or "X-API-Key"] = key
    elif auth_type in ("bearer", "oauth2"):
        token = _first_str(config, "access_token", "token", "api_key", "client_secret")
        if token:
            headers["Authorization"] = "Bearer {0}".format(token)
    elif auth_type == "basic":
        import base64

        user = _first_str(config, "username", "user", "client_id")
        password = _first_str(config, "password", "api_key", "client_secret")
        if user or password:
            raw = "{0}:{1}".format(user, password).encode("utf-8")
            headers["Authorization"] = "Basic {0}".format(base64.b64encode(raw).decode("ascii"))
    elif auth_type == "signed_webhook":
        secret = _first_str(config, "signing_secret", "webhook_secret", "secret", "api_key", "token")
        if secret:
            # Same header and HMAC-SHA256 construction the inbound webhook path verifies
            # (routers/operations.py:105 -> auth.verify_hmac_signature), so a partner that
            # already speaks RuleMind's webhook signing accepts this probe.
            headers["x-webhook-signature"] = hmac_signature(secret, body)
    return headers


def _display_url(url: str) -> str:
    """The URL to echo back: userinfo stripped.

    ``https://user:pass@host/path`` is the oldest credential-in-a-URL there is, and it is not
    a secret-marked config KEY, so ``mask_secret_values`` would never touch it. The query string
    is kept — an operator needs to see what was actually hit, and it is already visible to the
    same caller through ``GET /api/v1/connectors``.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.hostname:
        return url
    netloc = parsed.hostname
    if parsed.port:
        netloc = "{0}:{1}".format(netloc, parsed.port)
    return urlunparse(parsed._replace(netloc=netloc))


# ── failure classification ─────────────────────────────────────────────────
# egress.vet_url funnels two very different facts through one exception type. These substrings
# are the DNS half; anything else is a policy refusal. The coupling to egress's wording is
# deliberate and pinned by a test (ProbeEgressClassificationTest) so it cannot rot silently.
_RESOLUTION_FAILURE_MARKERS = ("dns resolution failed", "did not resolve")


def _egress_is_resolution_failure(blocked: EgressBlocked) -> bool:
    """Did egress refuse this because DNS failed, or because policy said no?

    Reads the machine-readable `reason` code rather than substring-matching the message. The
    message form worked, but coupled this classification to egress's exact wording -- so
    rephrasing an error string there would have silently reclassified every unresolvable host as
    a 422 configuration error instead of a 200 "probe ran, host is not there". The marker list
    is kept as a fallback only for an EgressBlocked raised without a reason.
    """
    reason = getattr(blocked, "reason", None)
    if reason is not None:
        return reason == EgressBlocked.REASON_DNS
    text = str(blocked).lower()
    return any(marker in text for marker in _RESOLUTION_FAILURE_MARKERS)


def _causes(exc: BaseException) -> List[BaseException]:
    chain: List[BaseException] = []
    seen: Set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _classify_transport_error(exc: Exception) -> Tuple[str, str]:
    """Map a transport exception to (outcome, short reason).

    The three an operator actually acts on differently: a timeout means "it is up but slow or
    the port is filtered", a TLS failure means "the certificate/chain is wrong — check the
    hostname or the CA", and a refused connection means "nothing is listening there".
    """
    if isinstance(exc, httpx.TimeoutException):
        return OUTCOME_TIMEOUT, "no response before the probe timeout elapsed"
    if any(isinstance(cause, ssl.SSLError) for cause in _causes(exc)):
        return OUTCOME_TLS_FAILURE, "TLS handshake failed: {0}".format(exc)
    text = str(exc).lower()
    if "ssl" in text or "certificate" in text or "tls" in text:
        return OUTCOME_TLS_FAILURE, "TLS handshake failed: {0}".format(exc)
    return OUTCOME_UNREACHABLE, "connection failed: {0}".format(exc)


def _classify_status(status: int) -> Tuple[bool, str, str]:
    if 200 <= status < 300:
        return True, OUTCOME_REACHABLE, "HTTP {0}".format(status)
    if status in (401, 403, 407):
        return False, OUTCOME_AUTH_FAILED, "HTTP {0} — the endpoint rejected the connector's credentials".format(status)
    if 300 <= status < 400:
        # follow_redirects is off by design (chasing a 3xx re-opens the SSRF the pinning closed),
        # so a redirect is reported rather than followed. Usually a stale base_url.
        return False, OUTCOME_HTTP_ERROR, "HTTP {0} — redirect not followed; point base_url at the final URL".format(status)
    return False, OUTCOME_HTTP_ERROR, "HTTP {0}".format(status)


# ── the probe ──────────────────────────────────────────────────────────────
def probe_connector(connector: Dict[str, Any]) -> Dict[str, Any]:
    """Open a real connection to the connector's endpoint and report what happened.

    ``connector`` must be the ``include_secrets=True`` form — the probe cannot authenticate with
    ``••••••••``. Raises ``ConnectorMisconfigured`` (→ 422) for anything the operator must fix in
    the connector itself; otherwise always returns a result, ``passed`` false included.
    """
    config = connector.get("config")
    if not isinstance(config, dict):
        config = {}
    view = _secret_view(connector)
    resolved: Set[str] = set()

    url = _probe_url(config, view, resolved)
    # GET by default even for a webhook-shaped connector. A probe is pressed by an operator on a
    # whim, and POSTing to a partner's receiver would deliver them a real (empty) event every
    # time. A 405 back from a POST-only endpoint is still the answer that matters -- something is
    # listening, it speaks HTTP, and TLS/auth got that far. `probe_method` overrides for an
    # endpoint that genuinely has nothing to GET.
    method = (_first_str(config, "probe_method", "method") or "GET").upper()
    started_wall = now_iso()

    if url.startswith(SIMULATE_SCHEME):
        # The seeded travel/loan/sme connectors are simulation stubs and the executor short-
        # circuits them the same way (executor._fire_action_request). Reporting them as passing
        # is honest ONLY because probed=false says plainly that no socket was opened.
        return {
            "probed": False,
            "passed": True,
            "outcome": OUTCOME_SIMULATED,
            "reason": "rulemind://simulate endpoint — no connection was attempted",
            "status": None,
            "latency_ms": 0,
            "attempts": 0,
            "url": _display_url(url),
            "method": method,
            "auth_type": str(config.get("auth_type") or "api_key"),
            "checked_at": started_wall,
        }

    # SSRF guard. Not optional and not bypassable: this is an AUTHOR-reachable endpoint that
    # attaches stored credentials to an author-supplied URL.
    try:
        pinned = build_pinned_request(url)
    except EgressBlocked as blocked:
        if _egress_is_resolution_failure(blocked):
            return {
                "probed": True,
                "passed": False,
                "outcome": OUTCOME_DNS_FAILURE,
                "reason": str(blocked)[:MAX_REASON_CHARS],
                "status": None,
                "latency_ms": 0,
                "attempts": 1,
                "url": _display_url(url),
                "method": method,
                "auth_type": str(config.get("auth_type") or "api_key"),
                "checked_at": started_wall,
            }
        raise ConnectorMisconfigured(
            "blocked_by_egress_policy", "Blocked by egress policy: {0}".format(blocked)
        ) from blocked

    body_template = config.get("probe_body") if isinstance(config.get("probe_body"), dict) else {}
    body = resolve_template(body_template, view, resolved)
    body_bytes = b"" if method in ("GET", "HEAD") else json_dumps(body).encode("utf-8")

    headers: Dict[str, str] = {}
    configured_headers = config.get("headers")
    if isinstance(configured_headers, dict):
        headers.update({str(k): str(resolve_template(v, view, resolved)) for k, v in configured_headers.items()})
    if body_bytes:
        headers["Content-Type"] = "application/json"
    headers.update(_auth_headers(config, body_bytes))
    headers["Host"] = pinned.host_header
    headers.setdefault("User-Agent", "RuleMind-ConnectorProbe/1")

    timeout_ms = min(clamp_timeout_ms(config.get("timeout_ms", config.get("timeoutMs", 5000))), PROBE_MAX_TIMEOUT_MS)
    retries = min(clamp_retries(config.get("retries", 0)), PROBE_MAX_RETRIES)
    extensions = {"sni_hostname": pinned.sni_hostname}

    status: Optional[int] = None
    passed = False
    outcome = OUTCOME_UNREACHABLE
    reason = "connection was never attempted"
    attempts = 0
    started = time.perf_counter()

    # follow_redirects=False is load-bearing, not a default: following a 3xx would connect to a
    # host that never went through vet_url, undoing the pinning above.
    with httpx.Client(timeout=timeout_ms / 1000, follow_redirects=False) as client:
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                response = client.request(
                    method,
                    pinned.url,
                    headers=headers,
                    content=body_bytes or None,
                    extensions=extensions,
                )
                status = response.status_code
                passed, outcome, reason = _classify_status(status)
                break
            except Exception as error:  # noqa: BLE001 — every transport failure is a report, not a 500
                outcome, reason = _classify_transport_error(error)
                passed = False
                status = None
            # One retry at most, and only if the remaining budget allows it — a probe that
            # retries into a second full timeout doubles the time the operator stares at a
            # spinner for no new information.
            if attempt >= retries or (time.perf_counter() - started) * 1000 >= timeout_ms:
                break

    return {
        "probed": True,
        "passed": passed,
        "outcome": outcome,
        # Redacted here as well as at the router: an exception string can embed the full URL,
        # and the URL can carry a resolved {{secrets.*}} value in its query.
        "reason": str(_redact_resolved_secrets(reason, resolved | connector_secret_values(connector)))[:MAX_REASON_CHARS],
        "status": status,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "attempts": attempts,
        "url": _display_url(url),
        "method": method,
        "auth_type": str(config.get("auth_type") or "api_key"),
        "checked_at": started_wall,
    }
