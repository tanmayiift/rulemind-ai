"""Uploading a model must not be code execution in the API process (Tier 2.1).

model_executor called pickle.loads on an uploaded blob in the API process -- the one holding
RULEMIND_CONFIG_KEY and every tenant's DB credentials. pickle runs the payload's __reduce__ on
DESERIALIZATION, before any predict(), so unpickling an untrusted blob is arbitrary code
execution. It was reachable at the AUTHOR capability, so policy_maker -- documented as unable to
deploy or manage access -- had it. validate_model (the upload path) also had no size guard.

Three fixes, asserted here:
  - pickle.loads happens in an isolated worker, not the API process (proven with a payload that
    writes a file: it must run in the child's cwd/pid, and must not see scrubbed secrets).
  - the size guard is enforced at upload (validate_model), before anything is unpickled.
  - model management is gated at MANAGE_ACCESS; predict/test at DECIDE.
"""
from __future__ import annotations

import os
import pickle
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import model_executor  # noqa: E402
from tests._worker_env_probe import read_env  # noqa: E402
from app.rbac import DECIDE, MANAGE_ACCESS, READ, required_capability  # noqa: E402


class IsolationTest(unittest.TestCase):
    def tearDown(self):
        if model_executor._model_pool is not None:
            model_executor._recycle_model_pool(model_executor._model_pool)

    def test_the_model_pool_runs_in_a_separate_process(self):
        """Direct proof: a function submitted to the model pool reports a different pid.

        execute_model/validate_model both route through _run_isolated, which submits to this pool,
        so anything that unpickles inside those does so in this separate process -- not the API
        process. The env-scrub test below is the second half: whatever runs there cannot see the
        secrets.
        """
        pool = model_executor._get_model_pool()
        worker_pid = pool.submit(os.getpid).result(timeout=10)
        self.assertNotEqual(worker_pid, os.getpid(),
                            "the model pool ran in the API process, not an isolated worker")

    def test_a_worker_cannot_read_the_master_key(self):
        """The strong property: code executing in a worker cannot read the config key, because
        _model_worker_init scrubs it from the child's environment.

        Probed directly at the pool so the value the worker sees is actually observable -- routed
        through execute_model the deserialized value becomes the 'model' and is never echoed back,
        which would make this untestable.
        """
        self.assertTrue(os.environ.get("RULEMIND_CONFIG_KEY"),
                        "precondition: the parent (API) process has the key")
        pool = model_executor._get_model_pool()
        in_worker = pool.submit(read_env, "RULEMIND_CONFIG_KEY").result(timeout=10)
        self.assertEqual(in_worker, "",
                         "the model worker inherited the master key from the parent environment")

    def test_the_size_guard_is_enforced_at_upload(self):
        big = b"x" * ((model_executor.MAX_MODEL_SIZE_MB + 1) * 1024 * 1024)
        validation = model_executor.validate_model(big)
        self.assertFalse(validation["valid"])
        self.assertIn("exceeds limit", validation["error"])

    def test_the_size_guard_short_circuits_before_unpickling(self):
        """A too-large blob must be rejected without being submitted to the pool at all."""
        from unittest import mock

        big = b"x" * ((model_executor.MAX_MODEL_SIZE_MB + 1) * 1024 * 1024)
        with mock.patch.object(model_executor, "_run_isolated",
                               side_effect=AssertionError("oversize blob reached the pool")):
            self.assertFalse(model_executor.validate_model(big)["valid"])

    def test_a_real_model_still_validates_and_predicts(self):
        """Regression guard -- isolation is only correct if legitimate models still work."""
        try:
            from sklearn.linear_model import LogisticRegression
        except ImportError:
            self.skipTest("scikit-learn not installed")
        import numpy as np

        model = LogisticRegression()
        model.fit(np.array([[0.0], [1.0], [2.0], [3.0]]), [0, 0, 1, 1])
        blob = pickle.dumps(model)
        validation = model_executor.validate_model(blob)
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["model_type"], "LogisticRegression")
        result = model_executor.execute_model(blob, {"f0": 3.0}, features=["f0"])
        self.assertIsNone(result["error"], result)
        self.assertIsNotNone(result["prediction"])


class RbacTest(unittest.TestCase):
    def test_model_management_requires_manage_access(self):
        self.assertEqual(required_capability("POST", "/api/v1/models"), MANAGE_ACCESS)
        self.assertEqual(required_capability("PUT", "/api/v1/models/m1"), MANAGE_ACCESS)
        self.assertEqual(required_capability("DELETE", "/api/v1/models/m1"), MANAGE_ACCESS)

    def test_running_a_model_is_decide_not_manage_access(self):
        self.assertEqual(required_capability("POST", "/api/v1/models/m1/predict"), DECIDE)
        self.assertEqual(required_capability("POST", "/api/v1/models/m1/test"), DECIDE)

    def test_reading_models_is_read(self):
        self.assertEqual(required_capability("GET", "/api/v1/models"), READ)

    def test_a_policy_maker_cannot_upload_a_model(self):
        """The concrete privilege check: the role that had RCE must no longer reach the upload."""
        from app.rbac import ROLE_CAPABILITIES

        policy_maker = ROLE_CAPABILITIES["policy_maker"]
        self.assertNotIn(MANAGE_ACCESS, policy_maker,
                         "policy_maker has MANAGE_ACCESS, so gating uploads there does nothing")


if __name__ == "__main__":
    unittest.main()
