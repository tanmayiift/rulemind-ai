"""SCIM 2.0 resource mapping (RFC 7643 / 7644) for workspace members — Track B6.

Enterprise buyers do not want a second place to manage people. SCIM is how Okta, Entra ID and
OneLogin push joiners, movers and leavers into an application automatically, and the leaver
half is the one that gets audited: when someone is terminated in the IdP, their access here
must end without anybody remembering to do it.

This maps SCIM's User resource onto WorkspaceMember, which already carries `external_id`,
`is_active` and per-member session invalidation -- so deprovisioning is a real revocation, not
a flag nobody reads.

Two details that break real IdP integrations if got wrong, and are therefore explicit here:

  * SCIM has its OWN error envelope (urn:...:Error with a STRING status). An IdP parses that
    body; FastAPI's default {"detail": ...} is not SCIM and Okta reports it as an unknown
    failure rather than the 404/409 it is.
  * List pagination is 1-BASED (`startIndex` starts at 1, not 0). Treating it as an offset
    silently drops the first user of every page -- and the symptom shows up as "one user never
    provisions", which is very hard to trace back to an off-by-one.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SERVICE_PROVIDER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"

CONTENT_TYPE = "application/scim+json"

# SCIM has no notion of our RBAC roles, so a provisioned user needs a default. `viewer` is the
# least-privileged role: an IdP that creates a user must never be able to mint an admin by
# omission. Elevation stays a deliberate act in RuleMind.
DEFAULT_ROLE = "viewer"


def error(status: int, detail: str, scim_type: Optional[str] = None) -> Dict[str, Any]:
    """The SCIM error envelope. `status` is a STRING per RFC 7644 s3.12."""
    body: Dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return body


def to_scim_user(member: Dict[str, Any], base_url: str = "/scim/v2") -> Dict[str, Any]:
    """WorkspaceMember -> SCIM User."""
    name = (member.get("name") or "").strip()
    given, _, family = name.partition(" ")
    resource = {
        "schemas": [USER_SCHEMA],
        "id": member["id"],
        "userName": member.get("email"),
        "name": {"formatted": name, "givenName": given or name, "familyName": family or ""},
        "displayName": name or member.get("email"),
        "emails": [{"value": member.get("email"), "primary": True, "type": "work"}],
        # `active` is the field that carries a termination. It maps to is_active, which
        # member session resolution reads live.
        "active": bool(member.get("is_active", True)),
        "meta": {
            "resourceType": "User",
            "created": member.get("created_at"),
            "lastModified": member.get("updated_at") or member.get("created_at"),
            "location": f"{base_url}/Users/{member['id']}",
        },
    }
    if member.get("external_id"):
        resource["externalId"] = member["external_id"]
    return resource


def from_scim_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    """SCIM User -> the fields create_member/update_member accept.

    `userName` is the email: SCIM's uniqueness key for a User is userName, and our uniqueness
    key is (tenant_id, email), so conflating them keeps one identity per person rather than
    letting an IdP create a second account that differs only in case or spelling.
    """
    email = (payload.get("userName") or "").strip()
    name_obj = payload.get("name") or {}
    display = (payload.get("displayName")
               or name_obj.get("formatted")
               or " ".join(x for x in (name_obj.get("givenName"), name_obj.get("familyName")) if x).strip()
               or email)
    fields: Dict[str, Any] = {
        "email": email,
        "name": display,
        "is_active": bool(payload.get("active", True)),
    }
    if payload.get("externalId"):
        fields["external_id"] = str(payload["externalId"])
    return fields


def list_response(resources: List[Dict[str, Any]], total: int, start_index: int,
                  per_page: int) -> Dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": per_page,
        "Resources": resources,
    }


_FILTER = re.compile(r'^\s*(?P<attr>[\w.]+)\s+(?P<op>eq|ne)\s+"(?P<value>[^"]*)"\s*$', re.IGNORECASE)


def parse_filter(expression: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """Parse the ONE filter shape IdPs actually send during provisioning.

    Okta and Entra both probe with `userName eq "someone@example.com"` before creating a user,
    to decide between POST and PATCH. Supporting exactly that (plus `externalId eq`) is the
    difference between working and not working; the rest of RFC 7644's filter grammar --
    `and`/`or`/`co`/`sw`/complex paths -- is not exercised by provisioning and is deliberately
    not implemented rather than half-implemented.

    Returns (attribute, operator, value), or None when there is no filter.
    Raises ValueError for a filter we cannot honour, so the caller can answer 400 rather than
    silently returning every user -- which an IdP would read as "no match" and duplicate.
    """
    if not expression or not expression.strip():
        return None
    match = _FILTER.match(expression)
    if not match:
        raise ValueError(f"Unsupported filter: {expression}")
    attr = match.group("attr")
    if attr.lower() not in {"username", "externalid", "active"}:
        raise ValueError(f"Unsupported filter attribute: {attr}")
    return attr.lower(), match.group("op").lower(), match.group("value")


def apply_patch(member: Dict[str, Any], operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply a PATCH op list, returning the fields to update.

    Deprovisioning arrives here, and IdPs disagree on its exact shape -- Okta sends
    `{"op":"replace","value":{"active":false}}` while others send
    `{"op":"replace","path":"active","value":false}`. Both are legal; handling only one means
    terminations silently do not apply, which is the single worst bug this feature can have.
    """
    patch: Dict[str, Any] = {}
    for operation in operations or []:
        op = str(operation.get("op", "")).lower()
        if op == "remove":
            removed = str(operation.get("path", "")).lower()
            if removed == "active":
                patch["is_active"] = False
            continue
        if op not in {"replace", "add"}:
            continue
        target = operation.get("path")
        value = operation.get("value")
        if target:
            key = str(target).lower()
            if key == "active":
                patch["is_active"] = _as_bool(value)
            elif key in {"username", "emails[type eq \"work\"].value"}:
                patch["email"] = value
            elif key in {"displayname", "name.formatted"}:
                patch["name"] = value
            elif key == "externalid":
                patch["external_id"] = value
        elif isinstance(value, dict):
            if "active" in value:
                patch["is_active"] = _as_bool(value["active"])
            if value.get("userName"):
                patch["email"] = value["userName"]
            if value.get("displayName"):
                patch["name"] = value["displayName"]
            if value.get("externalId"):
                patch["external_id"] = value["externalId"]
    return patch


def _as_bool(value: Any) -> bool:
    """IdPs send `active` as a JSON boolean, but some send the STRING "False". Treating that
    string as truthy would leave a terminated employee's access enabled."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def service_provider_config() -> Dict[str, Any]:
    """Advertise honestly what is supported. An IdP reads this to decide which calls to make,
    so claiming filter/patch support we lack would make it send requests that then fail."""
    return {
        "schemas": [SERVICE_PROVIDER_SCHEMA],
        "documentationUri": "https://github.com/tanmayiift/rulemind-ai",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken",
            "name": "API key",
            "description": "Send the workspace API key as x-api-key or as a bearer token.",
            "primary": True,
        }],
        "meta": {"resourceType": "ServiceProviderConfig", "location": "/scim/v2/ServiceProviderConfig"},
    }
