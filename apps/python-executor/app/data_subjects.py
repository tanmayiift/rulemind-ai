"""Data-subject access and erasure (plan 6.7).

THE GAP. Nothing existed: no way to answer "what do you hold about this person" (GDPR Art. 15, the
DPDP Act's right to information) and no way to act on "erase it" (Art. 17 / DPDP s.12). For an EU or
Indian lender that is a procurement blocker, not a feature request.

THE TENSION, AND HOW IT IS RESOLVED. A lender generally must NOT delete a credit decision outright:
adverse-action records have their own retention obligations (e.g. ECOA/Reg B's 25 months; RBI's
record-keeping for KYC). So erasure here is REDACTION of the personal data, not deletion of the
record:

  kept     the decision's id, outcome, policy and policy version, bundle, times, and its
           adverse-action reason codes (code, reason, variable, threshold -- the facts a decline
           notice needs), so the institution can still show it decided lawfully;
  removed  the payload, computed variables, trace, scorecard detail, and each reason code's
           `value` -- the applicant's own number, also stripped from the code's sentence;
  replaced the subject id, by a keyed pseudonym (stable, so erasing twice is idempotent and
           decision counts still add up, but not reversible without the install's key).

Whether a given request must be honoured, or is overridden by a retention duty, is the operator's
legal call. This module gives them an accurate tool and an accurate receipt -- including the stores
it CANNOT reach -- and records the erasure in the tamper-evident audit trail under the pseudonym,
never the raw id (an audit log that names the erased subject would undo the erasure).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, List, Optional

from .logic import adverse_reason_codes

# Stores outside this database. Listed on every receipt, because a receipt that only reports what
# it did would read as "all of it".
NOT_COVERED = [
    "Decision archives already written by the retention job to DECISION_ARCHIVE_SINK (object storage) "
    "-- erase those objects with the storage provider.",
    "Database backups -- they age out on your backup retention schedule.",
    "Decision events already relayed to Kafka (KAFKA_BOOTSTRAP_SERVERS), and webhook events already "
    "delivered to their receivers -- those copies are the receivers'.",
    "The on-disk decision WAL, if a decision about this subject is still waiting to be replayed after "
    "a database outage.",
    "Data your connectors sent to their providers, and prompts sent to an AI provider, if enabled.",
]


def pseudonym(subject_id: str, tenant_id: str) -> str:
    """A stable, keyed stand-in for an erased subject id. Keyed with the install's config key (the
    same material that encrypts at rest) so it cannot be reversed by hashing candidate ids."""
    from .storage import _config_key_secrets

    primary, _ = _config_key_secrets()
    # A labelled subkey, the pattern decision_payload_hash uses, so this never shares key material
    # with anything else derived from the config key.
    subkey = hmac.new(b"rulemind.data-subject-pseudonym.v1", primary.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(subkey, "data-subject:{0}:{1}".format(tenant_id, subject_id).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return "erased:" + digest[:24]


def export_subject(storage: Any, subject_id: str, tenant_id: str) -> Dict[str, Any]:
    """Everything this database holds about one subject, for an access request."""
    decisions = storage.list_subject_decisions(subject_id, tenant_id=tenant_id)
    executions = storage.list_subject_executions(subject_id, tenant_id=tenant_id)
    return {
        "subject_id": subject_id,
        "decisions": decisions,
        "executions": executions,
        "counts": {"decisions": len(decisions), "executions": len(executions)},
        "not_covered": NOT_COVERED,
    }


def _preserved_codes(decision: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The reason codes a decline notice needs, without the applicant's own values.

    The applicant's number is in TWO places: `value`, and inside the human `reason`
    ("... (required 650, actual 612)."). Dropping the key alone would have left it in the sentence,
    so the reason is rebuilt from the threshold only."""
    from .logic import _OPERATOR_PHRASE

    kept = []
    for code in adverse_reason_codes(decision.get("rule_results") or [], limit=10):
        phrase = _OPERATOR_PHRASE.get(str(code.get("operator")), "failed the check")
        kept.append({
            "code": code.get("code"),
            "variable_id": code.get("variable_id"),
            "variable_name": code.get("variable_name"),
            "operator": code.get("operator"),
            "threshold": code.get("threshold"),
            "reason": "{0} {1} (required {2}).".format(code.get("variable_name"), phrase, code.get("threshold")),
        })
    return kept


def erase_subject(storage: Any, subject_id: str, tenant_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Redact one subject's personal data. Returns a receipt: counts per store, and what was not
    reachable from here."""
    alias = pseudonym(subject_id, tenant_id)
    decisions = storage.list_subject_decisions(subject_id, tenant_id=tenant_id)
    preserved = {decision["id"]: _preserved_codes(decision) for decision in decisions}
    counts = storage.erase_data_subject(subject_id, alias, preserved, tenant_id=tenant_id)
    storage.add_audit_event({
        "event_type": "data_subject_erased",
        "entity_type": "data_subject",
        "entity_id": alias,
        "detail": "Personal data of a data subject erased ({0} decision(s)).".format(counts["decisions"]),
        "metadata": {"counts": counts, "reason": reason or ""},
    }, tenant_id=tenant_id)
    return {"subject": alias, "erased": counts, "not_covered": NOT_COVERED}
