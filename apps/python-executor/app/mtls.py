"""In-app mutual-TLS (client-certificate) verification.

RuleMind is typically deployed behind a TLS-terminating proxy (nginx, Envoy, an ingress
controller, or a service mesh) that performs the TLS handshake and forwards the verified
client certificate to the app in a header. This module lets the FastAPI app *enforce* mTLS
itself — independent of the API-key / session auth — so a request to a protected path is
rejected unless it carries a client certificate the operator has allow-listed.

It is fully config-gated (default: off) so it has zero effect until an operator opts in:

    MTLS_MODE                  off | optional | required   (default: off)
    MTLS_CERT_HEADER           header carrying the client cert PEM/DER (default: x-client-cert)
                               nginx: `proxy_set_header X-Client-Cert $ssl_client_escaped_cert;`
    MTLS_VERIFY_HEADER         header carrying the proxy verify result (default: x-client-verify)
                               nginx: `proxy_set_header X-Client-Verify $ssl_client_verify;`
    MTLS_REQUIRE_PROXY_VERIFY  1 => the verify header must equal SUCCESS  (default: 0)
    MTLS_ALLOWED_FINGERPRINTS  comma-separated SHA-256 fingerprints (hex, colons optional).
                               Empty => any well-formed, unexpired cert is accepted.
    MTLS_PROTECTED_PREFIXES    comma-separated path prefixes to guard (default: /sdk/v1)

Modes:
    off       — disabled; every request passes.
    optional  — a cert is verified when present, but its absence is allowed (useful while
                rolling clients over to mTLS).
    required  — a valid, allow-listed, unexpired client cert is mandatory.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes

_MODES = {"off", "optional", "required"}


def _norm_fingerprint(value: str) -> str:
    return value.strip().lower().replace(":", "").replace(" ", "")


class MtlsSettings:
    """Snapshot of the mTLS configuration, read fresh from the environment.

    Read per-request (cheap) rather than cached at import time so operators can flip the
    mode without a restart and tests can drive each mode deterministically.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        env = env if env is not None else os.environ
        mode = (env.get("MTLS_MODE", "off") or "off").strip().lower()
        self.mode = mode if mode in _MODES else "off"
        self.cert_header = (env.get("MTLS_CERT_HEADER", "x-client-cert") or "x-client-cert").strip().lower()
        self.verify_header = (env.get("MTLS_VERIFY_HEADER", "x-client-verify") or "x-client-verify").strip().lower()
        self.require_proxy_verify = (env.get("MTLS_REQUIRE_PROXY_VERIFY", "0") or "0").strip() in ("1", "true", "yes")
        self.allowed_fingerprints = {
            _norm_fingerprint(item)
            for item in (env.get("MTLS_ALLOWED_FINGERPRINTS", "") or "").split(",")
            if item.strip()
        }
        self.protected_prefixes = tuple(
            prefix.strip()
            for prefix in (env.get("MTLS_PROTECTED_PREFIXES", "/sdk/v1") or "/sdk/v1").split(",")
            if prefix.strip()
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def guards(self, path: str) -> bool:
        return self.enabled and any(path.startswith(prefix) for prefix in self.protected_prefixes)


def _normalize_pem(text: str) -> str:
    """Reconstruct a valid PEM even when a proxy collapsed newlines into spaces.

    Some proxies forward `$ssl_client_escaped_cert` (URL-encoded, newlines preserved after
    unquoting); others emit a single line with spaces between the base64 chunks. Handle both.
    """
    text = text.strip()
    if "\n" in text:
        return text
    header = "-----BEGIN CERTIFICATE-----"
    footer = "-----END CERTIFICATE-----"
    if header in text and footer in text:
        body = text.split(header, 1)[1].split(footer, 1)[0].strip()
        chunks = body.replace("\n", " ").split()
        return "{0}\n{1}\n{2}\n".format(header, "\n".join(chunks), footer)
    return text


def _parse_certificate(raw: str) -> Optional[x509.Certificate]:
    text = unquote(raw.strip())
    if not text:
        return None
    if "BEGIN CERTIFICATE" in text:
        try:
            return x509.load_pem_x509_certificate(_normalize_pem(text).encode("utf-8"))
        except Exception:
            return None
    # Fall back to base64-encoded DER (some proxies forward the raw DER, base64'd).
    import base64

    try:
        return x509.load_der_x509_certificate(base64.b64decode(text, validate=False))
    except Exception:
        return None


def fingerprint_sha256(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def _not_valid_range(cert: x509.Certificate) -> Tuple[datetime, datetime]:
    # cryptography >= 42 exposes tz-aware *_utc accessors; fall back for older builds.
    try:
        return cert.not_valid_before_utc, cert.not_valid_after_utc
    except AttributeError:  # pragma: no cover - defensive for older cryptography
        return (
            cert.not_valid_before.replace(tzinfo=timezone.utc),
            cert.not_valid_after.replace(tzinfo=timezone.utc),
        )


def evaluate_client_cert(
    headers: Mapping[str, str],
    settings: MtlsSettings,
    now: Optional[datetime] = None,
) -> Tuple[bool, str, Dict[str, str]]:
    """Return (allowed, reason, identity).

    `identity` carries the cert fingerprint + subject when a cert was verified, so callers
    can bind the request to a client identity for auditing.
    """
    if settings.mode == "off":
        return True, "disabled", {}

    now = now or datetime.now(timezone.utc)
    # Case-insensitive header lookup (Starlette headers are already lower-cased, but callers
    # may pass a plain dict).
    lower = {str(key).lower(): value for key, value in headers.items()}

    if settings.require_proxy_verify:
        verify = (lower.get(settings.verify_header, "") or "").strip().upper()
        if verify != "SUCCESS":
            if settings.mode == "required":
                return False, "proxy_verify_failed", {}
            # optional: no trusted proxy verification, treat as absent
            return True, "optional_proxy_unverified", {}

    raw = lower.get(settings.cert_header, "")
    if not raw or not raw.strip():
        if settings.mode == "required":
            return False, "client_cert_missing", {}
        return True, "optional_absent", {}

    cert = _parse_certificate(raw)
    if cert is None:
        if settings.mode == "required":
            return False, "client_cert_unparseable", {}
        return True, "optional_unparseable", {}

    not_before, not_after = _not_valid_range(cert)
    if now < not_before or now > not_after:
        return False, "client_cert_expired", {"fingerprint": fingerprint_sha256(cert)}

    fingerprint = fingerprint_sha256(cert)
    if settings.allowed_fingerprints and fingerprint not in settings.allowed_fingerprints:
        return False, "client_cert_not_allowed", {"fingerprint": fingerprint}

    try:
        subject = cert.subject.rfc4514_string()
    except Exception:
        subject = ""
    return True, "ok", {"fingerprint": fingerprint, "subject": subject}
