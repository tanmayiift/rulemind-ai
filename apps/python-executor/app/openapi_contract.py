"""Finishes the generated OpenAPI document so it describes the API that actually runs (plan 5.1).

FastAPI's generator gets three things wrong for this app, each of which a generated client
inherits:

  * **Errors.** It documents every 422 as `HTTPValidationError` -- `{detail: [...]}` with no
    `error` field. Since R10.18 every error, from every layer, is the envelope
    `{error, detail}`, so the document was wrong on every operation that takes a body, and said
    nothing at all about 401/403/404/429/5xx. Replaced with `ValidationErrorResponse` on 422 and
    `ErrorResponse` for every other 4XX/5XX.
  * **Authentication.** Zero `securitySchemes`: the document never said an API key was needed,
    or where it goes. Added, per operation, from the same rules the middleware applies --
    `tests/test_api_contract.py` sends every documented operation without credentials and
    fails if the document and the middleware disagree about which ones need them.
  * **Typed responses.** Routes document their response model with `responses={200: ...}` and
    keep FastAPI's inferred `Dict[str, Any]` field for serialisation (pydantic-core serialises
    a /decide body ~75us faster than `jsonable_encoder`, which is what `response_model=None`
    would switch to). The generator then deep-merges the two schemas into
    `{"$ref": ..., "type": "object", "additionalProperties": true}`; the inferred half is
    dropped here so the reference stands alone.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .api_models import ErrorResponse, ValidationErrorResponse

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

SECURITY_SCHEMES: Dict[str, Dict[str, Any]] = {
    "ApiKey": {
        "type": "apiKey", "in": "header", "name": "x-api-key",
        "description": "A workspace API key, from Settings -> Access or `POST /api/v1/access/keys`.",
    },
    "Bearer": {
        "type": "http", "scheme": "bearer",
        "description": "A sign-in session token (`POST /api/v1/auth/login`) or an OAuth2 access token.",
    },
    "OAuth2": {
        "type": "oauth2",
        "description": "Client credentials (RFC 6749 section 4.4): an API key's id and secret.",
        "flows": {"clientCredentials": {"tokenUrl": "/api/v1/oauth/token", "scopes": {}}},
    },
    "Session": {"type": "apiKey", "in": "cookie", "name": "rm_session",
                "description": "The dashboard's sign-in cookie."},
    "AdminSession": {"type": "apiKey", "in": "cookie", "name": "rulemind_admin_session",
                     "description": "The platform-admin console's cookie."},
    "AdminBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                    "description": "A platform-admin token from `POST /api/mobile/v1/auth/login`."},
}

TENANT = [{"ApiKey": []}, {"Bearer": []}, {"OAuth2": []}, {"Session": []}]
SIGNED_IN = [{"Bearer": []}, {"Session": []}]
ADMIN = [{"AdminSession": []}]
MOBILE_ADMIN = [{"AdminBearer": []}]
PUBLIC: List[Dict[str, List[str]]] = []

# /api/v1/auth/* manages its own identity (middleware.UNAUTH_AUTH_PREFIXES). /session reads the
# caller's session itself, so it needs one; the rest establish it.
_SIGNED_IN_AUTH_PATHS = {"/api/v1/auth/session"}
# The platform console's own sign-in and sign-out establish or clear the cookie; the mobile
# console's sign-in and demo access issue its token.
_PUBLIC_ADMIN_PATHS = {"/api/admin/v1/auth/login", "/api/admin/v1/auth/logout",
                       "/api/mobile/v1/auth/login", "/api/mobile/v1/auth/demo"}


def auth_requirement(method: str, path: str) -> List[Dict[str, List[str]]]:
    """Which credentials an operation needs, by the rules `TenantContextMiddleware` applies."""
    from .middleware import EXEMPT_PATHS, UNAUTH_AUTH_PREFIXES

    if path in EXEMPT_PATHS or path in _PUBLIC_ADMIN_PATHS:
        return PUBLIC
    if path.startswith("/api/admin/v1"):
        return ADMIN
    if path.startswith("/api/mobile/v1"):
        return MOBILE_ADMIN
    if not path.startswith(("/api/v1", "/sdk/v1", "/scim/v2")):
        return PUBLIC
    if path.startswith(UNAUTH_AUTH_PREFIXES):
        return SIGNED_IN if path in _SIGNED_IN_AUTH_PATHS else PUBLIC
    # Inbound webhooks authenticate by their HMAC signature, checked in the handler.
    if method == "post" and path.startswith("/api/v1/webhooks/") and not path.endswith("/test"):
        return PUBLIC
    return TENANT


def _schema_of(model: Any, components: Dict[str, Any]) -> Dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, definition in (schema.pop("$defs", None) or {}).items():
        components.setdefault(name, definition)
    components.setdefault(model.__name__, schema)
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _strip_merged_siblings(node: Any) -> None:
    """`{"$ref": X, "type": "object", ...}` -> `{"$ref": X}`, recursively."""
    if isinstance(node, dict):
        if "$ref" in node:
            for key in [k for k in node if k not in ("$ref", "description")]:
                del node[key]
            return
        for value in node.values():
            _strip_merged_siblings(value)
    elif isinstance(node, list):
        for value in node:
            _strip_merged_siblings(value)


def _references(node: Any, found: set) -> set:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _references(value, found)
    elif isinstance(node, list):
        for value in node:
            _references(value, found)
    return found


def finish(spec: Dict[str, Any]) -> Dict[str, Any]:
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    error_ref = _schema_of(ErrorResponse, schemas)
    validation_ref = _schema_of(ValidationErrorResponse, schemas)
    error_content = {"application/json": {"schema": error_ref}}

    for path, item in (spec.get("paths") or {}).items():
        for method, operation in (item or {}).items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            responses = operation.setdefault("responses", {})
            for code, response in responses.items():
                content = (response or {}).get("content") or {}
                if code.startswith("2"):
                    for media in content.values():
                        _strip_merged_siblings(media.get("schema"))
            if "422" in responses:
                responses["422"] = {"description": "The request failed validation.",
                                    "content": {"application/json": {"schema": validation_ref}}}
            responses.setdefault("4XX", {"description": "A client error, in the error envelope.",
                                         "content": error_content})
            responses.setdefault("5XX", {"description": "A server error, in the error envelope.",
                                         "content": error_content})
            operation["security"] = auth_requirement(method, path)

    # FastAPI's own validation schemas are referenced by nothing once 422 is replaced.
    used = _references(spec.get("paths"), set())
    frontier = set(used)
    while frontier:
        frontier = _references([schemas.get(name) for name in frontier], set()) - used
        used |= frontier
    for name in ("HTTPValidationError", "ValidationError"):
        if name not in used:
            schemas.pop(name, None)

    components["securitySchemes"] = SECURITY_SCHEMES
    spec["security"] = TENANT
    info = spec.setdefault("info", {})
    info["description"] = (
        "Authenticate with an `x-api-key` header, or a bearer token from `POST /api/v1/oauth/token`. "
        "Every error is `{error, detail}`: switch on `error`. Response objects may gain fields "
        "without a version bump, so ignore fields you do not know.")
    return spec


def install(app: Any) -> None:
    """Wrap `app.openapi` so the finished document is what every caller gets, cached as before."""
    generate: Callable[[], Dict[str, Any]] = app.openapi

    def openapi() -> Dict[str, Any]:
        if getattr(app, "_contract_finished", False):
            return app.openapi_schema
        spec = finish(generate())
        app.openapi_schema = spec
        app._contract_finished = True
        return spec

    app.openapi = openapi
