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

This module also owns the RETENTION WINDOW arithmetic (`retention_cutoff` and friends below),
because the window is what decides which rows a sink ever sees. It lives here rather than in the
scheduler so the enforcement job, the settings write path and the tests all resolve
months-vs-days through exactly one function.
"""
from __future__ import annotations

import calendar
import gzip
import io
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

# ── Retention window ────────────────────────────────────────────────────────────────────────
# Retention is configured in calendar months (`audit_retention_months`) or, for workspaces that
# predate months, in days (`audit_retention_days`). Everything that needs "how far back does
# this workspace keep decisions?" goes through `retention_cutoff`.

# 100 years. A bound, not a policy: `add_months` shifts a datetime's year field, and an absurd
# stored value would otherwise raise ValueError inside the purge job instead of being clamped.
MAX_RETENTION_MONTHS = 1200


def add_months(moment: datetime, months: int) -> datetime:
    """Shift `moment` by whole calendar months. Negative shifts go backwards.

    END-OF-MONTH CHOICE — **clamp to the last valid day of the target month.** 31 March minus one
    month is 28 February (29th in a leap year), not 3 March; 31 January plus one month is 28/29
    February, not 2/3 March. This is deliberate on two grounds:

    * It is what every other calendar implementation an operator will compare against does —
      dateutil's ``relativedelta``, PostgreSQL's ``interval '1 month'``, ``java.time``'s
      ``plusMonths``, .NET's ``AddMonths``. A retention window that disagreed with the database's
      own date arithmetic would be indefensible in an audit.
    * It is the safe direction here. Retention cutoffs are computed by SUBTRACTING months from
      now, and clamping puts the cutoff EARLIER than overflow would (28 Feb rather than 3 Mar),
      so a window can only ever be honoured a day or two longer — never cut short. Retaining a
      day too long is untidy; purging a day early breaks the promise the retention setting IS.
    """
    total = moment.year * 12 + (moment.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def normalize_retention_months(value: Any) -> Optional[int]:
    """Coerce a submitted or stored month count into a usable one.

    None, zero, negatives and garbage all collapse to None, which means "months are not in use
    for this workspace" — NOT "retain nothing". Anything else is clamped to
    ``[1, MAX_RETENTION_MONTHS]``."""
    if value is None:
        return None
    try:
        months = int(value)
    except (TypeError, ValueError):
        return None
    if months <= 0:
        return None
    return min(months, MAX_RETENTION_MONTHS)


def retention_window_days(months: Any, now: Optional[datetime] = None) -> int:
    """The month window measured in days against the real calendar as of `now`.

    This is the lossy direction and exists only to keep `audit_retention_days` — and every reader
    that still speaks days — approximately right for a months-configured workspace. Measured, not
    multiplied: 84 months is 2557 days, not the 2555 that ``years * 365`` gives, and those two
    leap days are exactly the error a hand conversion makes. The result drifts by at most a day as
    leap years pass, which is why it is never used for enforcement — `retention_cutoff` is."""
    moment = now or datetime.utcnow()
    normalized = normalize_retention_months(months)
    if not normalized:
        return 0
    return (moment - add_months(moment, -normalized)).days


def retention_cutoff(settings: Mapping[str, Any], now: Optional[datetime] = None) -> Optional[datetime]:
    """The instant before which a workspace's decisions are outside its retention window, or None
    when retention is disabled for that workspace.

    Months win over days when set, because months is what the customer bought; days is the legacy
    encoding kept working for every tenant that never migrated. Returning None rather than a
    far-past datetime is deliberate — None means "purge nothing", so an unset, malformed or
    unreadable setting can never delete more than intended."""
    moment = now or datetime.utcnow()
    months = normalize_retention_months(settings.get("audit_retention_months"))
    if months:
        return add_months(moment, -months)
    try:
        days = int(settings.get("audit_retention_days") or 0)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return moment - timedelta(days=days)


class DecisionArchiver(ABC):
    """Writes batches of decision records to an external store. Implementations must be safe to
    call repeatedly; the caller only purges rows from the hot DB after write() returns without
    raising."""

    name: str = "archiver"

    @abstractmethod
    def write(self, decisions: List[Dict[str, Any]]) -> None:  # pragma: no cover - interface
        ...

    def write_predictor_values(self, values: List[Dict[str, Any]]) -> None:
        """Ship predictor observations to the same cold store (P10 + P20).

        A DEFAULTED NO-OP, not an abstractmethod, so every existing sink keeps working untouched
        and a sink that has no useful place to put these simply does not. Making it abstract
        would break NullArchiver and any out-of-tree implementation for a capability most sinks
        do not need.

        Separate from `write` rather than folded into it because the two have different shapes and
        different lifetimes: a decision is archived when it falls out of the retention window,
        while a predictor value is copied out as it is produced -- the hot copy is what the drift
        report reads, so this is a fan-out for long-horizon analysis, not a move.
        """
        return None

    def available(self) -> Tuple[bool, str]:
        """Can this sink actually run here? (ok, reason-when-not).

        Sink clients are lazy optional imports and NONE of them -- clickhouse-connect, boto3,
        pyarrow -- is in requirements.txt, so they are absent from the shipped image. Setting
        DECISION_ARCHIVE_SINK=clickhouse therefore produced an ImportError on the first write,
        the purge (which only runs after a clean write) never happened, and
        `GET /api/v1/governance/...` went on reporting `retention_enforced: true` because it
        only looked at whether the variable was set. A workspace that configured "purge after
        24 months" for a compliance obligation was retaining forever, and the endpoint it would
        check to find that out said the opposite.
        """
        return True, ""


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
        self.predictor_values: List[Dict[str, Any]] = []
        self.batches: int = 0

    def write(self, decisions: List[Dict[str, Any]]) -> None:
        self.written.extend(decisions)
        self.batches += 1

    def write_predictor_values(self, values: List[Dict[str, Any]]) -> None:
        self.predictor_values.extend(values)


class ClickHouseArchiver(DecisionArchiver):
    """Insert decisions into a ClickHouse table. Lazy-imports clickhouse-connect."""

    name = "clickhouse"

    def __init__(self, env: Optional[Dict[str, str]] = None) -> None:
        # A separate name, not a rebind: reassigning the Optional parameter keeps its
        # declared type, so every .get() below is an error on the None arm.
        settings = env if env is not None else os.environ
        self._table = settings.get("CLICKHOUSE_TABLE", "rulemind_decisions")
        self._predictor_table = settings.get("CLICKHOUSE_PREDICTOR_TABLE", "rulemind_predictor_values")
        self._settings = {
            "host": settings.get("CLICKHOUSE_HOST", "localhost"),
            "port": int(settings.get("CLICKHOUSE_PORT", "8123")),
            "username": settings.get("CLICKHOUSE_USER", "default"),
            "password": settings.get("CLICKHOUSE_PASSWORD", ""),
            "database": settings.get("CLICKHOUSE_DATABASE", "default"),
        }
        self._client = None

    def available(self) -> Tuple[bool, str]:
        try:
            import clickhouse_connect  # noqa: F401
        except ImportError:
            return False, ("clickhouse-connect is not installed, so nothing can be archived and "
                           "no decision is ever purged. `pip install clickhouse-connect`.")
        return True, ""

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

    def write_predictor_values(self, values: List[Dict[str, Any]]) -> None:
        """Its own table, not a column on the decisions table.

        A predictor value is one number per predictor per decision, so folding it in would mean
        either a wide sparse row per decision or N decision rows per decision. A narrow table is
        also what an OLAP store is good at: the drift query is a scan of one column over a time
        range, which is the shape ClickHouse exists for.
        """
        if not values:
            return
        client = self._connect()
        columns = ["predictor_id", "tenant_id", "decision_id", "value", "created_at"]
        rows = [
            [
                v.get("predictor_id"),
                v.get("tenant_id"),
                v.get("decision_id"),
                float(v.get("value") or 0.0),
                v.get("created_at"),
            ]
            for v in values
        ]
        client.insert(self._predictor_table, rows, column_names=columns)


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

    def available(self) -> Tuple[bool, str]:
        try:
            import boto3  # noqa: F401
        except ImportError:
            return False, ("boto3 is not installed, so nothing can be archived and no decision "
                           "is ever purged. `pip install boto3`.")
        if not self._bucket:
            return False, "DECISION_ARCHIVE_S3_BUCKET is not set."
        if self._format == "parquet":
            try:
                import pyarrow  # noqa: F401
            except ImportError:
                return False, ("DECISION_ARCHIVE_S3_FORMAT=parquet needs pyarrow, which is not "
                               "installed. `pip install pyarrow`, or use jsonl.")
        return True, ""

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


def archiving_available(env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Whether the configured sink is both selected AND able to run.

    `archiving_enabled` answers only the first half, which is what made
    `retention_enforced` a claim about configuration rather than about behaviour.
    """
    if not archiving_enabled(env):
        return False, "DECISION_ARCHIVE_SINK is not set, so nothing is archived or purged."
    return get_archiver(env).available()


def archiving_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    # A separate name, not a rebind: reassigning the Optional parameter keeps its
    # declared type, so every .get() below is an error on the None arm.
    settings = env if env is not None else os.environ
    return (settings.get("DECISION_ARCHIVE_SINK", "none") or "none").strip().lower() not in ("", "none")
