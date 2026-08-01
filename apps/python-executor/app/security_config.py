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

import os
from typing import List, Mapping, Optional

# (env var, built-in default, human description). Keep the defaults in sync with the code that
# reads them (storage.py / auth.py).
PRODUCTION_SECRET_CHECKS = [
    ("RULEMIND_CONFIG_KEY", "rulemind-dev-master-key", "config-at-rest encryption key"),
    ("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret", "admin session JWT signing secret"),
    ("RULEMIND_ADMIN_PASSWORD", "rulemind-admin", "default admin password"),
    ("RULEMIND_DEV_API_KEY", "rm_live_devlocaltenantkey000000000000", "seeded dev API key"),
]


def _is_local_dev(env: Mapping[str, str]) -> bool:
    return env.get("NODE_ENV", "development") == "development" or env.get("PYTEST_CURRENT_TEST") is not None


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
        if value is None or value == "" or value == default:
            state = "is unset" if not value else "is still the built-in default"
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
