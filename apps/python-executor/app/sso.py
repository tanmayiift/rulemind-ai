"""Enterprise single sign-on: OIDC and SAML 2.0.

Both protocols end the same way — a verified assertion about *who* the user is
(their email) — which we map to a workspace member (JIT-provisioning a new one when
the workspace allows it) and mint the same bearer session the password/OTP paths use.

Design notes
------------
* **OIDC** (the modern path: Okta, Entra/Azure AD, Google, Auth0, Ping…) is verified
  entirely with PyJWT + cryptography: the ID token's RS256 signature is checked
  against the provider's JWKS, plus issuer / audience / expiry / nonce.
* **SAML 2.0** (legacy but still required) verifies the IdP's XML-DSig signature over
  the assertion with `signxml` against the configured X.509 certificate.
* Login **state** is a short-lived signed JWT (tenant + nonce + provider), so there is
  no server-side state table to migrate or clean up.
* Network calls (OIDC discovery, JWKS, token exchange) go through small module-level
  helpers that tests override — the verification logic is exercised without a network.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import zlib
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx
import jwt

from .auth import utcnow

STATE_ISSUER = "rulemind-sso"


class SsoError(Exception):
    """A recoverable SSO failure (misconfig, bad assertion, disallowed domain)."""


# ── injectable network helpers (overridden in tests) ─────────────────────
def _http_get_json(url: str, timeout: float = 8.0) -> Dict[str, Any]:  # pragma: no cover - network
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def _http_post_form(url: str, data: Dict[str, str], timeout: float = 8.0) -> Dict[str, Any]:  # pragma: no cover - network
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, data=data, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


_GET_JSON: Callable[[str], Dict[str, Any]] = _http_get_json
_POST_FORM: Callable[[str, Dict[str, str]], Dict[str, Any]] = _http_post_form


# ── state token (stateless CSRF/nonce carrier) ───────────────────────────
def _state_secret() -> str:
    import os

    return os.getenv("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret")


def issue_state(tenant_id: str, provider: str, nonce: str, redirect_uri: str, ttl_minutes: int = 10) -> str:
    payload = {
        "iss": STATE_ISSUER, "t": tenant_id, "p": provider, "n": nonce, "r": redirect_uri,
        "exp": utcnow() + timedelta(minutes=ttl_minutes), "iat": utcnow(),
    }
    return jwt.encode(payload, _state_secret(), algorithm="HS256")


def verify_state(token: str) -> Dict[str, Any]:
    try:
        claims = jwt.decode(token, _state_secret(), algorithms=["HS256"], issuer=STATE_ISSUER)
    except jwt.PyJWTError as exc:
        raise SsoError("Invalid or expired login state: {0}".format(exc))
    return claims


# ── domain / provisioning policy ─────────────────────────────────────────
def email_domain_allowed(email: str, allowed_domains: Optional[List[str]]) -> bool:
    """An empty allow-list means any domain; otherwise the email's domain must match."""
    if not allowed_domains:
        return True
    domain = (email or "").split("@")[-1].lower().strip()
    return domain in {d.lower().strip().lstrip("@") for d in allowed_domains if d}


