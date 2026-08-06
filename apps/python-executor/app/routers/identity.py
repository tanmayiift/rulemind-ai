"""Identity endpoints — access management (RBAC roles, API keys, members, SSO config) and human
auth (password/OTP login, session, logout, OIDC/SAML SSO flows). Extracted verbatim from
app/main.py. Stable helpers/models imported by value from app.main; direct storage calls use
main.storage live."""
from __future__ import annotations

import os
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from .. import main
from ..context import get_current_api_key_id
from ..main import (
    AccessKeyRequest,
    KeyRoleRequest,
    LoginRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    OidcCallbackRequest,
    OtpRequestRequest,
    OtpVerifyRequest,
    SsoConfigRequest,
    active_role,
    active_tenant_id,
)
from ..session_cookie import clear_session_cookies, session_token_from_request, set_session_cookies

router = APIRouter()


# ── Access: self, roles, keys ──────────────────────────────────────────────
@router.get("/api/v1/access/me")
def access_me() -> Dict[str, Any]:
    """The caller's own role + capabilities — lets the UI adapt to permissions."""
    from ..rbac import capabilities_for

    role = active_role()
    return {"role": role, "capabilities": sorted(capabilities_for(role))}


@router.get("/api/v1/access/roles")
def access_roles() -> Dict[str, Any]:
    """Reference: the assignable roles, their capabilities, and descriptions."""
    from ..rbac import ASSIGNABLE_ROLES, ROLE_CAPABILITIES, ROLE_DESCRIPTIONS

    return {
        "assignable": ASSIGNABLE_ROLES,
        "roles": [
            {"role": r, "capabilities": sorted(ROLE_CAPABILITIES[r]), "description": ROLE_DESCRIPTIONS.get(r, "")}
            for r in ["owner", *ASSIGNABLE_ROLES]
        ],
    }


@router.get("/api/v1/access/keys")
def access_list_keys() -> List[Dict[str, Any]]:
    """List this workspace's API keys with their roles (keys are masked)."""
    key_id = get_current_api_key_id()
    keys = main.storage.list_api_keys(active_tenant_id())
    for k in keys:
        if key_id and k.get("id") == key_id:
            k["is_current"] = True
    return keys


def _audit_access(event_type: str, tenant_id: str, entity_id: Optional[str], detail: str, metadata: Dict[str, Any]) -> None:
    """Write an access-management audit event (key issue/revoke) — a compliance
    requirement for regulated tenants. Best-effort; never fails the request."""
    try:
        main.storage.add_audit_event({
            "tenant_id": tenant_id, "event_type": event_type, "entity_type": "api_key",
            "entity_id": entity_id or "", "detail": detail,
            "metadata": {**metadata, "actor_api_key_id": get_current_api_key_id(), "actor_role": active_role()},
        }, tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit best effort
        pass


@router.post("/api/v1/access/keys")
def access_create_key(request: AccessKeyRequest) -> Dict[str, Any]:
    """Issue a new role-scoped API key for this workspace (requires manage_access)."""
    from ..rbac import ASSIGNABLE_ROLES

    if request.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=422, detail="Role must be one of: {0}".format(", ".join(ASSIGNABLE_ROLES)))
    tenant_id = active_tenant_id()
    created = main.storage.generate_api_key_for_tenant(
        tenant_id, environment=request.environment, label=request.label, role=request.role,
    )
    _audit_access("api_key_issued", tenant_id, created.get("kid"),
                  "Issued {0} key '{1}' ({2})".format(request.role, request.label or "—", created.get("kid")),
                  {"role": request.role, "environment": request.environment})
    return created


@router.delete("/api/v1/access/keys/{kid}")
def access_revoke_key(kid: str) -> Dict[str, Any]:
    """Revoke an API key by its kid (requires manage_access). Cannot revoke the key
    used for this very request."""
    tenant_id = active_tenant_id()
    keys = main.storage.list_api_keys(tenant_id)
    current = next((k for k in keys if k.get("id") == get_current_api_key_id()), None)
    if current and current.get("kid") == kid:
        raise HTTPException(status_code=409, detail="You cannot revoke the key you are currently using.")
    revoked = next((k for k in keys if k.get("kid") == kid), None)
    if not main.storage.revoke_api_key(tenant_id, kid):
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    _audit_access("api_key_revoked", tenant_id, kid,
                  "Revoked key '{0}'".format(kid), {"role": (revoked or {}).get("role")})
    return {"revoked": True, "kid": kid}


