"""`/metrics` must aggregate across uvicorn workers, in every deployment -- not just in Helm.

The Dockerfile runs `uvicorn --workers 2`. `prometheus_client` keeps counters **per process**,
so without `PROMETHEUS_MULTIPROC_DIR` a scrape reports whichever worker happened to answer it.
The symptom is not a missing metric, it is a *wrong* one: `rulemind_decisions_total` jumps
around and goes backwards between scrapes, and `rate()` over it -- the one query an operator
actually writes -- is meaningless.

Measured on the running compose stack before this file existed:

    sum(rulemind_decisions_total), 10-minute window
      min=175,073  max=280,562  spread=105,489
      times the counter went BACKWARDS: 2     <- impossible in a single process

`app/metrics_registry.py` predicted this in its own module docstring -- *"That variable
appeared nowhere in this repo, so the mode was never on"* -- and the fix was then applied to
`helm/rulemind/templates/configmap.yaml` and **not** to `docker-compose.yml`. So the one
deployment every developer and every evaluator runs first was the broken one, for as long as
the chart was right. Five of the eight rules in `observability/alerts.yml` read these counters.

The assertion that would have caught it is a *relationship*, not a value: if the image starts
more than one worker, then every deployment artifact that runs that image must configure
multiprocess mode. Checking only that the variable is set somewhere would have passed
throughout, because it was set in the chart the whole time.

`test_deployment_with_many_workers_configures_multiproc` is therefore the load-bearing test
here; the rest guard the code path it depends on.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest

import yaml

def _repo_root() -> str:
    """Walk up to the directory holding docker-compose.yml, so a move does not break this."""
    here = os.path.abspath(os.path.dirname(__file__))
    while here != os.path.dirname(here):
        if os.path.exists(os.path.join(here, "docker-compose.yml")):
            return here
        here = os.path.dirname(here)
    raise RuntimeError("could not locate the repository root from %s" % __file__)


REPO = _repo_root()
COMPOSE = os.path.join(REPO, "docker-compose.yml")
DOCKERFILE = os.path.join(REPO, "apps", "python-executor", "Dockerfile")
CONFIGMAP = os.path.join(REPO, "helm", "rulemind", "templates", "configmap.yaml")

VAR = "PROMETHEUS_MULTIPROC_DIR"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _dockerfile_worker_count() -> int:
    """Workers the shipped CMD starts. 1 when `--workers` is absent (uvicorn's default)."""
    m = re.search(r'"--workers",\s*"(\d+)"', _read(DOCKERFILE))
    return int(m.group(1)) if m else 1


class DeploymentArtifactsTest(unittest.TestCase):
    """The config relationship. These read files, not a running stack, so they run everywhere."""

    def test_deployment_with_many_workers_configures_multiproc(self) -> None:
        workers = _dockerfile_worker_count()
        if workers <= 1:
            self.skipTest("Dockerfile starts a single worker; multiprocess mode is not required")

        compose = yaml.safe_load(_read(COMPOSE))
        env = compose["services"]["api"]["environment"]
        self.assertIn(
            VAR, env,
            "apps/python-executor/Dockerfile starts uvicorn with --workers %d, so the compose "
            "`api` service must set %s. Without it /metrics reports one worker's counters and "
            "rulemind_decisions_total goes backwards between scrapes." % (workers, VAR),
        )
        value = str(env[VAR])
        self.assertTrue(value.strip(), "%s is present but empty -- env.env_opt() treats a blank "
                                       "value as unset, so this is the same bug." % VAR)
        # A ${VAR:-default} interpolation is fine; a ${VAR} with no default is not, because an
        # unset host variable silently renders empty and multiprocess mode goes back off.
        if value.startswith("${"):
            self.assertIn(":-", value,
                          "%s uses shell interpolation with no default (%s): an unset host "
                          "variable renders empty and turns multiprocess mode back off."
                          % (VAR, value))
            self.assertTrue(value.split(":-", 1)[1].rstrip("}").strip(),
                            "%s interpolation has an empty default" % VAR)

    def test_helm_configmap_still_sets_it(self) -> None:
        """The chart was the one path that had this right. Keep it that way."""
        self.assertIn(VAR, _read(CONFIGMAP),
                      "helm configmap no longer sets %s" % VAR)

    def test_worker_service_is_not_expected_to_set_it(self) -> None:
        """Documents a deliberate asymmetry so nobody 'fixes' it later.

        `worker` runs `python3 -m app.worker`, which serves no /metrics endpoint. Setting the
        variable there would write mmap files that nothing ever reads.
        """
        compose = yaml.safe_load(_read(COMPOSE))
        self.assertEqual(
            compose["services"]["worker"]["command"],
            ["python3", "-m", "app.worker"],
            "worker no longer runs app.worker -- if it now serves /metrics it needs %s too" % VAR,
        )


class BuildRegistryTest(unittest.TestCase):
    """The code path the config above depends on."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="rm-prom-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = {k: os.environ.get(k) for k in (VAR, "prometheus_multiproc_dir")}

    def tearDown(self) -> None:
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_unset_returns_the_default_registry(self) -> None:
        from app import metrics_registry as mr
        from prometheus_client import REGISTRY

        os.environ.pop(VAR, None)
        os.environ.pop("prometheus_multiproc_dir", None)
        self.assertFalse(mr.is_multiprocess())
        self.assertIs(mr.build_registry(), REGISTRY)

    def test_set_returns_a_fresh_multiprocess_registry(self) -> None:
        from app import metrics_registry as mr
        from prometheus_client import REGISTRY

        os.environ[VAR] = self.tmp
        self.assertTrue(mr.is_multiprocess())
        registry = mr.build_registry()
        self.assertIsNot(registry, REGISTRY,
                         "multiprocess mode must render a fresh registry carrying a "
                         "MultiProcessCollector, not the per-process default one")
        names = {type(c).__name__ for c in registry._collector_to_names}
        self.assertIn("MultiProcessCollector", names)

    def test_blank_is_treated_as_unset(self) -> None:
        """A Helm chart renders an unset override as an empty string, not as an absent key."""
        from app import metrics_registry as mr

        os.environ[VAR] = ""
        os.environ.pop("prometheus_multiproc_dir", None)
        self.assertFalse(mr.is_multiprocess())
        self.assertIsNone(mr.prepare_multiproc_dir())

    def test_lowercase_spelling_is_accepted(self) -> None:
        """Some charts still set the older lowercase name; a casing difference must not
        silently downgrade a working deployment to per-process metrics."""
        from app import metrics_registry as mr

        os.environ.pop(VAR, None)
        os.environ["prometheus_multiproc_dir"] = self.tmp
        self.assertTrue(mr.is_multiprocess())


if __name__ == "__main__":
    unittest.main()