# ── OIDC ─────────────────────────────────────────────────────────────────
def discover_oidc(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return {authorization_endpoint, token_endpoint, jwks_uri, issuer}, honouring an
    explicit config first and falling back to the issuer's discovery document."""
    endpoints = {
        "authorization_endpoint": config.get("authorization_endpoint"),
        "token_endpoint": config.get("token_endpoint"),
        "jwks_uri": config.get("jwks_uri"),
        "issuer": config.get("issuer"),
    }
    if all(endpoints.values()):
        return endpoints
    issuer = (config.get("issuer") or "").rstrip("/")
    if not issuer:
        raise SsoError("OIDC issuer is required.")
    doc = _GET_JSON(issuer + "/.well-known/openid-configuration")
    return {
        "authorization_endpoint": endpoints["authorization_endpoint"] or doc["authorization_endpoint"],
        "token_endpoint": endpoints["token_endpoint"] or doc["token_endpoint"],
        "jwks_uri": endpoints["jwks_uri"] or doc["jwks_uri"],
        "issuer": doc.get("issuer", issuer),
    }


def oidc_authorize_url(config: Dict[str, Any], state: str, nonce: str) -> str:
    endpoints = discover_oidc(config)
    params = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "scope": config.get("scope", "openid email profile"),
        "state": state,
        "nonce": nonce,
    }
    return endpoints["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def verify_oidc_id_token(id_token: str, jwks: Dict[str, Any], *, issuer: str, client_id: str,
                         nonce: Optional[str] = None, leeway: int = 60) -> Dict[str, Any]:
    """Verify an ID token's RS256 signature against the JWKS plus iss/aud/exp/nonce."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SsoError("Malformed ID token: {0}".format(exc))
    keys = {k.get("kid"): k for k in (jwks.get("keys") or [])}
    jwk = keys.get(header.get("kid"))
    if jwk is None and jwks.get("keys"):
        jwk = jwks["keys"][0]  # single-key providers may omit kid
    if jwk is None:
        raise SsoError("No matching signing key in JWKS.")
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims = jwt.decode(
            id_token, public_key, algorithms=["RS256"],
            audience=client_id, issuer=issuer, leeway=leeway,
        )
    except jwt.PyJWTError as exc:
        raise SsoError("ID token verification failed: {0}".format(exc))
    if nonce is not None and claims.get("nonce") != nonce:
        raise SsoError("Nonce mismatch — possible replay.")
    if not claims.get("email"):
        raise SsoError("ID token has no email claim.")
    return claims


def complete_oidc_login(config: Dict[str, Any], code: str, nonce: str) -> Dict[str, Any]:
    """Exchange the auth code, verify the ID token, and return the identity claims."""
    endpoints = discover_oidc(config)
    token_response = _POST_FORM(endpoints["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config["redirect_uri"],
        "client_id": config["client_id"],
        "client_secret": config.get("client_secret", ""),
    })
    id_token = token_response.get("id_token")
    if not id_token:
        raise SsoError("Token endpoint returned no id_token.")
    jwks = _GET_JSON(endpoints["jwks_uri"])
    claims = verify_oidc_id_token(id_token, jwks, issuer=endpoints["issuer"],
                                  client_id=config["client_id"], nonce=nonce)
    return {"email": claims["email"], "name": claims.get("name") or claims.get("email"), "sub": claims.get("sub")}


# ── SAML 2.0 ─────────────────────────────────────────────────────────────
_SAML_NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}


def _cert_to_pem(cert: str) -> str:
    """Accept either a full PEM block or a bare base64 X.509 body (as in SAML metadata)."""
    cert = (cert or "").strip()
    if "BEGIN CERTIFICATE" in cert:
        return cert
    body = "".join(cert.split())
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return "-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n".format(lines)


def saml_authn_request_url(config: Dict[str, Any], relay_state: str) -> str:
    """Build an SP-initiated AuthnRequest and return the IdP redirect URL (HTTP-Redirect
    binding: DEFLATE + base64 + urlencode)."""
    issue_instant = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    request_id = "_" + base64.urlsafe_b64encode(utcnow().isoformat().encode()).decode().rstrip("=")
    authn = (
        '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        'ID="{id}" Version="2.0" IssueInstant="{ts}" '
        'Destination="{dest}" AssertionConsumerServiceURL="{acs}" '
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        '<saml:Issuer>{sp}</saml:Issuer>'
        '</samlp:AuthnRequest>'
    ).format(id=request_id, ts=issue_instant, dest=config["sso_url"],
             acs=config.get("acs_url", ""), sp=config["sp_entity_id"])
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflated = compressor.compress(authn.encode("utf-8")) + compressor.flush()
    encoded = base64.b64encode(deflated).decode("ascii")
    params = {"SAMLRequest": encoded, "RelayState": relay_state}
    return config["sso_url"] + ("&" if "?" in config["sso_url"] else "?") + urllib.parse.urlencode(params)


def parse_and_verify_saml_response(config: Dict[str, Any], saml_response_b64: str) -> Dict[str, Any]:
    """Verify the IdP's XML-DSig signature over a SAML Response against the configured
    certificate and return the asserted identity {email, name, attributes}."""
    from lxml import etree  # local import: SAML is optional at runtime
    from signxml import XMLVerifier

    try:
        xml_bytes = base64.b64decode(saml_response_b64)
    except Exception as exc:
        raise SsoError("SAMLResponse is not valid base64: {0}".format(exc))
    try:
        root = etree.fromstring(xml_bytes)  # noqa: S320 - verified below
    except Exception as exc:
        raise SsoError("SAMLResponse is not valid XML: {0}".format(exc))

    cert_pem = _cert_to_pem(config.get("idp_cert", ""))
    if not cert_pem or "BEGIN CERTIFICATE" not in cert_pem:
        raise SsoError("No IdP certificate configured for SAML signature verification.")
    try:
        # require_x509=False: trust the pinned cert we were configured with, not one
        # embedded in the document. This is what actually authenticates the assertion.
        verified = XMLVerifier().verify(root, x509_cert=cert_pem, require_x509=False)
        signed = verified.signed_xml
    except Exception as exc:
        raise SsoError("SAML signature verification failed: {0}".format(exc))

    # The signed element is the Assertion (or the Response wrapping it).
    assertion = signed if signed.tag.endswith("}Assertion") else signed.find(".//saml:Assertion", _SAML_NS)
    if assertion is None:
        assertion = root.find(".//saml:Assertion", _SAML_NS)
    if assertion is None:
        raise SsoError("No signed assertion found in SAMLResponse.")

    attributes: Dict[str, str] = {}
    for attr in assertion.findall(".//saml:Attribute", _SAML_NS):
        name = attr.get("Name") or attr.get("FriendlyName") or ""
        value_el = attr.find("saml:AttributeValue", _SAML_NS)
        if name and value_el is not None and value_el.text:
            attributes[name] = value_el.text.strip()

    name_id_el = assertion.find(".//saml:Subject/saml:NameID", _SAML_NS)
    name_id = name_id_el.text.strip() if name_id_el is not None and name_id_el.text else ""

    email = ""
    for key in ("email", "mail", "urn:oid:0.9.2342.19200300.100.1.3",
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"):
        if attributes.get(key):
            email = attributes[key]
            break
    if not email and "@" in name_id:
        email = name_id
    if not email:
        raise SsoError("SAML assertion has no email (NameID or email attribute).")

    display = attributes.get("name") or attributes.get(
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name") or email
    return {"email": email, "name": display, "attributes": attributes}
