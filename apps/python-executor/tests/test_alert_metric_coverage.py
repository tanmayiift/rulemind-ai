"""The alert rules must reference metrics that exist -- and the checker must parse PromQL right.

`scripts/check_alert_metrics.py` exists because `RuleMindPagingIsFailing`, the one rule whose
entire job is to catch "alerts are firing and NOT arriving", queried
`alertmanager_notifications_failed_total` while `observability/prometheus.yml` had no scrape job
for Alertmanager. The metric had no series, so the rule evaluated to empty forever and could
never fire -- and Prometheus's /alerts page rendered it the same green as a healthy rule. The
stack logged 440 genuine delivery failures underneath it.

The live half of that checker needs a running Prometheus. These tests cover the half that does
not: the PromQL name extraction, which is where the false positives come from, and the
source-definition scan that separates "nothing can ever emit this" from "nothing has emitted it
yet". Both were wrong on the first attempt -- `by (le)` read as a missing metric named `le`, and
every `prometheus_client` Counter read as undefined because the rules use its exposed
`_total` spelling and the constructor does not.
"""
from __future__ import annotations

import os
import sys
import unittest


def _repo_root() -> str:
    here = os.path.abspath(os.path.dirname(__file__))
    while here != os.path.dirname(here):
        if os.path.exists(os.path.join(here, "docker-compose.yml")):
            return here
        here = os.path.dirname(here)
    raise RuntimeError("could not locate the repository root")


REPO = _repo_root()
sys.path.insert(0, os.path.join(REPO, "scripts"))
import check_alert_metrics as cam  # noqa: E402


class MetricNameExtractionTest(unittest.TestCase):
    def test_plain_counter(self) -> None:
        self.assertEqual(
            cam.metric_names_in("rate(alertmanager_notifications_failed_total[10m]) > 0"),
            {"alertmanager_notifications_failed_total"},
        )

    def test_label_matchers_are_not_metrics(self) -> None:
        self.assertEqual(
            cam.metric_names_in('rate(rulemind_bundle_syncs_total{status!="ok"}[15m]) > 0'),
            {"rulemind_bundle_syncs_total"},
        )

    def test_aggregation_modifier_labels_are_not_metrics(self) -> None:
        """`by (le, source)` sits OUTSIDE any braces. Missing this reported a dead metric named
        `le` on both latency rules -- two false positives on a check whose whole value is that
        you believe its output."""
        expr = ('histogram_quantile(0.95, sum by (le, source) '
                '(rate(rulemind_decision_seconds_bucket[5m]))) > 0.25')
        self.assertEqual(cam.metric_names_in(expr), {"rulemind_decision_seconds_bucket"})

    def test_without_and_on_are_handled_too(self) -> None:
        self.assertEqual(
            cam.metric_names_in("sum without (pod) (foo_total) / on (tenant) bar_total"),
            {"foo_total", "bar_total"},
        )

    def test_vector_literal_references_no_metric(self) -> None:
        """The Watchdog is `vector(1)`: absence is the signal, so it names nothing."""
        self.assertEqual(cam.metric_names_in("vector(1)"), set())

    def test_functions_are_not_metrics(self) -> None:
        got = cam.metric_names_in("histogram_quantile(0.99, rate(x_bucket[5m]))")
        self.assertEqual(got, {"x_bucket"})


class SourceDefinitionScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.defined = cam.defined_metric_names(REPO)

    def test_finds_metrics_the_app_constructs(self) -> None:
        self.assertIn("rulemind_bundle_syncs_total", self.defined,
                      "app/main.py constructs this Counter; a rule querying it is untested, "
                      "not dead, and must not be reported as a failure")
        self.assertIn("rulemind_decisions_total", self.defined)

    def test_counter_total_suffix_is_recorded(self) -> None:
        """prometheus_client exposes Counter('x') as `x_total`, and the rules use the exposed
        spelling. Without the suffix every Counter in the repo reads as undefined."""
        self.assertIn("rulemind_decisions_total", self.defined)

    def test_third_party_metrics_are_not_claimed_as_ours(self) -> None:
        """Alertmanager's own metric is NOT defined by this repo -- which is exactly why its
        absence had to be a failure rather than a warning."""
        self.assertNotIn("alertmanager_notifications_failed_total", self.defined)

    def test_invented_names_are_not_defined(self) -> None:
        self.assertNotIn("rulemind_metric_that_nothing_emits_total", self.defined)


class PrometheusConfigTest(unittest.TestCase):
    def test_alertmanager_is_scraped(self) -> None:
        """The original defect. `alerting:` points Prometheus AT Alertmanager, which looks like
        the two are wired together, but it only says where to SEND alerts -- it scrapes nothing.
        Without a scrape job, every rule reading an `alertmanager_*` metric is dead."""
        import yaml

        with open(os.path.join(REPO, "observability", "prometheus.yml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        targets = set()
        for job in cfg.get("scrape_configs", []):
            for sc in job.get("static_configs", []):
                targets.update(sc.get("targets", []))
        self.assertTrue(
            any(t.startswith("alertmanager:") for t in targets),
            "observability/prometheus.yml has no scrape_config for Alertmanager, so "
            "alertmanager_notifications_failed_total has no series and RuleMindPagingIsFailing "
            "can never fire. Found targets: %s" % sorted(targets),
        )


if __name__ == "__main__":
    unittest.main()
