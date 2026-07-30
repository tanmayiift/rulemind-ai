"""Email delivery for scheduled reports.

`send_report_email` makes a single SMTP attempt and reports the outcome; durability
(retry / never-lost) is the caller's job via the DB-backed outbox
(storage.enqueue_email + the leader-gated retry job in the scheduler). The SMTP
factory is module-level so tests can substitute a fake transport (no real network).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

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


def build_message(email_config: Optional[Dict[str, Any]], recipients: List[str], subject: str, body: str,
                  csv_content: str, csv_filename: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (email_config or {}).get("from_addr", "reports@rulemind.local")
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    message.add_attachment(csv_content.encode("utf-8"), maintype="text", subtype="csv", filename=csv_filename)
    return message


def send_text_email(
    email_config: Optional[Dict[str, Any]],
    recipients: List[str],
    subject: str,
    body: str,
) -> Dict[str, Any]:
    """One SMTP attempt for a plain-text message (no attachment) — used for login
    OTP / transactional email. Returns a delivery record; when SMTP is unconfigured
    the caller falls back to surfacing the code in a dev/debug channel."""
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return {"delivered": False, "transport": "none", "error": "no recipients"}
    if not is_configured(email_config):
        return {"delivered": False, "transport": "unconfigured", "recipients": recipients}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (email_config or {}).get("from_addr", "no-reply@rulemind.local")
    message["To"] = ", ".join(recipients)
    message.set_content(body)
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
        return {"delivered": False, "transport": "failed", "recipients": recipients, "error": str(exc)}


def send_report_email(
    email_config: Optional[Dict[str, Any]],
    recipients: List[str],
    subject: str,
    body: str,
    csv_content: str,
    csv_filename: str = "report.csv",
) -> Dict[str, Any]:
    """One SMTP attempt. Returns a delivery record; the caller persists a failed /
    unconfigured send to the durable outbox and a retry job drains it."""
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return {"delivered": False, "transport": "none", "error": "no recipients"}
    if not is_configured(email_config):
        return {"delivered": False, "transport": "unconfigured", "recipients": recipients,
                "note": "No SMTP configured — report queued; configure email to deliver."}

    message = build_message(email_config, recipients, subject, body, csv_content, csv_filename)
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
        return {"delivered": False, "transport": "failed", "recipients": recipients, "error": str(exc)}
