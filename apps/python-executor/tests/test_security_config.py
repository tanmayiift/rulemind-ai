"""Fail-closed production secret verification.

In production the app must refuse to boot with default/unset critical secrets (config key,
admin JWT secret, admin password, dev API key) — otherwise it ships with credentials anyone
can read in this repo. Locally / under pytest the check is a no-op so dev stays frictionless.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.security_config import (  # noqa: E402
    InsecureProductionConfigError,
    default_secret_violations,
    verify_production_secrets,
)

_STRONG = {
    "RULEMIND_CONFIG_KEY": "a" * 64,
    "RULEMIND_ADMIN_JWT_SECRET": "b" * 64,
    "RULEMIND_ADMIN_PASSWORD": "s3cure-Passw0rd!",
    "RULEMIND_DEV_API_KEY": "rm_live_" + "c" * 32,
}


class SecurityConfigTests(unittest.TestCase):
    def _prod(self, **overrides):
        env = {"NODE_ENV": "production"}
        env.update(overrides)
        return env

    def test_all_defaults_reported_as_violations(self):
        violations = default_secret_violations(self._prod())
        self.assertEqual(len(violations), 4)

    def test_strong_secrets_have_no_violations(self):
        self.assertEqual(default_secret_violations(self._prod(**_STRONG)), [])

    def test_explicit_default_value_flagged(self):
        env = self._prod(**_STRONG)
        env["RULEMIND_CONFIG_KEY"] = "rulemind-dev-master-key"  # the built-in default
        violations = default_secret_violations(env)
        self.assertEqual(len(violations), 1)
        self.assertIn("RULEMIND_CONFIG_KEY", violations[0])

    def test_verify_raises_in_production_with_defaults(self):
        with self.assertRaises(InsecureProductionConfigError):
            verify_production_secrets(self._prod())

    def test_verify_passes_in_production_with_strong_secrets(self):
        verify_production_secrets(self._prod(**_STRONG))  # no raise

    def test_local_dev_is_a_noop(self):
        # NODE_ENV=development (or unset) -> never enforced, even with all defaults.
        verify_production_secrets({"NODE_ENV": "development"})
        verify_production_secrets({})

    def test_pytest_env_is_a_noop(self):
        verify_production_secrets({"NODE_ENV": "production", "PYTEST_CURRENT_TEST": "x"})

    def test_per_secret_waiver(self):
        env = self._prod(**_STRONG)
        env["RULEMIND_ADMIN_PASSWORD"] = "rulemind-admin"  # default
        env["RULEMIND_ALLOW_DEFAULT_SECRETS"] = "RULEMIND_ADMIN_PASSWORD"
        verify_production_secrets(env)  # waived -> no raise

    def test_global_skip_escape_hatch(self):
        env = self._prod()  # all defaults
        env["RULEMIND_SKIP_SECRET_CHECK"] = "1"
        verify_production_secrets(env)  # skipped -> no raise


if __name__ == "__main__":
    unittest.main()
