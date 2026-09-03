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

from . import env
from .auth import utcnow

STATE_ISSUER = "rulemind-sso"


class SsoError(Exception):
    """A recoverable SSO failure (misconfig, bad assertion, disallowed domain)."""


# ── injectable network helpers (overridden in tests) ─────────────────────
def _http_get_json(url: str, timeout: float = 8.0) -> Dict[str, Any]:
    # Covered by tests/test_sso_live_oidc.py against a real in-process OIDC provider.
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def _http_post_form(url: str, data: Dict[str, str], timeout: float = 8.0) -> Dict[str, Any]:
    # Covered by tests/test_sso_live_oidc.py against a real in-process OIDC provider.
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, data=data, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


_GET_JSON: Callable[[str], Dict[str, Any]] = _http_get_json
_POST_FORM: Callable[[str, Dict[str, str]], Dict[str, Any]] = _http_post_form


# ── state token (stateless CSRF/nonce carrier) ───────────────────────────
def _state_secret() -> str:
    # env.env_str, not os.getenv: auth.create_admin_jwt/decode_admin_jwt read the SAME variable
    # through env.env_str, so a set-but-EMPTY RULEMIND_ADMIN_JWT_SECRET made the two disagree —
    # auth signed with the dev fallback while this signed the SSO state token with "", a key
    # anyone can guess. A forgeable state token is a forgeable OIDC/SAML login nonce.
    return env.env_str("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret")


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
        raise SsoError("Invalid or expired login state: {0}".format(exc)) from exc
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
        raise SsoError("Malformed ID token: {0}".format(exc)) from exc
    keys = {k.get("kid"): k for k in (jwks.get("keys") or [])}
    jwk = keys.get(header.get("kid"))
    if jwk is None and jwks.get("keys"):
        jwk = jwks["keys"][0]  # single-key providers may omit kid
    if jwk is None:
        raise SsoError("No matching signing key in JWKS.")
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims = jwt.decode(
            # jwt.decode wants the PUBLIC key; jwk_to_public_key returns the union type.
            id_token, public_key, algorithms=["RS256"],  # type: ignore[arg-type]
            audience=client_id, issuer=issuer, leeway=leeway,
        )
    except jwt.PyJWTError as exc:
        raise SsoError("ID token verification failed: {0}".format(exc)) from exc
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


def _saml_parser():
    """An XML parser that will not process external entities or DTDs.

    ``etree.fromstring`` with the default parser resolves entities, and this runs PRE-AUTH on an
    attacker-supplied document: a billion-laughs entity is a denial of service, and an external
    entity is file disclosure from the API process. ``resolve_entities=False`` plus ``no_network``
    and ``load_dtd=False`` closes both.
    """
    from lxml import etree

    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                           dtd_validation=False, huge_tree=False)


def _parse_saml_datetime(value: Optional[str]):
    """Parse a SAML xs:dateTime into a naive UTC datetime, or None if absent/unparseable.

    Normalised to naive-UTC because everything it is compared against (utcnow, the stored
    not_on_or_after) is naive UTC; mixing aware and naive datetimes raises at comparison time,
    which on this path would turn a malformed timestamp into a 500 instead of a rejected login.
    """
    from datetime import datetime, timezone

    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _validate_saml_conditions(assertion, config: Dict[str, Any], skew_seconds: int = 120) -> None:
    """Enforce the assertion's own validity constraints.

    NONE of this was checked. A verified signature only proves the IdP said it -- not that it
    said it recently, to US, for THIS service. Concretely, without these checks:

      * no NotBefore/NotOnOrAfter: an assertion is valid for as long as the IdP's certificate is,
        so one captured a year ago still logs you in;
      * no AudienceRestriction: an assertion the IdP minted for a DIFFERENT service provider is
        accepted here -- so any other SP sharing the customer's IdP can impersonate its users;
      * no SubjectConfirmationData Recipient: same problem at the endpoint level.

    A 2-minute skew allowance is standard: IdP and SP clocks are not synchronised, and a stricter
    window rejects legitimate logins.
    """
    from datetime import timedelta

    now = utcnow().replace(tzinfo=None)
    conditions = assertion.find("saml:Conditions", _SAML_NS)
    if conditions is None:
        raise SsoError("SAML assertion has no Conditions element (cannot establish validity).")

    not_before = _parse_saml_datetime(conditions.get("NotBefore"))
    not_on_or_after = _parse_saml_datetime(conditions.get("NotOnOrAfter"))
    if not_before is None or not_on_or_after is None:
        raise SsoError("SAML assertion Conditions must carry NotBefore and NotOnOrAfter.")
    skew = timedelta(seconds=skew_seconds)
    if now + skew < not_before:
        raise SsoError("SAML assertion is not yet valid.")
    if now - skew >= not_on_or_after:
        raise SsoError("SAML assertion has expired.")

    expected_audience = config.get("sp_entity_id") or ""
    restrictions = conditions.findall("saml:AudienceRestriction", _SAML_NS)
    if not restrictions:
        raise SsoError("SAML assertion has no AudienceRestriction.")
    audiences = {
        (el.text or "").strip()
        for restriction in restrictions
        for el in restriction.findall("saml:Audience", _SAML_NS)
    }
    if expected_audience not in audiences:
        raise SsoError("SAML assertion audience {0} does not include this service provider.".format(
            sorted(audiences)))

    # SubjectConfirmationData, when present, pins the assertion to our ACS URL and its own
    # shorter expiry. Absent is tolerated (some IdPs omit it); present and WRONG is not.
    expected_acs = (config.get("acs_url") or "").strip()
    for data in assertion.findall(
            "saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", _SAML_NS):
        recipient = (data.get("Recipient") or "").strip()
        if recipient and expected_acs and recipient != expected_acs:
            raise SsoError("SAML SubjectConfirmationData Recipient does not match this ACS URL.")
        scd_expiry = _parse_saml_datetime(data.get("NotOnOrAfter"))
        if scd_expiry is not None and now - skew >= scd_expiry:
            raise SsoError("SAML SubjectConfirmation has expired.")