@router.patch("/api/v1/access/keys/{kid}/role")
def access_update_key_role(kid: str, request: KeyRoleRequest) -> Dict[str, Any]:
    """Change an API key's role in place (requires manage_access). Cannot change the
    role of the key used for this request (avoids self-lockout mid-session)."""
    from ..rbac import ASSIGNABLE_ROLES

    if request.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=422, detail="Role must be one of: {0}".format(", ".join(ASSIGNABLE_ROLES)))
    tenant_id = active_tenant_id()
    keys = main.storage.list_api_keys(tenant_id)
    current = next((k for k in keys if k.get("id") == get_current_api_key_id()), None)
    if current and current.get("kid") == kid:
        raise HTTPException(status_code=409, detail="You cannot change the role of the key you are currently using.")
    updated = main.storage.update_api_key_role(tenant_id, kid, request.role)
    if not updated:
        raise HTTPException(status_code=404, detail="Key not found or revoked.")
    _audit_access("api_key_role_changed", tenant_id, kid,
                  "Changed key '{0}' role to {1}".format(kid, request.role), {"role": request.role})
    return updated


# ── Access: workspace members (human accounts with RBAC roles) ─────────────
def _audit_member(event_type: str, tenant_id: str, member_id: Optional[str], detail: str, metadata: Dict[str, Any]) -> None:
    try:
        main.storage.add_audit_event({
            "tenant_id": tenant_id, "event_type": event_type, "entity_type": "member",
            "entity_id": member_id or "", "detail": detail,
            "metadata": {**metadata, "actor_api_key_id": get_current_api_key_id(), "actor_role": active_role()},
        }, tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit best effort
        pass


@router.get("/api/v1/access/members")
def access_list_members() -> List[Dict[str, Any]]:
    """List the workspace's human members and their roles (requires manage_access)."""
    return main.storage.list_members(active_tenant_id())


@router.post("/api/v1/access/members")
def access_create_member(request: MemberCreateRequest) -> Dict[str, Any]:
    """Invite/create a human member with a role (requires manage_access)."""
    from ..rbac import ASSIGNABLE_ROLES

    if request.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=422, detail="Role must be one of: {0}".format(", ".join(ASSIGNABLE_ROLES)))
    tenant_id = active_tenant_id()
    try:
        member = main.storage.create_member(tenant_id, request.email, request.name or request.email,
                                             request.role, password=request.password)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit_member("member_created", tenant_id, member["id"],
                  "Created member '{0}' ({1})".format(member["email"], request.role), {"role": request.role})
    return member


