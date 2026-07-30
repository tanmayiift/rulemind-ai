"""Reports — pure, DB-free report generation over the decision log.

A report definition selects dynamic columns (dot-paths into a decision, including
its inputs `payload_preview.*` and computed features `computed_variables.*`),
applies filters (time window, outcome, policy, source), and formats timestamps in
the report's timezone. ``generate_report`` returns projected rows plus a CSV
rendering; scheduled email delivery (see app/mailer.py + the scheduler) reuses the
same function.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:  # stdlib on 3.9+; degrade to naive UTC formatting if a zone is unknown.
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Columns offered by default; the UI also suggests dynamic input/output paths.
BASE_COLUMNS = [
    {"key": "id", "label": "Decision ID", "path": "id"},
    {"key": "created_at", "label": "Timestamp", "path": "created_at"},
    {"key": "policy_id", "label": "Policy", "path": "policy_id"},
    {"key": "outcome", "label": "Outcome", "path": "outcome"},
    {"key": "latency_ms", "label": "Latency (ms)", "path": "latency_ms"},
    {"key": "source", "label": "Source", "path": "source"},
]


def _resolve_path(record: Dict[str, Any], path: str) -> Any:
    current: Any = record
    for token in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(token)
        elif isinstance(current, list) and token.isdigit():
            idx = int(token)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_timestamp(value: Any, tz_name: str) -> Any:
    dt = _parse_dt(value)
    if dt is None:
        return value
    if ZoneInfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _passes_filters(decision: Dict[str, Any], filters: Dict[str, Any], cutoff: Optional[datetime]) -> bool:
    if cutoff is not None:
        created = _parse_dt(decision.get("created_at"))
        if created is not None and created < cutoff:
            return False
    outcomes = filters.get("outcomes") or []
    if outcomes and decision.get("outcome") not in outcomes:
        return False
    policies = filters.get("policy_ids") or []
    if policies and decision.get("policy_id") not in policies:
        return False
    sources = filters.get("sources") or []
    if sources and decision.get("source") not in sources:
        return False
    users = filters.get("users") or []
    if users:
        uid = _resolve_path(decision, "payload_preview.user_id") or _resolve_path(decision, "user_id")
        if uid not in users:
            return False
    return True


def generate_report(definition: Dict[str, Any], decisions: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Project + filter decisions per the report definition; return rows + CSV."""
    columns = definition.get("columns") or BASE_COLUMNS
    filters = definition.get("filters") or {}
    tz_name = definition.get("timezone") or "UTC"
    now = now or datetime.now(timezone.utc)

    cutoff: Optional[datetime] = None
    days = filters.get("days")
    if days:
        try:
            cutoff = now - timedelta(days=float(days))
        except (TypeError, ValueError):
            cutoff = None

    rows: List[Dict[str, Any]] = []
    for decision in decisions:
        if not _passes_filters(decision, filters, cutoff):
            continue
        row: Dict[str, Any] = {}
        for col in columns:
            value = _resolve_path(decision, col.get("path", col.get("key", "")))
            if col.get("path") == "created_at" or col.get("key") == "created_at":
                value = _format_timestamp(value, tz_name)
            row[col.get("key", col.get("path"))] = value
        rows.append(row)

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "timezone": tz_name,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "csv": to_csv(columns, rows),
    }


def to_csv(columns: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    keys = [c.get("key", c.get("path")) for c in columns]
    writer = csv.writer(buffer)
    writer.writerow([c.get("label", c.get("key")) for c in columns])
    for row in rows:
        writer.writerow(["" if row.get(k) is None else row.get(k) for k in keys])
    return buffer.getvalue()


def suggest_columns(decisions: List[Dict[str, Any]], limit: int = 40) -> List[Dict[str, Any]]:
    """Introspect recent decisions to offer dynamic input/output columns."""
    suggestions: List[Dict[str, Any]] = list(BASE_COLUMNS)
    seen = {c["path"] for c in suggestions}

    def _walk(prefix: str, obj: Any, label_prefix: str) -> None:
        if not isinstance(obj, dict):
            return
        for key, value in obj.items():
            path = f"{prefix}.{key}"
            if isinstance(value, dict):
                _walk(path, value, f"{label_prefix} {key}")
            elif path not in seen and len(suggestions) < limit:
                seen.add(path)
                suggestions.append({"key": path.replace(".", "_"), "label": f"{label_prefix} {key}".strip(), "path": path})

    for decision in decisions[:25]:
        _walk("payload_preview", decision.get("payload_preview") or {}, "Input:")
        _walk("computed_variables", decision.get("computed_variables") or {}, "Var:")
    return suggestions
