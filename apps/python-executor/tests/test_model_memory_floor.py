"""The model worker's memory cap must not be below what the shipped ML stack needs to import.

`MODEL_MEMORY_MB` defaulted to 512. Bisected inside the shipped api image (python 3.11, aarch64
linux, scipy-openblas):

    256 MB -> fails: libscipy_openblas-*.so: failed to map segment from shared object
    512 MB -> fails: sklearn/metrics/_pairwise_distances_reduction/*.so
    768 MB -> imports OK

So the shipped default sat *below the floor for the stack shipped alongside it*. Uploading a
scikit-learn model returned `422 Invalid model: failed to map segment from shared object`, and
every `model` policy step failed -- in compose and in the chart alike, because `MODEL_MEMORY_MB`
appears in no deployment artifact and every deployment therefore took the in-code default.

**Why ~5,000 passing tests could not see it.** They all run on darwin, where
`setrlimit(RLIMIT_AS)` is REFUSED outright ("current limit exceeds maximum limit").
`_model_worker_init` swallowed that `OSError` with a bare `pass`, so the cap was simply never
applied and sklearn imported fine. The mechanism under test does nothing on the only platform
that tests it -- the same shape as the sandbox's documented darwin caveat, but here it silently
broke a shipped feature rather than only weakening a defence.

Two assertions follow, and the platform split is deliberate:

* Everywhere: the configured value must exceed the measured floor. This is the one that would
  have caught it, and it runs on the developer machine that cannot reproduce the failure.
* Linux only: actually import the ML stack under the configured limit, which is the real thing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import model_executor  # noqa: E402


class ModelMemoryFloorTest(unittest.TestCase):
    def test_configured_cap_exceeds_the_measured_import_floor(self) -> None:
        self.assertGreater(
            model_executor._MODEL_MEMORY_MB, model_executor.MODEL_MEMORY_FLOOR_MB,
            "MODEL_MEMORY_MB=%d is at or below the measured %d MB floor for importing "
            "numpy+scipy+scikit-learn in a fresh worker. Below it, every model upload returns "
            "422 and every `model` policy step fails -- on Linux. This assertion exists because "
            "darwin refuses setrlimit(RLIMIT_AS) entirely, so the local test suite cannot "
            "reproduce the failure it is guarding against."
            % (model_executor._MODEL_MEMORY_MB, model_executor.MODEL_MEMORY_FLOOR_MB),
        )

    def test_floor_has_real_headroom(self) -> None:
        """The floor is the import cost alone. A model still needs room to run."""
        self.assertGreaterEqual(
            model_executor._MODEL_MEMORY_MB, model_executor.MODEL_MEMORY_FLOOR_MB * 2,
            "the cap should leave the model's own working set at least as much room as the "
            "interpreter needs to import the stack",
        )

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "RLIMIT_AS is unsettable on darwin, so the cap cannot be exercised here")
    def test_ml_stack_imports_under_the_configured_cap(self) -> None:
        """The real assertion, on the platform that actually enforces the limit."""
        probe = textwrap.dedent(
            """
            import resource, sys
            b = int(sys.argv[1]) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (b, b))
            import sklearn.linear_model  # noqa: F401
            import numpy  # noqa: F401
            print("ok")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, str(model_executor._MODEL_MEMORY_MB)],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(
            proc.returncode, 0,
            "the ML stack cannot import under MODEL_MEMORY_MB=%d: %s"
            % (model_executor._MODEL_MEMORY_MB, (proc.stderr or "")[-400:]),
        )


class LimitFailureIsVisibleTest(unittest.TestCase):
    def test_worker_init_reports_when_the_cap_is_not_applied(self) -> None:
        """A bare `except: pass` is how a missing memory ceiling stays secret for a month.

        On darwin this path is taken on every single worker start and printed nothing, so
        'this worker has no memory ceiling' was true, permanent, and unobservable.
        """
        import inspect

        src = inspect.getsource(model_executor._model_worker_init)
        self.assertNotRegex(
            src, r"except \([^)]*\):\s*\n\s*pass\b",
            "_model_worker_init silently swallows the setrlimit failure again",
        )
        self.assertIn("NO memory ceiling", src,
                      "the failure branch must say that isolation is absent, not pass quietly")


if __name__ == "__main__":
    unittest.main()
