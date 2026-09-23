"""Fail-closed production secret verification.

Every critical secret has a built-in dev default so the app runs out of the box locally. In
production those defaults are dangerous: the config-encryption key protects config-at-rest, the
JWT secret signs admin sessions, and the seeded admin password / dev API key are publicly known.
A prod deploy that forgets to override them would run with credentials anyone can read in this
repo.

`verify_production_secrets()` is called at server startup. Outside local dev, if any required
secret is unset or still its default, it raises and the app refuses to boot — surfacing the
misconfiguration immediately instead of shipping known-insecure credentials. It can be waived
per-secret with RULEMIND_ALLOW_DEFAULT_SECRETS (comma-separated var names) for constrained
environments, or globally with RULEMIND_SKIP_SECRET_CHECK=1 (discouraged; logged intent).
"""
from __future__ import annotations

import logging
import os
from typing import List, Mapping, Optional

# (env var, built-in default, human description). Keep the defaults in sync with the code that
# reads them (storage.py / auth.py).
PRODUCTION_SECRET_CHECKS = [
    ("RULEMIND_CONFIG_KEY", "rulemind-dev-master-key", "config-at-rest encryption key"),
    ("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret", "admin session JWT signing secret"),
    ("RULEMIND_ADMIN_PASSWORD", "rulemind-admin", "default admin password"),
    ("RULEMIND_DEV_API_KEY", "rm_live_devlocaltenantkey000000000000", "seeded dev API key"),
    # Not a "default secret" like the four above -- there IS no default. Unset,
    # compiler.py:_load_signing_keypair generates a fresh RSA-2048 key PER PROCESS. That is
    # exactly right on a laptop and broken on any real deployment:
    #
    #   * at replicaCount.api > 1, an SDK that fetches /sdk/v1/signing-key from one pod and
    #     /sdk/v1/bundle from another fails signature verification -- and the SDK is built to
    #     REFUSE a bundle it cannot verify, so on-device decisioning simply stops;
    #   * every restart invalidates a key a device has pinned.
    #
    # The code already knew: signing_key_is_ephemeral() exists, and surfaced the answer only as
    # a JSON field on the signing-key response (sdk.py:94). No startup warning, no readiness
    # failure -- so the deployment that breaks is the one that never looks at that field.
    # `None` as the default is a sentinel that no real value can equal; only unset/blank trips.
    ("RULEMIND_SIGNING_PRIVATE_KEY", None,
     "SDK bundle signing key -- unset generates a FRESH key per process, so replicas disagree"),
]


LOGGER = logging.getLogger(__name__)


def _is_local_dev(env: Mapping[str, str]) -> bool:
    """Whether to skip the fail-closed default-secret check.

    RULEMIND_ENV is checked FIRST and is the signal a deployment should set, because
    NODE_ENV defaulting to "development" means a deploy that simply never sets it skips the
    entire check and boots with the four known-public defaults listed in
    PRODUCTION_SECRET_CHECKS. That silent path is the real hazard here.

    The unset case still counts as local dev -- removing it would break `pnpm dev` and every
    shell that does not export NODE_ENV, contradicting this module's own goal of running out
    of the box -- but it now WARNS, so the gap is visible instead of silent.
    """
    if env.get("PYTEST_CURRENT_TEST") is not None:
        return True
    rulemind_env = (env.get("RULEMIND_ENV") or "").strip().lower()
    if rulemind_env:
        return rulemind_env not in ("production", "prod", "staging")
    node_env = (env.get("NODE_ENV") or "").strip().lower()
    if not node_env:
        LOGGER.warning(
            "Neither RULEMIND_ENV nor NODE_ENV is set, so the default-secret check is being "
            "SKIPPED and built-in development secrets may be in use. Set RULEMIND_ENV=production "
            "on any real deployment."
        )
        return True
    return node_env == "development"


def default_secret_violations(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Return a human-readable list of secrets that are unset or still their built-in default.
    Empty list == all required secrets have been overridden."""
    env = env if env is not None else os.environ
    waived = {
        item.strip()
        for item in (env.get("RULEMIND_ALLOW_DEFAULT_SECRETS", "") or "").split(",")
        if item.strip()
    }
    violations: List[str] = []
    for var, default, description in PRODUCTION_SECRET_CHECKS:
        if var in waived:
            continue
        value = env.get(var)
        if value is None or value.strip() == "" or (default is not None and value == default):
            state = "is unset" if not (value or "").strip() else "is still the built-in default"
            violations.append("{0} ({1}) {2}".format(var, description, state))
    return violations


class InsecureProductionConfigError(RuntimeError):
    """Raised when a production deploy would run with default/known secrets."""


def verify_production_secrets(env: Optional[Mapping[str, str]] = None) -> None:
    """Fail closed: in production, refuse to start with any default/unset critical secret.

    No-op in local dev / tests, or when RULEMIND_SKIP_SECRET_CHECK=1 (escape hatch).
    """
    env = env if env is not None else os.environ
    if _is_local_dev(env):
        return
    if (env.get("RULEMIND_SKIP_SECRET_CHECK", "") or "").strip() in ("1", "true", "yes"):
        return
    violations = default_secret_violations(env)
    if violations:
        raise InsecureProductionConfigError(
            "Refusing to start in production with insecure secrets:\n  - "
            + "\n  - ".join(violations)
            + "\nSet each to a strong unique value (e.g. `openssl rand -hex 32`). "
            + "To waive a specific one, add it to RULEMIND_ALLOW_DEFAULT_SECRETS."
        )
