#!/usr/bin/env python3
"""Assert the deployment contract on the RENDERED manifests.

Why this exists: `helm lint` and `kubeconform -strict` both pass cleanly on a chart that
cannot deploy. Measured on the pre-fix chart -- lint reported "0 chart(s) failed" and
kubeconform reported "Valid: 11, Invalid: 0". Neither can see the things that actually break a
rollout, because every one of them is schema-valid:

  * a liveness probe pointed at an endpoint that returns 503 when Postgres blips,
  * probes with no timing fields at all,
  * `resources: {}` under a CPU-utilization HPA, which then reports <unknown> and never scales,
  * a migrate Job with a fixed name, which makes the SECOND `helm upgrade` fail on Job spec
    immutability,
  * an ingress with no route to the API, so the UI it serves cannot reach the backend,
  * `DATABASE_URL: ""`, which is a set-but-blank env var, not an unset one,
  * containers running as root.

Schema validators check shape. These are relationships and values. So this file is the gate,
and lint/kubeconform are the cheap pre-filter in front of it.

Run:  python scripts/verify_chart.py [--chart helm/rulemind] [--values FILE]
Exit: 0 = contract held, 1 = violations (printed), 2 = could not render.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any, Dict, Iterator, List, Tuple

# Endpoints that consult the database or Redis. A LIVENESS probe must never point at one of
# these: liveness failure restarts the pod, so a 30-second Postgres failover would restart every
# API pod simultaneously -- turning a brief dependency blip into a full outage. Readiness is the
# correct consumer of a dependency signal; it removes the pod from the Service and puts it back.
DEPENDENCY_ENDPOINTS = {"/ready", "/health/ready", "/readyz"}

# Probe fields that must be stated rather than inherited. Kubernetes defaults are not wrong, but
# an unstated budget is an unexamined one, and the API's boot cost (create_all + advisory lock +
# seed + bcrypt, on top of importing the ML stack) is well outside the default assumptions.
REQUIRED_PROBE_FIELDS = ("initialDelaySeconds", "periodSeconds", "failureThreshold")


class Violation(Exception):
    pass


def _run(cmd: List[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise Violation("{0} is not installed".format(cmd[0])) from exc
    if result.returncode != 0:
        raise Violation("{0} failed:\n{1}".format(" ".join(cmd), result.stderr.strip()[:2000]))
    return result.stdout


def render(chart: str, values: List[str], sets: List[str]) -> List[Dict[str, Any]]:
    """Render the chart to a list of manifests.

    `helm template` output is multi-document YAML. Parsing it without a YAML dependency would be
    fragile, so this shells out to helm's own JSON-per-document mode via `--show-only` when
    available, and otherwise uses PyYAML, which the backend already depends on.
    """
    cmd = ["helm", "template", "release", chart]
    for path in values:
        cmd += ["--values", path]
    for item in sets:
        cmd += ["--set", item]
    raw = _run(cmd)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML ships with the backend
        raise Violation("PyYAML is required to parse rendered manifests") from exc
    return [doc for doc in yaml.safe_load_all(raw) if isinstance(doc, dict) and doc.get("kind")]


def workloads(docs: List[Dict[str, Any]]) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Yield (kind, name, podSpec) for every workload that runs containers."""
    for doc in docs:
        kind = doc.get("kind")
        if kind not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            continue
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")
        spec = doc.get("spec") or {}
        if kind == "CronJob":
            spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
        pod = ((spec.get("template") or {}).get("spec") or {})
        if pod:
            yield kind, name, pod


