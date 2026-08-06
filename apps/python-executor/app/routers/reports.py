"""Reports-builder endpoints — list/CRUD, column suggestions, email config, preview, run, CSV
export, scheduled send. Extracted verbatim from app/main.py. Stable models imported by value from
app.main; direct storage calls use main.storage live."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response

from .. import main
from ..logic import slugify
from ..main import EmailConfigRequest, ReportDraft, ReportRequest, active_tenant_id

router = APIRouter()


def _decisions_for_report(limit: int = 1000) -> List[Dict[str, Any]]:
    return main.storage.list_decisions(tenant_id=active_tenant_id(), limit=limit)


def _sync_report_schedule(report: Dict[str, Any]) -> None:
    """Register/refresh this report's delivery job (best-effort; no-op if the
    scheduler isn't running, e.g. under tests)."""
    try:
        from ..scheduler import schedule_report_job

        schedule_report_job(main.storage, {**report, "tenant_id": active_tenant_id()})
    except Exception:  # pragma: no cover - scheduling is best-effort
        pass


def _send_report_and_record(report_id: str, tenant_id: str, report: Dict[str, Any], result: Dict[str, Any], recipients: List[str]) -> None:
    """Background task: the blocking SMTP send + last_run write, off the request path."""
    from ..scheduler import deliver_report_result

    delivery = deliver_report_result(main.storage, tenant_id, report, result, recipients)
    main.storage.update_report(report_id, {"last_run": {
        "generated_at": result["generated_at"], "row_count": result["row_count"],
        "truncated": result.get("truncated", False), "delivery": delivery,
    }}, tenant_id=tenant_id)


@router.get("/api/v1/reports")
def list_reports() -> List[Dict[str, Any]]:
    return main.storage.list_reports()


@router.get("/api/v1/reports/column-suggestions")
def report_column_suggestions() -> Dict[str, Any]:
    from ..reports import suggest_columns

    return {"columns": suggest_columns(_decisions_for_report(limit=100))}


@router.get("/api/v1/reports/email-config")
def get_report_email_config() -> Dict[str, Any]:
    return main.storage.get_email_config_masked()


@router.put("/api/v1/reports/email-config")
def put_report_email_config(request: EmailConfigRequest) -> Dict[str, Any]:
    return main.storage.set_email_config(request.model_dump(exclude_none=True))


@router.post("/api/v1/reports/preview")
def preview_report(draft: ReportDraft) -> Dict[str, Any]:
    from ..scheduler import generate_report_for

    result = generate_report_for(main.storage, draft.model_dump(), active_tenant_id())
    result.pop("csv", None)  # preview is JSON rows; CSV is a separate download
    return result


@router.get("/api/v1/reports/{report_id}")
def get_report(report_id: str) -> Dict[str, Any]:
    report = main.storage.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    return report


@router.post("/api/v1/reports")
def create_report(request: ReportRequest) -> Dict[str, Any]:
    report_id = slugify(request.name)
    existing = {r["id"] for r in main.storage.list_reports()}
    if report_id in existing:
        report_id = f"{report_id}_{uuid.uuid4().hex[:6]}"
    data = request.model_dump()
    data["id"] = report_id
    created = main.storage.create_report(data)
    _sync_report_schedule(created)
    return created


@router.put("/api/v1/reports/{report_id}")
def update_report(report_id: str, request: ReportRequest) -> Dict[str, Any]:
    updated = main.storage.update_report(report_id, request.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="Report not found.")
    _sync_report_schedule(updated)
    return updated


@router.delete("/api/v1/reports/{report_id}")
def delete_report(report_id: str) -> Dict[str, Any]:
    tenant_id = active_tenant_id()
    if not main.storage.delete_report(report_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Report not found.")
    try:  # stop its scheduled delivery job so it doesn't keep firing
        from ..scheduler import unschedule_report_job

        unschedule_report_job(tenant_id, report_id)
    except Exception:  # pragma: no cover - best effort
        pass
    return {"deleted": True, "id": report_id}


@router.post("/api/v1/reports/{report_id}/run")
def run_report(report_id: str) -> Dict[str, Any]:
    from ..scheduler import generate_report_for

    report = main.storage.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    result = generate_report_for(main.storage, report, active_tenant_id())
    result.pop("csv", None)
    return result


@router.get("/api/v1/reports/{report_id}/export.csv")
def export_report_csv(report_id: str) -> Response:
    from ..scheduler import generate_report_for

    report = main.storage.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    result = generate_report_for(main.storage, report, active_tenant_id())
    return Response(
        content=result["csv"],
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{report_id}.csv"'},
    )


@router.post("/api/v1/reports/{report_id}/send")
def send_report_now(report_id: str, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """Generate the report now and email it in the background (SMTP never blocks the
    request). Delivery is durable: a failed/unconfigured send is queued + retried."""
    from ..scheduler import generate_report_for

    tenant_id = active_tenant_id()
    report = main.storage.get_report(report_id, tenant_id=tenant_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    result = generate_report_for(main.storage, report, tenant_id)
    recipients = (report.get("schedule") or {}).get("recipients") or []
    background_tasks.add_task(_send_report_and_record, report_id, tenant_id, report, result, recipients)
    return {"row_count": result["row_count"], "truncated": result.get("truncated", False), "status": "sending"}