@router.patch("/api/v1/access/members/{member_id}")
def access_update_member(member_id: str, request: MemberUpdateRequest) -> Dict[str, Any]:
    """Change a member's role/status/name/password in place (requires manage_access).
    A role change takes effect on the member's next request (session cache is cleared)."""
    from ..rbac import ASSIGNABLE_ROLES

    if request.role is not None and request.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=422, detail="Role must be one of: {0}".format(", ".join(ASSIGNABLE_ROLES)))
    tenant_id = active_tenant_id()
    patch = {k: v for k, v in request.model_dump().items() if v is not None}
    updated = main.storage.update_member(tenant_id, member_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found.")
    _audit_member("member_updated", tenant_id, member_id,
                  "Updated member '{0}'".format(updated["email"]),
                  {k: patch[k] for k in ("role", "is_active") if k in patch})
    return updated


@router.delete("/api/v1/access/members/{member_id}")
def access_deactivate_member(member_id: str) -> Dict[str, Any]:
    """Deactivate a member (requires manage_access). Their sessions stop working
    immediately. Soft-disable rather than hard-delete, preserving the audit trail."""
    tenant_id = active_tenant_id()
    updated = main.storage.update_member(tenant_id, member_id, {"is_active": False})
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found.")
    _audit_member("member_deactivated", tenant_id, member_id,
                  "Deactivated member '{0}'".format(updated["email"]), {})
    return {"deactivated": True, "id": member_id}


# ── Human login (password + email OTP) ─────────────────────────────────────
@router.post("/api/v1/auth/login")
def member_login(request: LoginRequest, response: Response) -> Dict[str, Any]:
    """Password login for a workspace member. Sets an httpOnly session cookie (browser) + a CSRF
    cookie, and also returns the token in the body for machine/mobile callers."""
    member = main.storage.get_member_by_email(request.email, tenant_id=request.tenant_id)
    if not member or not member.get("is_active"):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not main.storage.verify_member_password(member["id"], request.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    session = main.storage.create_member_session(member["tenant_id"], member["id"])
    _audit_member("member_login", member["tenant_id"], member["id"],
                  "Password login by '{0}'".format(member["email"]), {"method": "password"})
    csrf = set_session_cookies(response, session["token"])
    return {"token": session["token"], "expires_at": session["expires_at"], "member": member, "csrf_token": csrf}


@router.post("/api/v1/auth/otp/request")
def member_otp_request(request: OtpRequestRequest) -> Dict[str, Any]:
    """Email a one-time login code. Always returns the same shape whether or not the
    email maps to a member (no account enumeration). The code is emailed when SMTP is
    configured; in a dev workspace (AUTH_MODE!=production) it is also returned inline."""
    member = main.storage.get_member_by_email(request.email, tenant_id=request.tenant_id)
    response: Dict[str, Any] = {"requested": True}
    if not member or not member.get("is_active"):
        return response
    code = main.storage.issue_member_otp(member["tenant_id"], member["email"])
    body = "Your RuleMind login code is {0}. It expires in 10 minutes.".format(code)
    try:
        email_cfg = main.storage.get_email_credentials(tenant_id=member["tenant_id"])
        from .. import mailer
        delivery = mailer.send_text_email(email_cfg, [member["email"]], "Your RuleMind login code", body)
    except Exception:  # pragma: no cover - delivery best effort
        delivery = {"delivered": False, "transport": "error"}
    response["delivered"] = bool(delivery.get("delivered"))
    # Dev convenience: surface the code inline when email isn't wired up and we're
    # not in a production deployment, so login is testable end-to-end.
    if not delivery.get("delivered") and os.getenv("AUTH_MODE") != "production":
        response["debug_code"] = code
    return response


@router.post("/api/v1/auth/otp/verify")
def member_otp_verify(request: OtpVerifyRequest, response: Response) -> Dict[str, Any]:
    """Exchange a valid OTP for a session — sets the httpOnly session + CSRF cookies (browser)
    and returns the token for machine/mobile callers."""
    member = main.storage.get_member_by_email(request.email, tenant_id=request.tenant_id)
    if not member or not member.get("is_active"):
        raise HTTPException(status_code=401, detail="Invalid or expired code.")
    if not main.storage.verify_member_otp(member["tenant_id"], member["email"], request.code):
        raise HTTPException(status_code=401, detail="Invalid or expired code.")
    session = main.storage.create_member_session(member["tenant_id"], member["id"])
    _audit_member("member_login", member["tenant_id"], member["id"],
                  "OTP login by '{0}'".format(member["email"]), {"method": "otp"})
    csrf = set_session_cookies(response, session["token"])
    return {"token": session["token"], "expires_at": session["expires_at"], "member": member, "csrf_token": csrf}


@router.get("/api/v1/auth/session")
def member_session(http_request: Request) -> Dict[str, Any]:
    """The current member for a bearer session token, with role + capabilities."""
    from ..rbac import capabilities_for

    resolved = main.storage.resolve_member_session(session_token_from_request(http_request))
    if not resolved:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    role = resolved.get("role", "viewer")
    return {"member": resolved.get("member"), "role": role, "capabilities": sorted(capabilities_for(role))}


@router.post("/api/v1/auth/logout")
def member_logout(http_request: Request, response: Response) -> Dict[str, Any]:
    """Revoke the current session and clear its cookies."""
    token = session_token_from_request(http_request)
    revoked = main.storage.revoke_member_session(token) if token else False
    clear_session_cookies(response)
    return {"logged_out": bool(revoked)}


# ── Enterprise SSO (OIDC / SAML) ───────────────────────────────────────────
@router.get("/api/v1/access/sso")
def access_get_sso() -> Dict[str, Any]:
    """The workspace's SSO connection config (secrets masked). Requires manage_access."""
    return main.storage.get_sso_config_masked(active_tenant_id())


@router.put("/api/v1/access/sso")
def access_set_sso(request: SsoConfigRequest) -> Dict[str, Any]:
    """Configure the workspace's OIDC or SAML connection (requires manage_access)."""
    from ..rbac import ASSIGNABLE_ROLES

    patch = {k: v for k, v in request.model_dump().items() if v is not None}
    if "default_role" in patch and patch["default_role"] not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=422, detail="default_role must be one of: {0}".format(", ".join(ASSIGNABLE_ROLES)))
    if "provider" in patch and patch["provider"] not in ("oidc", "saml"):
        raise HTTPException(status_code=422, detail="provider must be 'oidc' or 'saml'.")
    tenant_id = active_tenant_id()
    result = main.storage.set_sso_config(patch, tenant_id=tenant_id)
    _audit_member("sso_configured", tenant_id, None,
                  "Updated SSO ({0}, enabled={1})".format(result.get("provider"), result.get("enabled")),
                  {"provider": result.get("provider"), "enabled": result.get("enabled")})
    return result


def _sso_finish_login(tenant_id: str, identity: Dict[str, Any], provider: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Map a verified SSO identity to a member (JIT-provisioning if allowed) and issue
    a bearer session — shared by the OIDC and SAML callbacks."""
    from .. import sso

    email = identity["email"]
    if not sso.email_domain_allowed(email, cfg.get("allowed_domains")):
        raise HTTPException(status_code=403, detail="Your email domain is not permitted for this workspace.")
    member = main.storage.get_member_by_email(email, tenant_id=tenant_id)
    if member and not member.get("is_active"):
        raise HTTPException(status_code=403, detail="This account is deactivated.")
    if not member:
        if not cfg.get("jit_provisioning", True):
            raise HTTPException(status_code=403, detail="No account for this email, and just-in-time provisioning is off.")
        member = main.storage.create_member(
            tenant_id, email, identity.get("name") or email,
            cfg.get("default_role", "viewer"), password=None, auth_provider=provider,
        )
    session = main.storage.create_member_session(member["tenant_id"], member["id"])
    _audit_member("member_login", member["tenant_id"], member["id"],
                  "SSO login by '{0}' via {1}".format(email, provider), {"method": provider})
    return {"token": session["token"], "expires_at": session["expires_at"], "member": member}


@router.get("/api/v1/auth/sso/available")
def sso_available(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Public: whether SSO is enabled for a workspace (so the login screen can show
    the button). Exposes only the on/off flag and protocol — never any config/secret."""
    resolved_tenant = tenant_id or main.storage.default_tenant_id
    cfg = main.storage.get_sso_config_masked(tenant_id=resolved_tenant)
    return {"enabled": bool(cfg.get("enabled")), "provider": cfg.get("provider", "oidc")}


@router.get("/api/v1/auth/sso/start")
def sso_start(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Begin an SSO login: returns the IdP redirect URL the browser should navigate to.
    Public (identity is not yet established)."""
    from .. import sso

    resolved_tenant = tenant_id or main.storage.default_tenant_id
    cfg = main.storage.get_sso_config_internal(tenant_id=resolved_tenant)
    if not cfg.get("enabled"):
        raise HTTPException(status_code=404, detail="SSO is not enabled for this workspace.")
    provider = cfg.get("provider", "oidc")
    nonce = secrets.token_urlsafe(16)
    try:
        if provider == "oidc":
            state = sso.issue_state(resolved_tenant, provider, nonce, cfg.get("redirect_uri", ""))
            return {"provider": "oidc", "redirect_url": sso.oidc_authorize_url(cfg, state, nonce)}
        state = sso.issue_state(resolved_tenant, provider, nonce, cfg.get("acs_url", ""))
        return {"provider": "saml", "redirect_url": sso.saml_authn_request_url(cfg, state)}
    except sso.SsoError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/v1/auth/sso/oidc/callback")
def sso_oidc_callback(request: OidcCallbackRequest) -> Dict[str, Any]:
    """Complete an OIDC login: validate state, exchange the code, verify the ID token,
    and issue a bearer session. Public."""
    from .. import sso

    try:
        state = sso.verify_state(request.state)
        tenant_id = state["t"]
        cfg = main.storage.get_sso_config_internal(tenant_id=tenant_id)
        if not cfg.get("enabled") or cfg.get("provider") != "oidc":
            raise HTTPException(status_code=404, detail="OIDC is not enabled for this workspace.")
        identity = sso.complete_oidc_login(cfg, request.code, state.get("n", ""))
    except sso.SsoError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return _sso_finish_login(tenant_id, identity, "oidc", cfg)


@router.post("/api/v1/auth/sso/saml/acs")
async def sso_saml_acs(http_request: Request) -> Dict[str, Any]:
    """SAML assertion consumer: verify the IdP's signature and issue a bearer session.
    Accepts a browser form POST (SAMLResponse/RelayState) or a JSON body. Public."""
    from .. import sso

    saml_response = relay_state = None
    ctype = http_request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await http_request.json()
        saml_response, relay_state = body.get("saml_response"), body.get("relay_state")
    else:
        form = await http_request.form()
        saml_response = form.get("SAMLResponse") or form.get("saml_response")
        relay_state = form.get("RelayState") or form.get("relay_state")
    if not saml_response or not relay_state:
        raise HTTPException(status_code=400, detail="Missing SAMLResponse or RelayState.")
    try:
        state = sso.verify_state(str(relay_state))
        tenant_id = state["t"]
        cfg = main.storage.get_sso_config_internal(tenant_id=tenant_id)
        if not cfg.get("enabled") or cfg.get("provider") != "saml":
            raise HTTPException(status_code=404, detail="SAML is not enabled for this workspace.")
        identity = sso.parse_and_verify_saml_response(cfg, str(saml_response))
    except sso.SsoError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return _sso_finish_login(tenant_id, identity, "saml", cfg)
