"""`COMPUTE_ONLY_USED_VARS` is an audit-surface decision, so it belongs to the tenant.

The flag restricts a decision to the variables the policy can transitively reach. It is worth
the entire measured +82% on the decision path, and it is off by default NOT for performance
reasons: with it on, a decision's `computed_variables` records what the policy read rather than
the tenant's whole catalog. A regulated lender reconstructing declines from the recorded set
wants everything; a high-volume tenant scoring thin-file applicants wants the latency. Those are
different answers for different tenants of the same install, and a process-wide environment
variable cannot express that.

So `engine_config.compute_only_used_vars` (PUT /api/v1/settings) wins, `COMPUTE_ONLY_USED_VARS`
is the install-wide default, and off is the default of that.

The second thing here is smaller and worse. `variable_reachability.compute_only_used_vars()`
carries a docstring saying it is the ONE reader, "because both decision paths ask this question
and two different answers is its own divergence" -- and `PolicyExecutor.execute` asked
`os.getenv` directly, bypassing it. The guard existed; one of its two callers did not use it.
`test_both_paths_ask_the_same_function` is a source check because no behavioural test could
catch it: with only the env var in play both readings agree, and they diverge only once a
per-tenant value exists.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import variable_reachability  # noqa: E402

ON = {"COMPUTE_ONLY_USED_VARS": "1"}
OFF = {"COMPUTE_ONLY_USED_VARS": "0"}


class TenantSettingTests(unittest.TestCase):
    def _flag(self, env, engine_config=None):
        with mock.patch.dict(os.environ, env, clear=False):
            return variable_reachability.compute_only_used_vars(engine_config)

    def test_the_environment_is_the_default(self):
        self.assertTrue(self._flag(ON))
        self.assertFalse(self._flag(OFF))

    def test_off_is_the_default_of_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(variable_reachability.compute_only_used_vars())

    def test_a_tenant_can_turn_it_on_where_the_install_has_it_off(self):
        self.assertTrue(self._flag(OFF, {"compute_only_used_vars": True}))
        self.assertTrue(self._flag(OFF, {"compute_only_used_vars": "1"}))
        self.assertTrue(self._flag(OFF, {"compute_only_used_vars": 1}))

    def test_a_tenant_can_turn_it_OFF_where_the_install_has_it_on(self):
        """The direction that matters for compliance: an install may default it on for
        throughput, and one regulated tenant must be able to keep the full recorded catalog."""
        self.assertFalse(self._flag(ON, {"compute_only_used_vars": False}))
        self.assertFalse(self._flag(ON, {"compute_only_used_vars": "0"}))

    def test_an_absent_tenant_key_falls_through_to_the_environment(self):
        self.assertTrue(self._flag(ON, {"timeout_ms": 2000}))
        self.assertFalse(self._flag(OFF, {"timeout_ms": 2000}))
        self.assertTrue(self._flag(ON, {}))

    def test_a_null_tenant_value_falls_through_rather_than_meaning_off(self):
        """`{"compute_only_used_vars": null}` is what a UI sends for "not set". Reading it as
        False would silently override an install that deliberately turned the flag on."""
        self.assertTrue(self._flag(ON, {"compute_only_used_vars": None}))

    def test_a_junk_tenant_value_does_not_turn_it_on(self):
        """Fail to the environment default, never to ON. This flag changes which variables get
        computed; a typo must not shrink someone's audit record."""
        for junk in ("yes", "true", "TRUE", "on", 2, [], {}, "maybe"):
            with self.subTest(value=junk):
                self.assertFalse(self._flag(OFF, {"compute_only_used_vars": junk}))

    def test_the_env_var_is_read_strictly(self):
        """Siblings with FAST_DECIDE, which routers/runtime.py reads as `== "1"`. A looser read
        would turn the flag on for a deployment that wrote something env_bool calls truthy."""
        for value in ("true", "yes", "on", "TRUE", "2", ""):
            with self.subTest(value=value):
                self.assertFalse(self._flag({"COMPUTE_ONLY_USED_VARS": value}))


class BothPathsAskTheSameFunctionTests(unittest.TestCase):
    def test_neither_decision_path_reads_the_environment_directly(self):
        """The bypass. `variable_reachability.compute_only_used_vars` says it is the one reader
        so the two paths cannot answer differently -- and `PolicyExecutor.execute` called
        `os.getenv("COMPUTE_ONLY_USED_VARS")` itself. Source-level because no behavioural test
        could see it: with only the env var in play both readings agree."""
        for module in ("executor.py", "fast_decide.py"):
            source = (APP_ROOT / "app" / module).read_text(encoding="utf-8")
            code = "\n".join(line for line in source.splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertNotIn('getenv("COMPUTE_ONLY_USED_VARS"', code,
                             f"{module} reads the flag directly instead of through "
                             f"variable_reachability.compute_only_used_vars()")

    def test_both_paths_pass_the_tenant_config(self):
        """Asking the shared function is not enough if one path asks it with no tenant: it
        would answer from the environment while the other honoured the tenant's setting."""
        executor = (APP_ROOT / "app" / "executor.py").read_text(encoding="utf-8")
        fast = (APP_ROOT / "app" / "fast_decide.py").read_text(encoding="utf-8")
        self.assertIn("compute_only_used_vars(engine_config)", executor)
        self.assertIn('compute_only_used_vars(bundle.get("engine_config"))', fast)


if __name__ == "__main__":
    unittest.main()