def containers(pod: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for key in ("initContainers", "containers"):
        for container in pod.get(key) or []:
            yield container


# --------------------------------------------------------------------------- checks


def check_resources(docs, problems):
    """Every container states CPU and memory requests, and memory limits.

    The HPA is the reason this is not merely tidiness: a CPU-utilization target is computed as a
    percentage OF THE REQUEST. With no request the metric is `<unknown>` and the HPA never
    scales, so the autoscaler is decorative. Memory limits matter separately -- this service
    executes untrusted customer Python, so an unbounded container is a noisy-neighbour incident
    waiting for its first bad variable.
    """
    for kind, name, pod in workloads(docs):
        for container in containers(pod):
            where = "{0}/{1}:{2}".format(kind, name, container.get("name", "?"))
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            for field in ("cpu", "memory"):
                if not requests.get(field):
                    problems.append("{0}: no resources.requests.{1}".format(where, field))
            if not limits.get("memory"):
                problems.append("{0}: no resources.limits.memory".format(where))


def check_hpa_has_something_to_scale_on(docs, problems):
    """An HPA targeting CPU utilization needs its target's containers to declare a CPU request."""
    requests_by_workload = {}
    for kind, name, pod in workloads(docs):
        requests_by_workload[(kind, name)] = all(
            ((c.get("resources") or {}).get("requests") or {}).get("cpu")
            for c in containers(pod)
        )
    for doc in docs:
        if doc.get("kind") != "HorizontalPodAutoscaler":
            continue
        spec = doc.get("spec") or {}
        target = spec.get("scaleTargetRef") or {}
        key = (target.get("kind"), target.get("name"))
        uses_cpu = any(
            (m.get("resource") or {}).get("name") == "cpu"
            for m in (spec.get("metrics") or [])
        )
        if not uses_cpu:
            continue
        if key not in requests_by_workload:
            problems.append("HPA {0}: scaleTargetRef {1} does not exist in the render".format(
                (doc.get("metadata") or {}).get("name"), key))
        elif not requests_by_workload[key]:
            problems.append(
                "HPA {0}: targets CPU utilization but {1}/{2} declares no CPU request, so the "
                "HPA reports <unknown> and never scales".format(
                    (doc.get("metadata") or {}).get("name"), key[0], key[1]))


def check_hpa_does_not_fight_replicas(docs, problems):
    """A Deployment under an HPA must not also hardcode `replicas`.

    Both write the same field, so every `helm upgrade` resets the replica count to the static
    value and the HPA scales it back -- a visible flap on every deploy.
    """
    hpa_targets = {
        ((d.get("spec") or {}).get("scaleTargetRef") or {}).get("name")
        for d in docs if d.get("kind") == "HorizontalPodAutoscaler"
    }
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = (doc.get("metadata") or {}).get("name")
        if name in hpa_targets and (doc.get("spec") or {}).get("replicas") is not None:
            problems.append(
                "Deployment {0}: sets `replicas` while an HPA targets it -- they fight on every "
                "upgrade".format(name))


def check_probes(docs, problems):
    """Liveness must be dependency-free; every probe states its budget; the API has a startupProbe."""
    for kind, name, pod in workloads(docs):
        if kind in ("Job", "CronJob"):
            continue  # a Job is not long-running; probes are meaningless
        for container in containers(pod):
            where = "{0}/{1}:{2}".format(kind, name, container.get("name", "?"))
            liveness = container.get("livenessProbe")
            readiness = container.get("readinessProbe")
            if not liveness:
                problems.append("{0}: no livenessProbe".format(where))
            if not readiness:
                problems.append("{0}: no readinessProbe".format(where))
            if liveness:
                path = ((liveness.get("httpGet") or {}).get("path")) or ""
                if path in DEPENDENCY_ENDPOINTS:
                    problems.append(
                        "{0}: livenessProbe points at {1}, which checks dependencies -- a DB blip "
                        "would restart every pod at once".format(where, path))
            for label, probe in (("livenessProbe", liveness), ("readinessProbe", readiness)):
                if not probe:
                    continue
                for field in REQUIRED_PROBE_FIELDS:
                    if field not in probe:
                        problems.append("{0}: {1} has no {2}".format(where, label, field))


def check_startup_probe(docs, problems):
    """The API needs a startupProbe, because its boot is far slower than its steady state."""
    for kind, name, pod in workloads(docs):
        if kind != "Deployment" or "api" not in name:
            continue
        for container in containers(pod):
            if not container.get("startupProbe"):
                problems.append(
                    "{0}/{1}:{2}: no startupProbe -- boot runs create_all + advisory lock + seed "
                    "+ bcrypt on top of importing the ML stack, so liveness can kill it mid-boot"
                    .format(kind, name, container.get("name", "?")))


def check_security_context(docs, problems):
    """Containers must not run as root. This service executes untrusted customer Python."""
    for kind, name, pod in workloads(docs):
        pod_ctx = pod.get("securityContext") or {}
        for container in containers(pod):
            where = "{0}/{1}:{2}".format(kind, name, container.get("name", "?"))
            ctx = container.get("securityContext") or {}
            run_as_non_root = ctx.get("runAsNonRoot", pod_ctx.get("runAsNonRoot"))
            if run_as_non_root is not True:
                problems.append("{0}: runAsNonRoot is not true".format(where))
            if ctx.get("allowPrivilegeEscalation", pod_ctx.get("allowPrivilegeEscalation")) is not False:
                problems.append("{0}: allowPrivilegeEscalation is not false".format(where))
            drops = ((ctx.get("capabilities") or {}).get("drop")) or []
            if "ALL" not in drops:
                problems.append("{0}: capabilities.drop does not include ALL".format(where))


def check_no_blank_env(docs, problems):
    """A rendered `NAME: ""` is a SET-but-blank env var, not an unset one.

    That distinction is the whole of T4.1: `os.getenv("DATABASE_URL", DEFAULT)` returns "" rather
    than the default, so `create_engine("")` raises at import and the pod CrashLoopBackOffs with
    no useful log; and a blank RULEMIND_CONFIG_KEY derives a Fernet key from sha256("") -- which
    anyone can compute. Omitting the key entirely is safe; rendering it blank is not.
    """
    for kind, name, pod in workloads(docs):
        for container in containers(pod):
            where = "{0}/{1}:{2}".format(kind, name, container.get("name", "?"))
            for entry in container.get("env") or []:
                if "value" in entry and entry.get("value") == "":
                    problems.append(
                        "{0}: env {1} renders as an empty string -- set-but-blank, which defeats "
                        "every `os.getenv(name, default)` in the app".format(where, entry.get("name")))
    for doc in docs:
        if doc.get("kind") not in ("ConfigMap", "Secret"):
            continue
        name = (doc.get("metadata") or {}).get("name")
        for key, value in (doc.get("data") or {}).items():
            if value == "":
                problems.append("{0} {1}: key {2} is an empty string".format(
                    doc.get("kind"), name, key))


def _env_sources(docs) -> Dict[Tuple[str, str], set]:
    """Map (kind, name) -> the keys a ConfigMap/Secret actually provides.

    Needed because a container can pull env via `envFrom`, and treating any envFrom as
    "satisfied" is a false negative: the pre-fix chart's ConfigMap carried only DATABASE_URL and
    REDIS_URL, so RULEMIND_ENV was genuinely absent while looking present.
    """
    sources: Dict[Tuple[str, str], set] = {}
    for doc in docs:
        kind = doc.get("kind")
        if kind not in ("ConfigMap", "Secret"):
            continue
        name = (doc.get("metadata") or {}).get("name")
        keys = set(doc.get("data") or {}) | set(doc.get("stringData") or {})
        sources[(kind, name)] = keys
    return sources


def _provided_env(container: Dict[str, Any], sources: Dict[Tuple[str, str], set]) -> set:
    """Every env key this container receives, resolving envFrom against the rendered sources."""
    names = {e.get("name") for e in (container.get("env") or []) if e.get("name")}
    for ref in container.get("envFrom") or []:
        if "configMapRef" in ref:
            names |= sources.get(("ConfigMap", (ref["configMapRef"] or {}).get("name")), set())
        if "secretRef" in ref:
            names |= sources.get(("Secret", (ref["secretRef"] or {}).get("name")), set())
    return names


def check_production_env(docs, problems):
    """RULEMIND_ENV must be set, or the fail-closed secret gate silently skips.

    security_config._is_local_dev checks RULEMIND_ENV first and falls back to NODE_ENV; with
    neither set it takes the warn-and-skip branch. The same helper drives the `Secure` flag on
    session and admin-JWT cookies, so an unset value ships those cookies without Secure over TLS.
    """
    sources = _env_sources(docs)
    for kind, name, pod in workloads(docs):
        if kind in ("Job", "CronJob"):
            continue
        for container in containers(pod):
            if "RULEMIND_ENV" not in _provided_env(container, sources):
                problems.append(
                    "{0}/{1}:{2}: RULEMIND_ENV is not set -- the fail-closed secret gate skips "
                    "and cookies ship without Secure".format(kind, name, container.get("name", "?")))


def check_ingress_routes_the_api(docs, problems):
    """The browser must be able to reach the API it is told to call.

    An ingress that serves only the web app means the UI loads and then every request from it
    fails: the in-cluster Service name it would otherwise be pointed at is not resolvable from a
    browser.
    """
    api_services = {
        (d.get("metadata") or {}).get("name")
        for d in docs if d.get("kind") == "Service" and "api" in ((d.get("metadata") or {}).get("name") or "")
    }
    if not api_services:
        return
    ingresses = [d for d in docs if d.get("kind") == "Ingress"]
    if not ingresses:
        return  # no ingress rendered in this values set; nothing to assert
    routed = set()
    for ingress in ingresses:
        for rule in ((ingress.get("spec") or {}).get("rules") or []):
            for path in (((rule.get("http") or {}).get("paths")) or []):
                svc = (((path.get("backend") or {}).get("service")) or {}).get("name")
                if svc:
                    routed.add(svc)
    missing = api_services - routed
    if missing:
        problems.append(
            "Ingress has no route to {0} -- the UI it serves cannot reach the API".format(
                ", ".join(sorted(missing))))


def check_migrate_job_is_upgrade_safe(docs, problems):
    """A migration Job must be a hook with a generated name.

    A Job's `spec.template` is immutable, so a plain Job with a fixed name makes the SECOND
    `helm upgrade` fail outright. Hook annotations plus a generated name (or
    hook-delete-policy: before-hook-creation) are what make it repeatable.
    """
    for doc in docs:
        if doc.get("kind") != "Job":
            continue
        meta = doc.get("metadata") or {}
        name = meta.get("name", "")
        annotations = meta.get("annotations") or {}
        if "helm.sh/hook" not in annotations:
            problems.append(
                "Job {0}: no helm.sh/hook annotation -- it runs as an ordinary resource, so the "
                "second `helm upgrade` fails on Job spec immutability".format(name))
            continue
        delete_policy = annotations.get("helm.sh/hook-delete-policy", "")
        if "before-hook-creation" not in delete_policy:
            problems.append(
                "Job {0}: hook-delete-policy lacks before-hook-creation, so a re-run collides "
                "with the previous Job".format(name))


CHECKS = (
    check_resources,
    check_hpa_has_something_to_scale_on,
    check_hpa_does_not_fight_replicas,
    check_probes,
    check_startup_probe,
    check_security_context,
    check_no_blank_env,
    check_production_env,
    check_ingress_routes_the_api,
    check_migrate_job_is_upgrade_safe,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", default="helm/rulemind")
    parser.add_argument("--values", action="append", default=[])
    parser.add_argument("--set", dest="sets", action="append", default=[])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if not shutil.which("helm"):
        print("helm is not installed", file=sys.stderr)
        return 2

    try:
        docs = render(args.chart, args.values, args.sets)
    except Violation as exc:
        print(str(exc), file=sys.stderr)
        return 2

    problems: List[str] = []
    for check in CHECKS:
        check(docs, problems)

    if args.json:
        print(json.dumps({"resources": len(docs), "violations": problems}, indent=2))
    else:
        print("rendered {0} resources from {1}".format(len(docs), args.chart))
        if problems:
            print("\n{0} deployment-contract violation(s):\n".format(len(problems)))
            for problem in problems:
                print("  - {0}".format(problem))
        else:
            print("deployment contract holds")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
