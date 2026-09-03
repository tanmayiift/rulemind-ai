"""Pluggable decision-log archiver — dump aged decisions out of the hot DB into an OLAP store.

The append-only decision log is the fastest-growing table in the system. Keeping every decision
in the transactional DB forever inflates storage and slows queries. This module lets an operator
push decisions older than the workspace's retention window into a cheap analytical store, then
purge them from the hot DB — so the OLTP database stays lean while history is preserved for
analytics.

The sink is pluggable and client-chosen (`DECISION_ARCHIVE_SINK`):
  - `none`     (default) — archiving disabled; nothing is purged (current behavior, zero risk).
  - `clickhouse` — insert into a ClickHouse table (columnar OLAP; great for decision analytics).
  - `s3`         — write batched objects to S3/GCS-compatible object storage (data-lake; query
                   later with Athena/DuckDB/Spark). NDJSON by default, Parquet when pyarrow is
                   installed (`DECISION_ARCHIVE_S3_FORMAT`).
  - `memory`     — in-process list, for tests.

Sink clients (`clickhouse-connect`, `boto3`, `pyarrow`) are imported lazily, only when that sink
is selected, so the app and its tests run without those optional dependencies installed.

The design is use-case agnostic: it archives the generic decision record (id, policy, outcome,
latency, variant, timestamp, redacted payload) for ANY decisioning domain — lending, fraud,
insurance, eligibility, pricing, content moderation — not anything lending-specific.
"""
from __future__ import annotations

import gzip
import io
import json
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class DecisionArchiver(ABC):
    """Writes batches of decision records to an external store. Implementations must be safe to
    call repeatedly; the caller only purges rows from the hot DB after write() returns without
    raising."""

    name: str = "archiver"

    @abstractmethod
    def write(self, decisions: List[Dict[str, Any]]) -> None:  # pragma: no cover - interface
        ...


class NullArchiver(DecisionArchiver):
    """Archiving disabled. Never used to purge (the retention job skips when the sink is null)."""

    name = "none"

    def write(self, decisions: List[Dict[str, Any]]) -> None:
        return None


class MemoryArchiver(DecisionArchiver):
    """In-process sink for tests: records everything written."""

    name = "memory"

    def __init__(self) -> None:
        self.written: List[Dict[str, Any]] = []
        self.batches: int = 0

    def write(self, decisions: List[Dict[str, Any]]) -> None:
        self.written.extend(decisions)
        self.batches += 1


class ClickHouseArchiver(DecisionArchiver):
    """Insert decisions into a ClickHouse table. Lazy-imports clickhouse-connect."""

    name = "clickhouse"

    def __init__(self, env: Optional[Dict[str, str]] = None) -> None:
        # A separate name, not a rebind: reassigning the Optional parameter keeps its
        # declared type, so every .get() below is an error on the None arm.
        settings = env if env is not None else os.environ
        self._table = settings.get("CLICKHOUSE_TABLE", "rulemind_decisions")
        self._settings = {
            "host": settings.get("CLICKHOUSE_HOST", "localhost"),
            "port": int(settings.get("CLICKHOUSE_PORT", "8123")),
            "username": settings.get("CLICKHOUSE_USER", "default"),
            "password": settings.get("CLICKHOUSE_PASSWORD", ""),
            "database": settings.get("CLICKHOUSE_DATABASE", "default"),
        }
        self._client = None

    def _connect(self):
        if self._client is None:
            import clickhouse_connect  # lazy: only needed when this sink is selected

            self._client = clickhouse_connect.get_client(**self._settings)
        return self._client

    def write(self, decisions: List[Dict[str, Any]]) -> None:
        if not decisions:
            return
        client = self._connect()
        columns = ["id", "tenant_id", "policy_id", "outcome", "latency_ms", "source", "experiment_id", "experiment_variant", "created_at", "record"]
        rows = [
            [
                d.get("id"),
                d.get("tenant_id"),
                d.get("policy_id"),
                d.get("outcome"),
                int(d.get("latency_ms") or 0),
                d.get("source"),
                d.get("experiment_id"),
                d.get("experiment_variant"),
                d.get("created_at"),
                json.dumps(d, default=str),
            ]
            for d in decisions
        ]
        client.insert(self._table, rows, column_names=columns)


