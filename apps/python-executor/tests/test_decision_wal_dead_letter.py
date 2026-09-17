"""A decision the WAL can never re-insert must stop being retried, and must be visible.

WHAT WAS MISSING. `_recover_one` KEEPS a record whose INSERT fails and retries it on the next
boot. That is exactly right for a transient DB error at startup, and exactly wrong for a record
that can never insert -- a constraint violation, a row that predates a schema change. With no
bound, such a record is replayed on every boot for the life of the deployment, it makes every
startup a little slower, and nothing anywhere says it exists. The code comment claimed a
permanently-bad record "stays inspectable on disk"; nothing ever pointed an operator at the disk.

Deleting it was never an option. A WAL record is a decision that was already RETURNED TO A
CALLER and is not in the database -- the single thing this module exists to prevent losing. So
after DECISION_WAL_MAX_REPLAY_ATTEMPTS it moves to a dead-letter file: out of the boot path,
still exactly recoverable, and counted where someone can see it.

This is the DLQ half of parity row P09, which is worth far more than the Kafka half and does not
require it. The other half of P09-partial, `Idempotency-Key`, already ships (app/idempotency.py).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from sqlalchemy import func, select  # noqa: E402

import app.decision_wal as decision_wal  # noqa: E402
from app.models import Decision  # noqa: E402
from app.storage import Storage  # noqa: E402


def _record(rec_id: str, tenant_id: str) -> dict:
    return {"id": rec_id, "tenant_id": tenant_id, "policy_id": "p1", "outcome": "approve",
            "payload": {"score": 720}, "source": "api_fast"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DECISION_WAL"] = "1"
        os.environ["DECISION_WAL_DIR"] = os.path.join(self.tempdir.name, "wal")
        os.environ["DECISION_WAL_MAX_REPLAY_ATTEMPTS"] = "3"
        self.storage = Storage(path=os.path.join(self.tempdir.name, "wal.db"))
        self.tenant = self.storage.default_tenant_id
        decision_wal.reset_for_test()

    def tearDown(self):
        decision_wal.reset_for_test()
        for key in ("DECISION_WAL", "DECISION_WAL_DIR", "DECISION_WAL_MAX_REPLAY_ATTEMPTS"):
            os.environ.pop(key, None)
        self.tempdir.cleanup()

    def decisions_in_db(self) -> int:
        with self.storage.connect() as session:
            return int(session.scalar(select(func.count(Decision.id))) or 0)

    def append(self, rec_id="d-bad"):
        decision_wal.append(_record(rec_id, self.tenant), tenant_id=self.tenant)

    def recover_while_failing(self, times, exc=None):
        """Run recover() `times` times with add_decision always raising -- one boot per call."""
        results = []
        error = exc or RuntimeError("constraint violation")
        for _ in range(times):
            decision_wal.reset_for_test()
            with mock.patch.object(type(self.storage), "add_decision", side_effect=error):
                results.append(decision_wal.recover(self.storage))
        return results


class ARecordThatCanNeverInsertStopsBeingRetriedTest(_Base):

    def test_it_is_retried_up_to_the_limit_then_dead_lettered(self):
        self.append()
        results = self.recover_while_failing(3)
        # The first two boots keep it for retry; the third exhausts the attempts.
        self.assertEqual([r["dead_lettered"] for r in results], [0, 0, 1],
                         "the record was not dead-lettered on the attempt limit")

    def test_once_dead_lettered_a_later_boot_does_not_retry_it(self):
        """The point of the bound. Before this, every boot forever re-attempted the same
        unusable record."""
        self.append()
        self.recover_while_failing(3)
        attempts = []

        def counting_add(self_inner, record, tenant_id=None):
            attempts.append(record.get("id"))
            raise RuntimeError("constraint violation")

        decision_wal.reset_for_test()
        with mock.patch.object(type(self.storage), "add_decision", counting_add):
            decision_wal.recover(self.storage)
        self.assertEqual(attempts, [], "a dead-lettered record was retried on a later boot")

    def test_the_record_is_not_lost(self):
        """It is a decision that was returned to a caller. Every field must survive."""
        self.append("d-keepme")
        self.recover_while_failing(3)
        entries = decision_wal.dead_letter_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "d-keepme")
        self.assertEqual(entries[0]["record"], _record("d-keepme", self.tenant))
        self.assertEqual(entries[0]["tenant_id"], self.tenant)

    def test_the_reason_is_recorded_so_the_cause_is_diagnosable(self):
        self.append()
        self.recover_while_failing(3, exc=RuntimeError("UNIQUE constraint failed: decisions.id"))
        entry = decision_wal.dead_letter_entries()[0]
        self.assertIn("UNIQUE constraint failed", entry["dead_lettered_reason"])

    def test_a_transient_failure_that_later_succeeds_is_never_dead_lettered(self):
        """The control, and the behaviour that must NOT regress: two bad boots then a good one
        replays normally, because that is the case the retry exists for."""
        self.append("d-transient")
        self.recover_while_failing(2)
        self.assertEqual(decision_wal.dead_letter_entries(), [])
        decision_wal.reset_for_test()
        result = decision_wal.recover(self.storage)
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(result["dead_lettered"], 0)
        self.assertEqual(self.decisions_in_db(), 1)

    def test_a_healthy_record_alongside_a_poisonous_one_still_lands(self):
        """One bad record must not hold up the rest -- the existing guarantee, re-asserted
        because the dead-letter branch is new code in that loop."""
        good = _record("d-good", self.tenant)
        decision_wal.append(good, tenant_id=self.tenant)
        decision_wal.append(_record("d-bad", self.tenant), tenant_id=self.tenant)
        real_add = type(self.storage).add_decision

        def selective(self_inner, record, tenant_id=None):
            if record.get("id") == "d-bad":
                raise RuntimeError("constraint violation")
            return real_add(self_inner, record, tenant_id=tenant_id)

        for _ in range(3):
            decision_wal.reset_for_test()
            with mock.patch.object(type(self.storage), "add_decision", selective):
                decision_wal.recover(self.storage)
        self.assertEqual(self.decisions_in_db(), 1)
        self.assertEqual([e["id"] for e in decision_wal.dead_letter_entries()], ["d-bad"])


class TheDeadLetterIsAQueueNotAGraveyardTest(_Base):
    """The usual cause is a bug or a missing migration that has since been fixed, and the fix
    must not require hand-editing a JSONL file."""

    def dead_letter_one(self, rec_id="d-bad"):
        self.append(rec_id)
        self.recover_while_failing(3)

    def test_replay_reinserts_a_record_once_the_cause_is_fixed(self):
        self.dead_letter_one()
        result = decision_wal.replay_dead_letter(self.storage)
        self.assertEqual(result, {"attempted": 1, "replayed": 1, "still_failing": 0})
        self.assertEqual(self.decisions_in_db(), 1)
        self.assertEqual(decision_wal.dead_letter_entries(), [],
                         "a successfully replayed record was left in the dead letter")

    def test_replay_leaves_exactly_what_still_fails(self):
        self.append("d-ok")
        self.append("d-still-bad")
        self.recover_while_failing(3)
        self.assertEqual(len(decision_wal.dead_letter_entries()), 2)
        real_add = type(self.storage).add_decision

        def selective(self_inner, record, tenant_id=None):
            if record.get("id") == "d-still-bad":
                raise RuntimeError("still broken")
            return real_add(self_inner, record, tenant_id=tenant_id)

        with mock.patch.object(type(self.storage), "add_decision", selective):
            result = decision_wal.replay_dead_letter(self.storage)
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(result["still_failing"], 1)
        self.assertEqual([e["id"] for e in decision_wal.dead_letter_entries()], ["d-still-bad"])

    def test_a_record_that_became_durable_elsewhere_counts_as_resolved(self):
        """Idempotency: if the decision reached the DB by another route, replay must not try to
        insert it again and must not report it as still failing."""
        self.dead_letter_one("d-elsewhere")
        self.storage.add_decision(_record("d-elsewhere", self.tenant), tenant_id=self.tenant)
        result = decision_wal.replay_dead_letter(self.storage)
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(result["still_failing"], 0)
        self.assertEqual(self.decisions_in_db(), 1, "the decision was inserted twice")

    def test_replay_on_an_empty_dead_letter_is_a_no_op(self):
        self.assertEqual(decision_wal.replay_dead_letter(self.storage),
                         {"attempted": 0, "replayed": 0, "still_failing": 0})

    def test_a_replayed_record_that_fails_again_gets_a_fresh_attempt_budget(self):
        """An operator invoking replay is asserting the cause has changed, so the counter that
        sent it here is reset rather than leaving it permanently one attempt from exhaustion."""
        self.dead_letter_one()
        with mock.patch.object(type(self.storage), "add_decision",
                               side_effect=RuntimeError("still broken")):
            decision_wal.replay_dead_letter(self.storage)
        self.assertNotIn("replay_attempts", decision_wal.dead_letter_entries()[0])


class ItIsVisibleTest(_Base):

    def test_recover_reports_the_standing_total_not_a_delta(self):
        """It persists across boots until someone acts on it, so a boot that dead-letters
        nothing must still report what is sitting there."""
        self.append()
        self.recover_while_failing(3)
        decision_wal.reset_for_test()
        self.assertEqual(decision_wal.recover(self.storage)["dead_lettered"], 1)

    def test_the_count_is_zero_on_a_clean_deployment(self):
        self.append("d-fine")
        decision_wal.reset_for_test()
        result = decision_wal.recover(self.storage)
        self.assertEqual(result["dead_lettered"], 0)
        self.assertEqual(result["replayed"], 1)

    def test_disabled_wal_reports_the_field_rather_than_omitting_it(self):
        """Callers read result["dead_lettered"]; a missing key would be a KeyError at startup."""
        os.environ.pop("DECISION_WAL", None)
        self.assertEqual(decision_wal.recover(self.storage)["dead_lettered"], 0)


if __name__ == "__main__":
    unittest.main()
