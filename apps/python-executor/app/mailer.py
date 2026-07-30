"""Email delivery for scheduled reports.

Sends via SMTP when the workspace has configured an email server; otherwise the
message is appended to an in-process OUTBOX so a scheduled report is never lost
and the delivery is inspectable in dev/CI. The SMTP factory is module-level so
tests can substitute a fake transport (no real network).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

# Dev/CI fallback + inspection surface when SMTP isn't configured.
OUTBOX: List[Dict[str, Any]] = []

# Overridable in tests: returns an object with .send_message()/.quit() (smtplib API).
_SMTP_FACTORY: Optional[Callable[[Dict[str, Any]], Any]] = None


def _default_smtp(config: Dict[str, Any]):  # pragma: no cover - real network
    host = config["host"]
    port = int(config.get("port", 587))
    if config.get("use_ssl"):
        client: Any = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        client = smtplib.SMTP(host, port, timeout=15)
        if config.get("use_tls", True):
            client.starttls()
    if config.get("username"):
        client.login(config["username"], config.get("password", ""))
    return client


def is_configured(config: Optional[Dict[str, Any]]) -> bool:
    return bool(config and config.get("host") and config.get("from_addr"))


def send_report_email(
    email_config: Optional[Dict[str, Any]],
    recipients: List[str],
    subject: str,
    body: str,
    csv_content: str,
    csv_filename: str = "report.csv",
) -> Dict[str, Any]:
    """Deliver a report by email (CSV attached). Returns a delivery record."""
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return {"delivered": False, "transport": "none", "error": "no recipients"}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (email_config or {}).get("from_addr", "reports@rulemind.local")
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    message.add_attachment(csv_content.encode("utf-8"), maintype="text", subtype="csv", filename=csv_filename)

    if not is_configured(email_config):
        OUTBOX.append({"to": recipients, "subject": subject, "body": body, "csv": csv_content})
        return {"delivered": False, "transport": "outbox", "recipients": recipients,
                "note": "No SMTP configured — report generated and stored; configure email to deliver."}

    factory = _SMTP_FACTORY or _default_smtp
    try:
        client = factory(email_config)
        try:
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:  # pragma: no cover
                pass
        return {"delivered": True, "transport": "smtp", "recipients": recipients}
    except Exception as exc:
        OUTBOX.append({"to": recipients, "subject": subject, "body": body, "csv": csv_content, "error": str(exc)})
        return {"delivered": False, "transport": "outbox", "recipients": recipients, "error": str(exc)}
