"""Decision payloads are encrypted at rest + PII redaction is configurable.

Financial/KYC/health/any-PII inputs must not sit in the DB as plaintext. The stored
payload_preview + computed_variables are Fernet-encrypted (keyed on RULEMIND_CONFIG_KEY) and
transparently decrypted on read; legacy plaintext rows still read. Redaction key-set is
extendable per deployment/domain.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app import storage as storage_mod  # noqa: E402
from app.logic import redact_payload  # noqa: E402
from app.models import Decision  # noqa: E402
from sqlalchemy import select  # noqa: E402


class DecisionEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id
        self.policy = self.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]

    def _raw_preview(self, decision_id):
        with self.storage.connect() as session:
            row = session.scalar(select(Decision).where(Decision.id == decision_id))
            return row.payload_preview

    def test_payload_is_ciphertext_at_rest_but_plaintext_on_read(self):
        saved = self.storage.add_decision(
            {"policy_id": self.policy["id"], "outcome": "approve", "payload": {"amount": 5000, "account": "acc-123"}},
            tenant_id=self.tenant_id,
        )
        # On the wire out of storage: decrypted.
        self.assertEqual(saved["payload"]["amount"], 5000)
        # At rest in the column: a Fernet token string, not the plaintext dict.
        raw = self._raw_preview(saved["id"])
        self.assertIsInstance(raw, str)
        self.assertNotIn("5000", raw)
        self.assertTrue(raw.startswith("gAAAAA"), "Fernet token expected")
        # Reading it back through the normal path decrypts.
        again = self.storage.get_decision(saved["id"], tenant_id=self.tenant_id) if hasattr(self.storage, "get_decision") else None
        if again is not None:
            self.assertEqual(again["payload"]["amount"], 5000)

    def test_legacy_plaintext_still_reads(self):
        # Pre-encryption rows stored the preview as a plain dict (or a JSON string); both must
        # still decode after encryption is turned on.
        self.assertEqual(storage_mod.decrypt_decision_field({"amount": 42}), {"amount": 42})
        self.assertEqual(storage_mod.decrypt_decision_field('{"amount": 42}'), {"amount": 42})
        self.assertIsNone(storage_mod.decrypt_decision_field(None))

    def test_disable_flag_stores_plaintext(self):
        saved_flag = os.environ.get("DECISION_ENCRYPT_AT_REST")
        os.environ["DECISION_ENCRYPT_AT_REST"] = "0"
        try:
            self.assertFalse(storage_mod.decision_encryption_enabled())
            self.assertEqual(storage_mod.encrypt_decision_field({"a": 1}), {"a": 1})
        finally:
            if saved_flag is None:
                os.environ.pop("DECISION_ENCRYPT_AT_REST", None)
            else:
                os.environ["DECISION_ENCRYPT_AT_REST"] = saved_flag

    def test_pii_redaction_builtin_and_configurable(self):
        # Built-in keys (email) redacted.
        red = redact_payload({"email": "a@b.com", "amount": 10})
        self.assertEqual(red["email"], "***")
        self.assertEqual(red["amount"], 10)
        # Extra keys via env (domain-agnostic: patient_id, card_no, ...).
        saved = os.environ.get("RULEMIND_PII_REDACT_KEYS")
        os.environ["RULEMIND_PII_REDACT_KEYS"] = "patient_id,card_no"
        try:
            red2 = redact_payload({"patient_id": "P1", "card_no": "4111", "amount": 10})
            self.assertEqual(red2["patient_id"], "***")
            self.assertEqual(red2["card_no"], "***")
            self.assertEqual(red2["amount"], 10)
        finally:
            if saved is None:
                os.environ.pop("RULEMIND_PII_REDACT_KEYS", None)
            else:
                os.environ["RULEMIND_PII_REDACT_KEYS"] = saved


if __name__ == "__main__":
    unittest.main()
