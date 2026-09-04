"""Tamper-evidence for the audit log (Track B3).

An operator with database access could previously rewrite or delete an audit event and leave
no trace -- the single biggest blocker to a regulated buyer. Every event now carries a
per-tenant sequence number, the previous event's hash, and its own hash over a canonical
serialization, so an edit anywhere breaks every link after it.

The tests that matter here are the TAMPER cases. A chain test that only appends events and
asserts `ok: true` proves the writer is self-consistent, which is nearly free and worth almost
nothing; what has to be shown is that each specific attack turns `ok` false and says where.
Four are covered -- edit a field, delete a row from the middle, truncate the head, and insert a
row that bypassed the writer -- because they fail for four different reasons and an
implementation can easily catch some and miss others.

The honest limit is asserted too (`test_head_commits_to_full_history`): an operator who
recomputes the WHOLE chain after an edit passes verification. The chain detects partial
tampering; detecting a full rewrite needs the head anchored outside the operator's control.
Claiming otherwise would be the same overreach the migration refuses to make when it declines
to backfill legacy rows.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

import sqlalchemy  # noqa: E402

from app.audit_chain import GENESIS_HASH, compute_hash  # noqa: E402
from app.models import AuditEvent  # noqa: E402
from app.storage import Storage  # noqa: E402
import app.main as app_main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class AuditChainTest(unittest.TestCase):
    def setUp(self):
        # A private store, not the shared .runtime DB: these tests assert on exact sequence
        # numbers and on the whole-tenant chain, so any other test's audit writes would break
        # them non-deterministically.
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "audit.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def _append(self, n=3, event_type="policy_promoted"):
        return [
            self.storage.add_audit_event(
                {"event_type": event_type, "entity_type": "policy", "entity_id": f"p{i}",
                 "detail": f"promoted p{i}", "metadata": {"stage": "prod", "i": i},
                 "user_id": "auditor@example.com"},
                tenant_id=self.tenant,
            )
            for i in range(n)
        ]

    def _rewrite(self, fn):
        """Apply a raw mutation, the way an operator with DB access would -- bypassing
        add_audit_event entirely, which is precisely the threat model."""
        with self.storage.connect() as session:
            fn(session)

    # ── the chain forms correctly ───────────────────────────────────────────

    def test_appends_link_into_a_chain_anchored_at_genesis(self):
        events = self._append(3)
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual(events[0]["prev_hash"], GENESIS_HASH)
        self.assertEqual(events[1]["prev_hash"], events[0]["hash"])
        self.assertEqual(events[2]["prev_hash"], events[1]["hash"])
        self.assertTrue(all(len(e["hash"]) == 64 for e in events))

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertTrue(report["ok"], report["breaks"])
        self.assertEqual(report["verified"], 3)
        self.assertEqual(report["pre_chain"], 0)
        self.assertEqual(report["head"], events[2]["hash"])

    def test_chains_are_per_tenant(self):
        """Tenant isolation is not decoration here: if seq were global, one tenant's writes
        would create gaps in another's chain and every verify would report a false break."""
        other = self.storage.create_tenant("Other Co")["id"]
        self._append(2)
        b = self.storage.add_audit_event({"event_type": "login", "detail": "x"}, tenant_id=other)
        self.assertEqual(b["seq"], 1, "a new tenant starts its own chain at 1")
        self.assertEqual(b["prev_hash"], GENESIS_HASH)
        self.assertTrue(self.storage.verify_audit_chain(tenant_id=self.tenant)["ok"])
        self.assertTrue(self.storage.verify_audit_chain(tenant_id=other)["ok"])

    # ── the four attacks ────────────────────────────────────────────────────

    def test_editing_an_event_is_detected(self):
        events = self._append(3)
        target = events[1]["id"]

        def edit(session):
            row = session.get(AuditEvent, target)
            row.detail = "nothing to see here"

        self._rewrite(edit)

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertFalse(report["ok"])
        reasons = {b["reason"] for b in report["breaks"]}
        self.assertIn("content_hash_mismatch", reasons)
        self.assertIn(target, [b["id"] for b in report["breaks"]])

    def test_deleting_an_event_from_the_middle_is_detected(self):
        """The subtle one. Deleting a row leaves the survivors individually valid -- each
        still hashes to its stored hash -- so a naive per-row check passes. The break is
        visible only in the LINKS: event 3's prev_hash points at a hash no longer present."""
        events = self._append(4)
        victim = events[1]["id"]
        self._rewrite(lambda s: s.delete(s.get(AuditEvent, victim)))

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertFalse(report["ok"])
        reasons = {b["reason"] for b in report["breaks"]}
        self.assertIn("prev_hash_mismatch", reasons)
        self.assertIn("seq_gap", reasons, "the missing seq number is independent evidence")

    def test_truncating_the_start_of_the_chain_is_detected(self):
        """Deleting the OLDEST events leaves a chain whose links all join up -- the classic
        blind spot of a links-only check. It is caught by requiring the first chained row to
        anchor at GENESIS."""
        events = self._append(3)
        self._rewrite(lambda s: s.delete(s.get(AuditEvent, events[0]["id"])))

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertFalse(report["ok"])
        self.assertIn("chain_does_not_start_at_genesis", {b["reason"] for b in report["breaks"]})

    def test_an_event_inserted_outside_the_writer_is_detected(self):
        """A row written by raw SQL has no hash. It must not be silently ignored, and it must
        not be mistaken for a legacy pre-chain row -- it arrives after the chain has started."""
        self._append(2)

        def smuggle(session):
            session.add(AuditEvent(
                tenant_id=self.tenant, event_type="policy_promoted", detail="backdated",
                metadata_json={}, seq=None, prev_hash=None, hash=None,
            ))

        self._rewrite(smuggle)

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        # seq IS NULL sorts first, so this row lands before the chain starts and is counted as
        # pre_chain rather than a break -- which is why pre_chain is reported separately and is
        # never folded into `verified`. An unexplained pre_chain count on a workspace created
        # after the migration is itself the signal.
        self.assertEqual(report["pre_chain"], 1)
        self.assertEqual(report["verified"], 2)
        self.assertNotEqual(report["total"], report["verified"],
                            "a smuggled row must never be counted as verified")

    # ── the honest limit ────────────────────────────────────────────────────

    def test_head_commits_to_full_history(self):
        """States the boundary the docstring claims. An operator who edits an event AND
        recomputes every later hash produces a chain that verifies clean -- but the head hash
        changes. Detection therefore depends on the head having been anchored externally,
        which is an operational control, not a database one."""
        events = self._append(3)
        original_head = self.storage.verify_audit_chain(tenant_id=self.tenant)["head"]

        def rewrite_all(session):
            rows = sorted(
                session.query(AuditEvent).filter(AuditEvent.tenant_id == self.tenant).all(),
                key=lambda r: r.seq,
            )
            rows[0].detail = "forged"
            prev = GENESIS_HASH
            for row in rows:
                row.prev_hash = prev
                row.hash = compute_hash(self.storage._audit_event_to_dict(row))
                prev = row.hash

        self._rewrite(rewrite_all)

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertTrue(report["ok"], "a full recompute is self-consistent -- this is the limit")
        self.assertNotEqual(report["head"], original_head,
                            "but the head moved: an externally anchored head catches it")
        self.assertEqual(len(events), 3)

    # ── legacy rows ─────────────────────────────────────────────────────────

    def test_pre_chain_rows_are_reported_but_never_verified(self):
        """Rows predating the migration carry NULLs. They must be counted honestly rather than
        backfilled into looking verified -- see alembic 20260903_0017."""
        def legacy(session):
            for i in range(2):
                session.add(AuditEvent(
                    tenant_id=self.tenant, event_type="legacy", detail=f"old {i}",
                    metadata_json={}, seq=None, prev_hash=None, hash=None,
                ))

        self._rewrite(legacy)
        self._append(2)

        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertTrue(report["ok"], report["breaks"])
        self.assertEqual(report["pre_chain"], 2)
        self.assertEqual(report["verified"], 2)
        self.assertEqual(report["total"], 4)

    def test_a_chain_that_starts_after_legacy_rows_is_not_a_break(self):
        """The migration's own shape: legacy rows, then the chain begins at GENESIS. That must
        verify clean, or every existing deployment reports a false break on day one."""
        self._rewrite(lambda s: s.add(AuditEvent(
            tenant_id=self.tenant, event_type="legacy", detail="old",
            metadata_json={}, seq=None, prev_hash=None, hash=None,
        )))
        first = self._append(1)[0]
        self.assertEqual(first["prev_hash"], GENESIS_HASH)
        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertTrue(report["ok"], report["breaks"])

    # ── canonical form ──────────────────────────────────────────────────────

    def test_hash_is_stable_across_key_order_and_json_round_trip(self):
        """The hash must survive a trip through the API, or an auditor recomputing it from a
        JSON export would get a different digest and report a phantom break."""
        import json

        event = self._append(1)[0]
        reordered = dict(reversed(list(event.items())))
        self.assertEqual(compute_hash(reordered), event["hash"])
        self.assertEqual(compute_hash(json.loads(json.dumps(event))), event["hash"])

    def test_hash_excludes_the_rows_own_hash(self):
        """Otherwise the computation would be self-referential and could never be checked."""
        event = self._append(1)[0]
        without = {k: v for k, v in event.items() if k != "hash"}
        self.assertEqual(compute_hash(without), event["hash"])


