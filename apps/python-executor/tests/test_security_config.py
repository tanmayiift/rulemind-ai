"""Fail-closed production secret verification.

In production the app must refuse to boot with default/unset critical secrets (config key,
admin JWT secret, admin password, dev API key) — otherwise it ships with credentials anyone
can read in this repo. Locally / under pytest the check is a no-op so dev stays frictionless.
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import security_config  # noqa: E402
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
    # Not a "default secret" -- there is no default. Unset, compiler.py generates a fresh
    # RSA key PER PROCESS, so replicas sign bundles an SDK fetching from a different pod
    # cannot verify, and a restart invalidates a pinned key. The value is not parsed here;
    # the check is only "was one supplied".
    "RULEMIND_SIGNING_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-parsed-here\n",
}


class SecurityConfigTests(unittest.TestCase):
    def _prod(self, **overrides):
        env = {"NODE_ENV": "production"}
        env.update(overrides)
        return env

    def test_all_defaults_reported_as_violations(self):
        violations = default_secret_violations(self._prod())
        self.assertEqual(len(violations), len(security_config.PRODUCTION_SECRET_CHECKS))

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



class LocalDevGateTest(unittest.TestCase):
    """The gate that decides whether the fail-closed secret check runs at all.

    A deployment that never sets NODE_ENV used to skip the whole check silently and boot with
    the built-in development secrets. That path still exists (removing it breaks zero-config
    local dev, which is this module's stated goal) but it now warns, and RULEMIND_ENV gives a
    deployment an explicit way to say production.
    """

    def test_rulemind_env_production_is_not_local_dev(self):
        self.assertFalse(security_config._is_local_dev({"RULEMIND_ENV": "production"}))
        self.assertFalse(security_config._is_local_dev({"RULEMIND_ENV": "staging"}))

    def test_rulemind_env_beats_node_env(self):
        """A deploy setting RULEMIND_ENV=production must be believed even if some base image
        left NODE_ENV=development lying around."""
        self.assertFalse(security_config._is_local_dev(
            {"RULEMIND_ENV": "production", "NODE_ENV": "development"}))

    def test_unset_environment_still_local_but_warns(self):
        # Patches the module logger rather than using assertLogs: assertLogs depends on
        # global logging configuration, so this passed alone and failed inside the full
        # suite once another module had touched the root logger. The claim under test is
        # "the unset path warns", and that is exactly what this asserts.
        with mock.patch.object(security_config.LOGGER, "warning") as warned:
            self.assertTrue(security_config._is_local_dev({}))
        warned.assert_called_once()
        self.assertIn("RULEMIND_ENV=production", warned.call_args[0][0])

    def test_an_explicit_environment_does_not_warn(self):
        """The warning must be specific to the silent-skip path, or it becomes noise that
        operators filter out -- which would defeat the point of adding it."""
        for env in ({"NODE_ENV": "development"}, {"RULEMIND_ENV": "production"}):
            with mock.patch.object(security_config.LOGGER, "warning") as warned:
                security_config._is_local_dev(env)
            warned.assert_not_called()

    def test_node_env_production_is_not_local_dev(self):
        self.assertFalse(security_config._is_local_dev({"NODE_ENV": "production"}))

if __name__ == "__main__":
    unittest.main()


class EphemeralSigningKeyTests(unittest.TestCase):
    """The signing key was deliverable by NOTHING -- no chart, no manifest, no compose file.

    Unset, `compiler._load_signing_keypair` generates a fresh RSA-2048 key per process. On a
    laptop that is right. On any real deployment it means:

      * at `replicaCount.api: 3`, an SDK that fetches /sdk/v1/signing-key from one pod and
        /sdk/v1/bundle from another fails signature verification -- and the SDK is built to
        REFUSE a bundle it cannot verify, so on-device decisioning stops;
      * every restart invalidates a key a device has pinned.

    The code already knew: `compiler.signing_key_is_ephemeral()` exists. It surfaced the answer
    only as a JSON field on the signing-key response, so the deployment that breaks is exactly
    the one that never reads that field. Now it fails startup, in production, like the other
    four -- and is waivable the same way for a single-replica install that genuinely does not
    care.
    """

    def _prod(self, **overrides):
        env = {"RULEMIND_ENV": "production"}
        env.update(_STRONG)
        env.update(overrides)
        return env

    def test_production_refuses_to_start_without_a_signing_key(self):
        env = self._prod()
        env.pop("RULEMIND_SIGNING_PRIVATE_KEY")
        with self.assertRaises(InsecureProductionConfigError) as caught:
            verify_production_secrets(env)
        self.assertIn("RULEMIND_SIGNING_PRIVATE_KEY", str(caught.exception))

    def test_a_blank_key_is_treated_as_unset(self):
        """`RULEMIND_SIGNING_PRIVATE_KEY=` is a set-but-empty env var, which os.getenv returns
        as "" -- and compiler.py's `if pem:` then falls through to generating one. A check that
        only tested `is None` would pass the exact configuration that breaks."""
        with self.assertRaises(InsecureProductionConfigError):
            verify_production_secrets(self._prod(RULEMIND_SIGNING_PRIVATE_KEY="   "))

    def test_a_supplied_key_passes(self):
        verify_production_secrets(self._prod())  # no raise

    def test_local_dev_still_boots_without_one(self):
        """An ephemeral key is CORRECT for `docker compose up`. Failing closed there would
        make the product un-runnable out of the box, which this module exists not to do."""
        env = {"RULEMIND_ENV": "development"}
        verify_production_secrets(env)  # no raise

    def test_it_can_be_waived_for_a_deployment_that_means_it(self):
        env = self._prod(RULEMIND_ALLOW_DEFAULT_SECRETS="RULEMIND_SIGNING_PRIVATE_KEY")
        env.pop("RULEMIND_SIGNING_PRIVATE_KEY")
        verify_production_secrets(env)  # no raise

    def test_the_check_matches_what_the_compiler_actually_does(self):
        """Pins the two to the same question. If compiler.signing_key_is_ephemeral() ever
        disagrees with this check, one of them is wrong about whether bundles are signed with
        a stable key -- and the startup gate is the one nobody would notice drifting."""
        from app import compiler

        with mock.patch.dict("os.environ", {"RULEMIND_SIGNING_PRIVATE_KEY": ""}, clear=False):
            self.assertTrue(compiler.signing_key_is_ephemeral())
            env = self._prod(RULEMIND_SIGNING_PRIVATE_KEY="")
            with self.assertRaises(InsecureProductionConfigError):
                verify_production_secrets(env)

        with mock.patch.dict("os.environ",
                             {"RULEMIND_SIGNING_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----"},
                             clear=False):
            self.assertFalse(compiler.signing_key_is_ephemeral())
