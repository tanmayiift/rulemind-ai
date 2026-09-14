"""Encrypting payload_preview while leaving the trace in the clear protects nothing (Tier 6.2).

`add_decision` encrypted two of the five JSON columns on a decision row:

    payload_preview       encrypt_decision_field(...)    <- encrypted
    computed_variables    encrypt_decision_field(...)    <- encrypted
    rule_results          copy.deepcopy(...)             <- PLAINTEXT
    scorecard_result      copy.deepcopy(...)             <- PLAINTEXT
    trace                 copy.deepcopy(...)             <- PLAINTEXT

The trace is not metadata. It records every evaluated step, and each step carries the INPUT
VALUES it was evaluated against -- the same PAN, phone number, income and bureau score that
`payload_preview` was encrypted to protect. They sit one column over in the same row, so
anyone reading the table (a replica, a nightly dump, a support export, a stolen backup) gets
the payload back in full without ever touching the ciphertext.

`redact_payload` does not close the gap either: it is applied ONLY to the payload, never to
the trace. So the deployment-configurable PII redaction that operators are told protects them
covered the one column that was already encrypted, and skipped the one that was not.

`add_decisions_batch` -- the SDK's on-device sync path, which ingests decisions in bulk --
mirrored the same split.

Separately, `payload_hash` was an UNSALTED, UNKEYED sha256 over the canonical JSON of the
payload, and `payload.get("payload_hash") or ...` let the CALLER supply it. Decision payloads
are low-entropy PII by nature (a 10-digit phone number is 10^10 candidates; a date of birth is
~40,000), so an unkeyed digest of one is reversible by enumeration in seconds. The keyed HMAC
makes that require RULEMIND_CONFIG_KEY, which is the same property the encrypted columns
already rely on.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from app import storage as storage_mod  # noqa: E402
from app.models import Decision  # noqa: E402
from app.storage import Storage  # noqa: E402
from sqlalchemy import select  # noqa: E402

# The PII that must not be recoverable from the raw row. Deliberately appears in the payload AND
# in the trace, because that duplication is the whole bug.
PAN = "ABCDE1234F"
PHONE = "9876543210"

TRACE = [
    {"step": {"type": "rule", "ref_id": "r1"},
     "result": {"passed": False, "inputs": {"pan": PAN, "phone": PHONE, "income": 42000}}},
    {"step": {"type": "outcome"}, "result": {"outcome": "reject"}},
]
RULE_RESULTS = [{"rule_id": "r1", "passed": False, "evaluated": {"pan": PAN}}]
SCORECARD = {"total": 610, "factors": [{"variable": "phone", "value": PHONE, "points": 0}]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "deccrypt.db"))
        self.tenant_id = self.storage.default_tenant_id
        self.policy = self.storage.list_policies(tenant_id=self.tenant_id)[0]

    def tearDown(self):
        self.tempdir.cleanup()

    def _decision_payload(self, **overrides):
        payload = {
            "policy_id": self.policy["id"],
            "outcome": "reject",
            "payload": {"pan": PAN, "phone": PHONE},
            "computed_variables": {"income": 42000},
            "rule_results": RULE_RESULTS,
            "scorecard_result": SCORECARD,
            "trace": TRACE,
        }
        payload.update(overrides)
        return payload

    def _raw_row(self, decision_id):
        """The row exactly as it sits on disk — what a DB dump or a replica would hand over."""
        with self.storage.connect() as session:
            row = session.scalar(select(Decision).where(Decision.id == decision_id))
            return {column: getattr(row, column) for column in
                    ("payload_preview", "computed_variables", "rule_results",
                     "scorecard_result", "trace", "payload_hash")}


class AtRestTests(_Base):
    def test_all_five_json_columns_are_ciphertext_at_rest(self):
        saved = self.storage.add_decision(self._decision_payload(), tenant_id=self.tenant_id)
        raw = self._raw_row(saved["id"])
        for column in ("payload_preview", "computed_variables", "rule_results",
                       "scorecard_result", "trace"):
            value = raw[column]
            self.assertIsInstance(value, str, "{0} is not a Fernet token".format(column))
            self.assertTrue(value.startswith(storage_mod._FERNET_PREFIX),
                            "{0} is not encrypted".format(column))

    def test_the_pii_is_not_recoverable_from_the_raw_row(self):
        # The assertion that would have failed before: the PAN and phone were readable straight
        # out of `trace`, `rule_results` and `scorecard_result` even though `payload_preview`
        # was ciphertext.
        saved = self.storage.add_decision(self._decision_payload(), tenant_id=self.tenant_id)
        blob = repr(self._raw_row(saved["id"]))
        self.assertNotIn(PAN, blob)
        self.assertNotIn(PHONE, blob)
        self.assertNotIn("42000", blob)

    def test_read_path_returns_all_three_decrypted(self):
        saved = self.storage.add_decision(self._decision_payload(), tenant_id=self.tenant_id)
        for record in (saved, self.storage.get_decision(saved["id"], tenant_id=self.tenant_id)):
            self.assertEqual(record["trace"], TRACE)
            self.assertEqual(record["rule_results"], RULE_RESULTS)
            self.assertEqual(record["scorecard_result"], SCORECARD)
            self.assertEqual(record["payload"]["pan"], PAN)

    def test_list_and_stream_read_paths_decrypt_too(self):
        # Everything funnels through _decision_to_dict, but assert it rather than trust it:
        # a second read path that forgot to decrypt would serve base64 to the dashboard.
        self.storage.add_decision(self._decision_payload(), tenant_id=self.tenant_id)
        listed = self.storage.list_decisions(tenant_id=self.tenant_id)[0]
        self.assertEqual(listed["trace"], TRACE)
        self.assertEqual(listed["scorecard_result"]["total"], 610)

    def test_batch_ingest_path_encrypts_the_same_three_columns(self):
        # add_decisions_batch is the on-device SDK sync path and had the identical split.
        self.storage.add_decisions_batch(
            [self._decision_payload(id="dev-1")], tenant_id=self.tenant_id)
        listed = self.storage.list_decisions(tenant_id=self.tenant_id)
        record = next(item for item in listed if item["client_id"] == "dev-1")
        raw = self._raw_row(record["id"])
        for column in ("rule_results", "scorecard_result", "trace"):
            self.assertTrue(str(raw[column]).startswith(storage_mod._FERNET_PREFIX), column)
        self.assertEqual(record["trace"], TRACE)
        self.assertEqual(record["rule_results"], RULE_RESULTS)

    def test_null_scorecard_stays_null(self):
        # scorecard_result is nullable and most decisions have none. Encrypting None into a
        # token would turn "no scorecard ran" into "a scorecard ran and returned null".
        saved = self.storage.add_decision(
            self._decision_payload(scorecard_result=None), tenant_id=self.tenant_id)
        self.assertIsNone(self._raw_row(saved["id"])["scorecard_result"])
        self.assertIsNone(saved["scorecard_result"])


class LegacyPlaintextRowTests(_Base):
    """Rows written before this change hold real JSON lists/dicts in those columns. They must
    keep reading — a migration that makes historical decisions unreadable is a compliance
    problem, since the audit trail is what this product exists to produce."""

    def test_pre_change_plaintext_rows_still_read(self):
        saved = self.storage.add_decision(self._decision_payload(), tenant_id=self.tenant_id)
        # Rewrite the row the way the old code would have: bare JSON, no encryption.
        with self.storage.connect() as session:
            row = session.scalar(select(Decision).where(Decision.id == saved["id"]))
            row.rule_results = RULE_RESULTS
            row.scorecard_result = SCORECARD
            row.trace = TRACE
        record = self.storage.get_decision(saved["id"], tenant_id=self.tenant_id)
        self.assertEqual(record["trace"], TRACE)
        self.assertEqual(record["rule_results"], RULE_RESULTS)
        self.assertEqual(record["scorecard_result"], SCORECARD)

    def test_encryption_toggle_off_stores_plaintext_for_a_migration(self):
        saved_flag = os.environ.get("DECISION_ENCRYPT_AT_REST")
        os.environ["DECISION_ENCRYPT_AT_REST"] = "0"
        try:
            saved = self.storage.add_decision(self._decision_payload(), tenant_id=self.tenant_id)
            raw = self._raw_row(saved["id"])
            self.assertEqual(raw["trace"], TRACE)
            self.assertEqual(raw["rule_results"], RULE_RESULTS)
        finally:
            if saved_flag is None:
                os.environ.pop("DECISION_ENCRYPT_AT_REST", None)
            else:
                os.environ["DECISION_ENCRYPT_AT_REST"] = saved_flag


class PayloadHashTests(_Base):
    def test_hash_is_keyed_not_a_bare_sha256(self):
        # The exact attack: hash a candidate payload and compare. With a bare sha256 this
        # matched; with an HMAC it cannot without RULEMIND_CONFIG_KEY.
        import hashlib

        from app.logic import json_dumps

        payload = {"pan": PAN, "phone": PHONE}
        saved = self.storage.add_decision(
            self._decision_payload(payload=payload), tenant_id=self.tenant_id)
        guessed = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
        self.assertNotEqual(saved["payload_hash"], guessed)

    def test_hash_changes_with_the_config_key(self):
        # Proves the key is actually an input, i.e. the previous assertion is not passing for
        # some incidental formatting reason.
        payload = {"pan": PAN}
        first = storage_mod.decision_payload_hash(payload)
        previous = os.environ.get("RULEMIND_CONFIG_KEY")
        os.environ["RULEMIND_CONFIG_KEY"] = "a-completely-different-master-key"
        try:
            self.assertNotEqual(storage_mod.decision_payload_hash(payload), first)
        finally:
            if previous is None:
                os.environ.pop("RULEMIND_CONFIG_KEY", None)
            else:
                os.environ["RULEMIND_CONFIG_KEY"] = previous
        # Same key -> same hash, so it is still usable for correlation.
        self.assertEqual(storage_mod.decision_payload_hash(payload), first)

    def test_caller_supplied_hash_is_ignored(self):
        # `payload.get("payload_hash") or ...` let any SDK client stamp its decision with an
        # arbitrary fingerprint — including one deliberately collided onto another decision's.
        saved = self.storage.add_decision(
            self._decision_payload(payload_hash="attacker-chosen-value"), tenant_id=self.tenant_id)
        self.assertNotEqual(saved["payload_hash"], "attacker-chosen-value")
        self.assertEqual(saved["payload_hash"],
                         storage_mod.decision_payload_hash({"pan": PAN, "phone": PHONE}))

    def test_batch_path_also_ignores_a_caller_supplied_hash(self):
        self.storage.add_decisions_batch(
            [self._decision_payload(id="dev-2", payload_hash="attacker-chosen-value")],
            tenant_id=self.tenant_id)
        record = next(item for item in self.storage.list_decisions(tenant_id=self.tenant_id)
                      if item["client_id"] == "dev-2")
        self.assertNotEqual(record["payload_hash"], "attacker-chosen-value")


if __name__ == "__main__":
    unittest.main()
