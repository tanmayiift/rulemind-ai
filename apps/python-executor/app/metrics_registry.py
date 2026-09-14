"""One place that knows how Prometheus collectors are created and scraped.

Two problems this exists to solve.

**1. ``/metrics`` was per-process, and therefore wrong.** The Dockerfile runs uvicorn
with ``--workers 4``. Four processes each keep their own counters; a scrape is routed
to *one of them at random*. So ``rulemind_decisions_total`` appears to jump around and
go backwards between scrapes, and ``rate()`` over it is meaningless — the one query an
operator actually writes. ``prometheus_client`` solves this with a multiprocess
registry: workers write counter values into mmap files under
``PROMETHEUS_MULTIPROC_DIR`` and the scrape sums them. That variable appeared nowhere
in this repo, so the mode was never on.

**2. Collector definitions were duplicated and re-registered.** Collectors live in
three modules, and ``decision_log`` reached into ``REGISTRY._names_to_collectors`` by
hand to survive an ``importlib.reload`` in its tests. That private dict is version
specific and its keys differ between a ``Counter`` (registered as both ``name`` and
``name_total``) and a ``Gauge``; ``get_or_create`` below does the same job through one
tested path, and falls back to the public registration error when the private
attribute is missing.

Notes for anyone adding a metric:

* ``Gauge`` in multiprocess mode needs an explicit ``multiprocess_mode`` (``"livesum"``,
  ``"max"``, …) or you get one time series per worker pid. ``Counter`` and ``Histogram``
  aggregate correctly with no extra argument.
* The mmap directory must be **empty at container start** and shared by every worker of
  one container. Creating it is done here; wiping it belongs in the entrypoint, before
  the master forks — a worker cannot safely wipe files its siblings are writing.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest

from . import env

logger = logging.getLogger("rulemind.metrics")


def multiproc_dir() -> Optional[str]:
    """``PROMETHEUS_MULTIPROC_DIR`` (blank-aware), or None when not configured.

    ``prometheus_multiproc_dir`` is the older lowercase spelling that some charts still
    set; accept both so a working deployment is not silently downgraded to per-process
    metrics by a casing difference.
    """
    return env.env_opt("PROMETHEUS_MULTIPROC_DIR") or env.env_opt("prometheus_multiproc_dir")


def is_multiprocess() -> bool:
    return multiproc_dir() is not None


def prepare_multiproc_dir() -> Optional[str]:
    """Ensure the mmap directory exists. Returns the path, or None when not configured.

    Deliberately does NOT delete existing files: under ``--workers 4`` this runs once per
    worker, and a worker that wiped the directory would destroy the counters its three
    siblings are actively writing — the same class of bug as the WAL's unscoped delete.
    Wiping is the entrypoint's job, before the master forks.
    """
    path = multiproc_dir()
    if not path:
        return None
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning("PROMETHEUS_MULTIPROC_DIR=%s is not usable (%s); "
                       "metrics will be per-process and rate() over them will be wrong.", path, exc)
        return None
    return path


def _find_existing(name: str) -> Optional[Any]:
    """The already-registered collector for `name`, if any.

    A Counter registers under both ``name`` and ``name_total``; a Gauge only under
    ``name``. Both spellings are checked so this works for either.
    """
    names_to_collectors = getattr(REGISTRY, "_names_to_collectors", None)
    if isinstance(names_to_collectors, dict):
        return names_to_collectors.get(name) or names_to_collectors.get(name + "_total")
    return None


def get_or_create(metric_cls: Any, name: str, documentation: str,
                  labelnames: Tuple[str, ...] = (), **kwargs: Any) -> Any:
    """Return the existing collector named `name`, or create and register it.

    Needed because module reload (the durability tests use ``importlib.reload``) and
    double-import re-run the constructor, which raises "Duplicated timeseries in
    CollectorRegistry". Falling back on the ValueError — not only on the private dict —
    means this keeps working if ``_names_to_collectors`` is ever renamed upstream.
    """
    existing = _find_existing(name)
    if existing is not None:
        return existing
    try:
        return metric_cls(name, documentation, labelnames, **kwargs)
    except ValueError:
        existing = _find_existing(name)
        if existing is None:
            raise
        return existing


def build_registry() -> CollectorRegistry:
    """The registry a scrape should render.

    In multiprocess mode this is a *fresh* registry carrying only a
    ``MultiProcessCollector``, which reads every worker's mmap files and sums them —
    that is the whole point. Building it per scrape is intentional and is what the
    upstream docs prescribe; reusing one would cache the set of pid files and miss
    workers that started later.
    """
    path = multiproc_dir()
    if not path:
        return REGISTRY
    try:
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=path)
        return registry
    except Exception as exc:  # pragma: no cover - misconfiguration path
        logger.warning("Multiprocess metrics collection failed (%s); "
                       "falling back to this worker's own registry.", exc)
        return REGISTRY


def render() -> Tuple[bytes, str]:
    """(payload, content_type) for the /metrics endpoint. One call, so the handler
    cannot forget the multiprocess branch."""
    return generate_latest(build_registry()), CONTENT_TYPE_LATEST


def mark_process_dead(pid: Optional[int] = None) -> None:
    """Drop a dead worker's per-pid gauge files so its values stop being summed.

    Counters and histograms are kept on purpose — their totals must survive a worker
    restart or every rolling deploy would look like a counter reset.
    """
    path = multiproc_dir()
    if not path:
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid if pid is not None else os.getpid(), path)
    except Exception:  # pragma: no cover - best effort cleanup
        pass