class S3Archiver(DecisionArchiver):
    """Write batched decision objects to S3-compatible object storage. Lazy-imports boto3
    (and pyarrow only for Parquet). One object per batch, keyed by tenant + timestamp."""

    name = "s3"

    def __init__(self, env: Optional[Dict[str, str]] = None) -> None:
        # A separate name, not a rebind: reassigning the Optional parameter keeps its
        # declared type, so every .get() below is an error on the None arm.
        settings = env if env is not None else os.environ
        self._bucket = settings.get("DECISION_ARCHIVE_S3_BUCKET", "")
        self._prefix = settings.get("DECISION_ARCHIVE_S3_PREFIX", "rulemind/decisions").strip("/")
        self._format = settings.get("DECISION_ARCHIVE_S3_FORMAT", "jsonl").lower()
        self._endpoint = settings.get("DECISION_ARCHIVE_S3_ENDPOINT") or None  # S3-compatible (MinIO/GCS)
        self._region = settings.get("AWS_REGION") or settings.get("DECISION_ARCHIVE_S3_REGION")
        self._client = None

    def _connect(self):
        if self._client is None:
            import boto3  # lazy

            self._client = boto3.client("s3", endpoint_url=self._endpoint, region_name=self._region)
        return self._client

    def _serialize(self, decisions: List[Dict[str, Any]]) -> tuple:
        if self._format == "parquet":
            try:
                import pyarrow as pa  # lazy, optional
                import pyarrow.parquet as pq

                table = pa.Table.from_pylist([{k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in d.items()} for d in decisions])
                buf = io.BytesIO()
                pq.write_table(table, buf)
                return buf.getvalue(), "parquet"
            except Exception:
                pass  # fall back to NDJSON if pyarrow is unavailable
        body = "\n".join(json.dumps(d, default=str) for d in decisions).encode("utf-8")
        return gzip.compress(body), "jsonl.gz"

    def write(self, decisions: List[Dict[str, Any]]) -> None:
        if not decisions:
            return
        if not self._bucket:
            raise RuntimeError("DECISION_ARCHIVE_S3_BUCKET is required for the s3 archive sink.")
        client = self._connect()
        tenant = decisions[0].get("tenant_id", "unknown")
        first_ts = str(decisions[0].get("created_at", "")).replace(":", "").replace("-", "")[:15] or "batch"
        body, ext = self._serialize(decisions)
        key = "{0}/{1}/{2}-{3}.{4}".format(self._prefix, tenant, first_ts, len(decisions), ext)
        client.put_object(Bucket=self._bucket, Key=key, Body=body)


_SINKS = {
    "none": NullArchiver,
    "memory": MemoryArchiver,
    "clickhouse": ClickHouseArchiver,
    "s3": S3Archiver,
}


def get_archiver(env: Optional[Dict[str, str]] = None) -> DecisionArchiver:
    """Build the configured archiver from DECISION_ARCHIVE_SINK (default 'none')."""
    # A separate name, not a rebind: reassigning the Optional parameter keeps its
    # declared type, so every .get() below is an error on the None arm.
    settings = env if env is not None else os.environ
    sink = (settings.get("DECISION_ARCHIVE_SINK", "none") or "none").strip().lower()
    factory = _SINKS.get(sink, NullArchiver)
    if factory in (ClickHouseArchiver, S3Archiver):
        # The `in` check narrows at runtime but not for mypy: `factory` is the union of all
        # sink classes, and NullArchiver.__init__ takes no arguments.
        return factory(env)  # type: ignore[call-arg]
    return factory()


def archiving_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    # A separate name, not a rebind: reassigning the Optional parameter keeps its
    # declared type, so every .get() below is an error on the None arm.
    settings = env if env is not None else os.environ
    return (settings.get("DECISION_ARCHIVE_SINK", "none") or "none").strip().lower() not in ("", "none")
