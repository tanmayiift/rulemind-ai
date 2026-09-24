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

from typing import Dict, Optional, Set

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

# Two different questions, and conflating them was a fail-open.
#
# DEFAULT_ROLE answers "no role was recorded at all" -- a seed key, a bootstrap key, a row
# written before the column existed. `owner` is deliberate there: those keys ARE the workspace
# owner, and demoting them would lock a fresh install out of itself.
#
# UNKNOWN_ROLE answers "a role was recorded and it is not one we know". That is a typo, a
# hand-edited row, a role renamed in a future release, or a value from a newer node during a
# rolling deploy -- and it used to resolve to `owner` as well, so any of those granted FULL
# ownership rather than the least privilege. `viewer` is the least-privileged defined role, the
# same choice and the same reasoning as scim.py:37: nothing must be able to mint an owner by
# getting a string wrong. The unknown-PATH default was made fail-closed long ago (see
# required_capability below); this is the matching half for the unknown-ROLE case.
DEFAULT_ROLE = "owner"
UNKNOWN_ROLE = "viewer"


def _resolve_role(role: Optional[str]) -> str:
    """Role name to act on. Absent -> DEFAULT_ROLE; present-but-unrecognised -> UNKNOWN_ROLE."""
    cleaned = (role or "").strip().lower()
    if not cleaned:
        return DEFAULT_ROLE
    return cleaned if cleaned in ROLE_CAPABILITIES else UNKNOWN_ROLE

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
    # Listed BEFORE "/api/v1/webhooks" would match, and separately from it: the inbound webhook
    # entity is an AUTHOR prefix (below), while a SUBSCRIPTION is standing egress configuration
    # -- it sends this workspace's decision data to a third party, for every matching event,
    # until someone removes it. That belongs with settings and branding. A policy_maker can
    # already POST to any URL from an action step, so this is not about reaching the network; it
    # is that the role documented as "cannot manage access" should not be able to point the
    # workspace's event stream at a new destination.
    ("/api/v1/webhook-subscriptions", MANAGE_ACCESS),
    # Redelivery re-sends a stored event to a subscription's destination -- the same egress
    # authority as configuring the subscription, so the same capability.
    ("/api/v1/webhook-deliveries", MANAGE_ACCESS),
    # Erasing a subject's personal data is irreversible and workspace-wide.
    ("/api/v1/data-subjects", MANAGE_ACCESS),
    ("/api/v1/settings", MANAGE_ACCESS),
    ("/api/v1/branding", MANAGE_ACCESS),
    ("/api/v1/reports/email-config", MANAGE_ACCESS),
    ("/api/v1/reviews", REVIEW),
    ("/api/v1/promotions", DEPLOY),
    ("/api/v1/deploy", DEPLOY),
]

# Prefixes whose writes are identity operations: provisioning members, superadmin actions, or
# establishing/ending a human session. These were all falling through the unknown-write default
# to AUTHOR, which meant `policy_maker` -- documented as unable to manage access -- could
# provision and DEACTIVATE members through /scim/v2, the workspace owner included.
#
# /api/v1/auth and /api/mobile/v1/auth are listed for completeness of the MAP, not as the
# enforcement point: those routes establish identity before one exists and manage their own
# auth (see middleware's human-auth carve-out), so the middleware does not run this check on
# them. Mapping them keeps the table honest -- an un-carved-out sibling added later inherits
# MANAGE_ACCESS rather than AUTHOR.
_IDENTITY_PREFIXES = (
    "/scim/v2",
    "/api/admin/v1",
    "/api/v1/auth",
    "/api/mobile/v1/auth",
    "/api/v1/oauth",
)

