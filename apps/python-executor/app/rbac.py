"""Role-based access control for the tenant API.

Every API key carries a role. A role grants a set of capabilities; each request
is mapped (by method + path) to the single capability it needs, and the
middleware allows it only if the caller's role holds that capability.

Capabilities
------------
* read          — any GET/HEAD (view any resource)
* decide        — run decisions / simulations / SDK evaluation
* author        — create / edit / delete authoring resources (connectors,
                  variables, rules, scorecards, policies, decision tables,
                  experiments, models, schedules, webhooks, bundles)
* review        — act on the human review queue
* deploy        — promote assets across environments (maker → checker)
* manage_access — manage API keys, roles, branding/settings (workspace admin)

Roles (admin-assignable per key)
--------------------------------
* owner        — everything (the bootstrap/default role; never lock-out)
* admin        — everything
* policy_maker — read + decide + author (but not review-act, deploy, or admin)
* reviewer     — read + decide + review
* viewer       — read only
"""
from __future__ import annotations

from typing import Dict, Set

READ = "read"
DECIDE = "decide"
AUTHOR = "author"
REVIEW = "review"
DEPLOY = "deploy"
MANAGE_ACCESS = "manage_access"

ALL_CAPABILITIES = {READ, DECIDE, AUTHOR, REVIEW, DEPLOY, MANAGE_ACCESS}

ROLE_CAPABILITIES: Dict[str, Set[str]] = {
    "owner": set(ALL_CAPABILITIES),
    "admin": set(ALL_CAPABILITIES),
    "policy_maker": {READ, DECIDE, AUTHOR},
    "reviewer": {READ, DECIDE, REVIEW},
    "viewer": {READ},
}

# Admin-assignable roles, in descending privilege (owner is the bootstrap role and
# is intentionally not offered in the UI — it is what seed/default keys carry).
ASSIGNABLE_ROLES = ["admin", "policy_maker", "reviewer", "viewer"]
DEFAULT_ROLE = "owner"

ROLE_DESCRIPTIONS = {
    "owner": "Full access, including workspace ownership. The bootstrap role.",
    "admin": "Full access: author, decide, review, deploy, and manage keys/roles.",
    "policy_maker": "Author connectors, variables, rules, scorecards, policies, and decision tables; run decisions. Cannot act on reviews, deploy, or manage access.",
    "reviewer": "Act on the review queue and run decisions. Read-only elsewhere.",
    "viewer": "Read-only access to everything.",
}

# Path-prefix → capability for write methods. First match wins; longest/most
# specific prefixes are listed before their parents.
_WRITE_PREFIX_CAPABILITY = [
    ("/api/v1/access", MANAGE_ACCESS),
    ("/api/v1/settings", MANAGE_ACCESS),
    ("/api/v1/branding", MANAGE_ACCESS),
    ("/api/v1/reports/email-config", MANAGE_ACCESS),
    ("/api/v1/reviews", REVIEW),
    ("/api/v1/promotions", DEPLOY),
    ("/api/v1/deploy", DEPLOY),
]

# Authoring resource prefixes — write methods here need the AUTHOR capability.
_AUTHOR_PREFIXES = (
    "/api/v1/connectors",
    "/api/v1/variables",
    "/api/v1/rules",
    "/api/v1/scorecards",
    "/api/v1/policies",
    "/api/v1/decision-tables",
    "/api/v1/experiments",
    "/api/v1/models",
    "/api/v1/schedules",
    "/api/v1/webhooks",
    "/api/v1/bundles",
    "/api/v1/segments",
)

# Write paths that are really "run a decision", not authoring.
_DECIDE_PREFIXES = (
    "/api/v1/decide",
    "/api/v1/simulate",
    "/api/v1/test",
    "/sdk/v1",
    "/api/v1/workflows",  # loop-debug / callbacks / resume — evaluation, not authoring
)


def required_capability(method: str, path: str) -> str:
    """Map a request to the single capability it requires."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return READ
    # `/policies/{id}/promote` is a deploy action even though it sits under /policies.
    if path.startswith("/api/v1/policies") and path.endswith("/promote"):
        return DEPLOY
    for prefix, capability in _WRITE_PREFIX_CAPABILITY:
        if path.startswith(prefix):
            return capability
    if path.startswith(_DECIDE_PREFIXES):
        return DECIDE
    if path.startswith(_AUTHOR_PREFIXES):
        return AUTHOR
    # Unknown write: require AUTHOR (blocks viewers, allows policy makers/admins).
    return AUTHOR


def capabilities_for(role: str) -> Set[str]:
    return ROLE_CAPABILITIES.get(role or DEFAULT_ROLE, ROLE_CAPABILITIES[DEFAULT_ROLE])


def is_allowed(role: str, method: str, path: str) -> bool:
    return required_capability(method, path) in capabilities_for(role)


def normalize_role(role: str) -> str:
    role = (role or "").strip().lower()
    return role if role in ROLE_CAPABILITIES else DEFAULT_ROLE