def parse_and_verify_saml_response(
    config: Dict[str, Any], saml_response_b64: str, storage: Any = None, tenant_id: Optional[str] = None
) -> Dict[str, Any]:
    """Verify the IdP's XML-DSig signature over a SAML Response against the configured
    certificate and return the asserted identity {email, name, attributes}.

    `storage`/`tenant_id` enable single-use enforcement. They are optional so the pure
    verification logic stays testable without a database, but the ACS route always passes them --
    without them a captured assertion replays for its whole validity window.
    """
    from lxml import etree  # local import: SAML is optional at runtime
    from signxml import XMLVerifier

    try:
        xml_bytes = base64.b64decode(saml_response_b64)
    except Exception as exc:
        raise SsoError("SAMLResponse is not valid base64: {0}".format(exc)) from exc
    try:
        root = etree.fromstring(xml_bytes, parser=_saml_parser())  # noqa: S320 - entities disabled, signature verified below
    except Exception as exc:
        raise SsoError("SAMLResponse is not valid XML: {0}".format(exc)) from exc

    # Destination, when the IdP sets it, must name our ACS endpoint -- it is the Response-level
    # counterpart to SubjectConfirmationData's Recipient.
    expected_acs = (config.get("acs_url") or "").strip()
    destination = (root.get("Destination") or "").strip()
    if destination and expected_acs and destination != expected_acs:
        raise SsoError("SAMLResponse Destination does not match this ACS URL.")

    cert_pem = _cert_to_pem(config.get("idp_cert", ""))
    if not cert_pem or "BEGIN CERTIFICATE" not in cert_pem:
        raise SsoError("No IdP certificate configured for SAML signature verification.")
    try:
        # require_x509=False: trust the pinned cert we were configured with, not one
        # embedded in the document. This is what actually authenticates the assertion.
        verified = XMLVerifier().verify(root, x509_cert=cert_pem, require_x509=False)
        signed = verified.signed_xml  # type: ignore[union-attr]
    except Exception as exc:
        raise SsoError("SAML signature verification failed: {0}".format(exc)) from exc

    # The assertion MUST come from inside the signed subtree. There used to be a fallback that
    # pulled one out of `root` -- the UNVERIFIED document -- when the signed subtree had none.
    # That is textbook XML Signature Wrapping: an attacker takes any genuinely signed assertion
    # (so verification passes), moves it somewhere the signature still covers, and inserts their
    # OWN unsigned assertion in the place this code reads from. The fallback made a valid
    # signature over someone else's identity into a login as anyone.
    #
    # signxml types verify() as VerifyResult | list[VerifyResult]; a SAML Response carries
    # exactly one signed assertion, so the single-result form is what comes back here.
    if signed.tag.endswith("}Assertion"):  # type: ignore[union-attr]
        assertion = signed
    else:
        assertion = signed.find(".//saml:Assertion", _SAML_NS)  # type: ignore[union-attr]
    if assertion is None:
        raise SsoError("No signed assertion found in SAMLResponse.")

    _validate_saml_conditions(assertion, config)

    # Single use. SAML has no built-in replay defence -- the assertion is a signed bearer token --
    # so an assertion captured from a proxy log or a shared browser would otherwise work for its
    # entire validity window, every time.
    if storage is not None:
        assertion_id = (assertion.get("ID") or "").strip()
        if not assertion_id:
            raise SsoError("SAML assertion has no ID, so single-use cannot be enforced.")
        conditions = assertion.find("saml:Conditions", _SAML_NS)
        expiry = _parse_saml_datetime(conditions.get("NotOnOrAfter")) if conditions is not None else None
        if not storage.claim_saml_assertion(assertion_id, not_on_or_after=expiry, tenant_id=tenant_id):
            raise SsoError("This SAML assertion has already been used.")

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