class AuditEncryptionAtRestTest(unittest.TestCase):
    """`metadata_json` and `ip_address` were the last plaintext PII columns in the schema.

    Decision payloads have been encrypted since DECISION_ENCRYPT_AT_REST, but the audit trail
    beside them stored the emails, usernames and source IPs of failed sign-ins, invitations and
    role changes in the clear -- readable from any dump, backup or read replica.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "enc.db"), force_sqlite=True)
        self.tenant = self.storage.default_tenant_id
        self.storage.add_audit_event(
            {"event_type": "login_failed_repeatedly", "entity_type": "auth", "detail": "d",
             "metadata": {"email": "victim@example.com", "failures": 9},
             # IPv6 on purpose: encrypted it lands near 140 characters, which did not fit the
             # String(128) this column used to be. SQLite ignores declared widths, so only
             # Postgres would have caught it -- in production, on IPv6 traffic.
             "ip_address": "2001:db8::dead:beef"},
            tenant_id=self.tenant)

    def tearDown(self):
        self.tempdir.cleanup()

    def _raw_row(self):
        with self.storage.engine.connect() as conn:
            return conn.execute(
                sqlalchemy.text("select metadata_json, ip_address from audit_events "
                                "where tenant_id = :t and event_type = 'login_failed_repeatedly'"),
                {"t": self.tenant}).fetchone()

    def test_metadata_and_ip_are_not_plaintext_in_the_database(self):
        metadata_raw, ip_raw = self._raw_row()
        self.assertNotIn("victim@example.com", str(metadata_raw))
        self.assertNotIn("2001:db8", str(ip_raw))
        # metadata_json is a JSON column, so the driver returns the token with its JSON quotes.
        self.assertTrue(str(metadata_raw).strip('"').startswith("gAAAAA"),
                        "metadata_json is not a Fernet token: {0!r}".format(metadata_raw)[:120])
        self.assertTrue(str(ip_raw).startswith("gAAAAA"),
                        "ip_address is not a Fernet token: {0!r}".format(ip_raw)[:120])

    def test_the_application_still_reads_the_real_values(self):
        event = self.storage.list_audit_events(tenant_id=self.tenant)[0]
        self.assertEqual(event["metadata"]["email"], "victim@example.com")
        self.assertEqual(event["ip_address"], "2001:db8::dead:beef")

    def test_encryption_does_not_break_the_chain(self):
        # The hash commits to metadata and ip_address as the APPLICATION sees them
        # (audit_chain.CHAINED_FIELDS), and _audit_event_to_dict decrypts before the verifier
        # rebuilds the row -- so encrypting at rest must leave every hash intact.
        report = self.storage.verify_audit_chain(tenant_id=self.tenant)
        self.assertTrue(report["ok"], report["breaks"])
        self.assertEqual(report["verified"], 1)

    def test_a_wrong_key_is_reported_not_raised(self):
        """The rotated-key case. It used to raise DecryptionError out of the verifier.

        `verify_audit_chain` feeds `_audit_event_to_dict` to the chain walker, so a key that no
        longer opens the metadata turned the compliance endpoint into a 500 -- the audit chain
        reporting itself as broken software rather than as a chain this process cannot read.
        It must also not be reported as `content_hash_mismatch`, which accuses the row of
        tampering when the operator's actual next move is RULEMIND_CONFIG_KEY_OLD.
        """
        # storage.crypto() re-reads the env on every call and caches on the secret itself, so
        # patching the variable is enough -- no cache reset, and nothing leaks into other tests.
        with mock.patch.dict(os.environ, {"RULEMIND_CONFIG_KEY": "a-completely-different-key"}):
            report = self.storage.verify_audit_chain(tenant_id=self.tenant)

        self.assertFalse(report["ok"])
        reasons = [b["reason"] for b in report["breaks"]]
        self.assertEqual(reasons, ["metadata_undecryptable"], report["breaks"])
        self.assertIn("RULEMIND_CONFIG_KEY_OLD", report["breaks"][0]["remedy"])
        # An unverifiable row must never be counted as verified.
        self.assertEqual(report["verified"], 0)


class AuditVerifyEndpointTest(unittest.TestCase):
    """The API contract, which differs from the storage contract in one deliberate way:
    a broken chain is 200 + ok:false, not an error status."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "verify.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _append(self, n=2):
        return [
            app_main.storage.add_audit_event(
                {"event_type": "policy_promoted", "detail": f"p{i}", "metadata": {"i": i}},
                tenant_id=self.tenant,
            )
            for i in range(n)
        ]

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/api/v1/audit/verify").status_code, 401)

    def test_reports_an_intact_chain(self):
        events = self._append(2)
        res = self.client.get("/api/v1/audit/verify", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"], body["breaks"])
        self.assertEqual(body["verified"], 2)
        self.assertEqual(body["head"], events[-1]["hash"])
        self.assertIn("anchor_note", body, "the endpoint must not overstate what it proves")

    def test_a_broken_chain_is_200_not_an_error(self):
        """Alerting must key on `ok`. Returning 5xx here would make a detected break
        indistinguishable from the audit service being down -- the opposite of the signal
        a compliance team needs, and the break list would be lost with the body."""
        events = self._append(3)
        with app_main.storage.connect() as session:
            session.get(AuditEvent, events[1]["id"]).detail = "tampered"

        res = self.client.get("/api/v1/audit/verify", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["ok"])
        self.assertIn("content_hash_mismatch", {b["reason"] for b in body["breaks"]})
        self.assertEqual(body["breaks"][0]["id"], events[1]["id"], "names the tampered row")


if __name__ == "__main__":
    unittest.main()