# Authoring resource prefixes — write methods here need the AUTHOR capability.
_AUTHOR_PREFIXES = (
    "/api/v1/connectors",
    # Installing a template creates a connector and variables -- authoring, like POSTing them.
    "/api/v1/connector-templates",
    "/api/v1/variables",
    "/api/v1/rules",
    "/api/v1/scorecards",
    "/api/v1/policies",
    "/api/v1/decision-tables",
    "/api/v1/experiments",
    "/api/v1/schedules",
    "/api/v1/webhooks",
    "/api/v1/bundles",
    "/api/v1/segments",
    "/api/v1/debug",        # P04: stepping a policy is authoring-time inspection; it runs
                            # with simulate=True and writes no decision.
    "/api/v1/journeys",     # P15: a journey is an authoring artifact -- screens and the policy
                            # they feed. Locking one is part of authoring it, not a deploy.
    "/api/v1/model-sets",  # P05: groups models this workspace already has and routes
                           # between them; grants no reach the author lacked.
    "/api/v1/predictors",   # P20: names an existing number and records it in THIS workspace,
                            # so it is authoring -- unlike a webhook subscription, which sends
                            # workspace data to a third party and is manage_access above.
    # Added when the unknown-write default was flipped to fail-closed. Each of these was
    # previously reaching AUTHOR only by falling off the end of the map; they are listed
    # explicitly so the fail-closed default cannot lock an author out of them.
    "/api/v1/ai",             # AI authoring assistance (draft a rule/policy)
    "/api/v1/collab",         # collaborative editing of drafts
    "/api/v1/import",         # bulk import of authoring entities
    "/api/v1/evaluations",    # model/policy evaluation runs
    "/api/v1/reports",        # report generation (email-config stays MANAGE_ACCESS above)
    "/api/v1/analytics",      # analytics recompute
    "/api/v1/onboarding",     # guided setup (signup itself is middleware-exempt)
    "/api/mobile/v1",         # mobile session/tenant selection (auth carved out above)
)

# Write paths that are really "run a decision", not authoring.
_DECIDE_PREFIXES = (
    "/api/v1/decide",
    "/api/v1/simulate",
    "/api/v1/test",
    "/sdk/v1",
    "/api/v1/workflows",  # loop-debug / callbacks / resume — evaluation, not authoring
    "/api/v1/executions",  # resume a paused execution — continuing an evaluation
    "/api/v1/decisions",   # replay a decision — re-running it, not authoring
)


# Reads that are themselves access-management data, or personal data in bulk. Every GET used to
# map to READ, so a VIEWER could list every member's email, the API keys (masked, but with the key
# ids that are now OAuth client_ids) and the SSO configuration -- while the members handler's own
# docstring said "requires manage_access". A data-subject export is everything held about one
# person, so it belongs here from the start.
_SENSITIVE_READ_PREFIXES = (
    "/api/v1/access/members",
    "/api/v1/access/keys",
    "/api/v1/access/sso",
    "/api/v1/data-subjects",
)


def required_capability(method: str, path: str) -> str:
    """Map a request to the single capability it requires."""
    if method in ("GET", "HEAD", "OPTIONS"):
        if method != "OPTIONS" and path.startswith(_SENSITIVE_READ_PREFIXES):
            return MANAGE_ACCESS
        return READ
    # `/policies/{id}/promote` is a deploy action even though it sits under /policies.
    if path.startswith("/api/v1/policies") and path.endswith("/promote"):
        return DEPLOY
    # Model MANAGEMENT (upload/update/delete) unpickles an author-supplied blob, which is code
    # execution (T2.1) -- restrict it to MANAGE_ACCESS so a policy_maker cannot reach it. Running
    # a STORED, admin-vetted model (predict/test) is an evaluation, so it stays at DECIDE.
    if path.startswith("/api/v1/models"):
        if path.endswith("/predict") or path.endswith("/test"):
            return DECIDE
        return MANAGE_ACCESS
    if path.startswith(_IDENTITY_PREFIXES):
        return MANAGE_ACCESS
    for prefix, capability in _WRITE_PREFIX_CAPABILITY:
        if path.startswith(prefix):
            return capability
    if path.startswith(_DECIDE_PREFIXES):
        return DECIDE
    if path.startswith(_AUTHOR_PREFIXES):
        return AUTHOR
    # Unknown write: FAIL CLOSED. This used to return AUTHOR, which meant every new mutating
    # prefix was auto-granted to policy_maker the moment it was added -- the map was
    # deny-by-omission-of-nothing. /scim/v2 is the proof it actually happened: member
    # provisioning, including deactivating the owner, was reachable by an author.
    #
    # MANAGE_ACCESS is the most restrictive capability, so an unmapped route is now admin-only.
    # A new route being too restrictive surfaces immediately as a 403 in development; a new
    # route being too permissive surfaces as an incident. Add the prefix to a list above.
    return MANAGE_ACCESS


def capabilities_for(role: str) -> Set[str]:
    return set(ROLE_CAPABILITIES[_resolve_role(role)])


def is_allowed(role: str, method: str, path: str) -> bool:
    return required_capability(method, path) in capabilities_for(role)


def normalize_role(role: str) -> str:
    return _resolve_role(role)
