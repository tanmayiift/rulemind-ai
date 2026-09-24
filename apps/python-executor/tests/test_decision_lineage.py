"""A decision must say WHICH VERSION of the policy produced it.

The question this product exists to answer is "why was this application rejected eight months
ago". `rule_results` answers which condition failed and by how much, and that part is genuinely
good. What could not be answered is **which version of the policy was live**, because `Decision`
carried only `policy_id` and policies mutate in place with `Policy.version` incrementing on the
row. The lineage had to be inferred — take `created_at`, walk `EntityHistory`, pick the snapshot
that was current — and for a regulator question that inference IS the answer. The product made
the examiner do it by hand.

`bundle_hash` is the same gap from the other side and is **deliberately not fixed the same way**.
The column has existed since 20260806_0015 and `add_decision` reads it, but only the SDK sync
path ever supplies one — so on-device decisions carry bundle lineage and server decisions do
not. It stays that way on BOTH server paths, and the reason is what the column means: "the
compiled SDK bundle this device decided from". The executor never loads a bundle at all (it
reads rules and scorecards live from the DB), and the fast path's in-process serving bundle is
not that artifact and has no published hash. Hashing it anyway would put two different meanings
in one column, which is worse than a NULL — a NULL is legible. `policy_version` is the
server-side lineage, and it is recorded on both.

(An earlier draft of this file, and a comment in `executor.py`, said bundle_hash *is* populated
on the fast path. It is not; the word does not appear in `_log_decision`. Corrected in both
places rather than left as a claim that reads as a guarantee.)

Existing rows stay NULL on purpose. Backfilling a guess from `EntityHistory` would put invented
lineage into an attestable record — the same mistake as the hardcoded adverse-action codes this
round removed. NULL means "not recorded", which is true.
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

from app.models import Decision  # noqa: E402
from app.storage import Storage  # noqa: E402


class DecisionLineageColumnTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "lineage.db"))
        self.tenant = self.storage.default_tenant_id

    def _add(self, **extra):
        rec = {"id": extra.pop("id", "d-lineage"), "policy_id": "p1", "outcome": "approve",
               "payload": {}, "computed_variables": {}, "rule_results": [], "trace": [],
               "latency_ms": 1, "source": "api"}
        rec.update(extra)
        return self.storage.add_decision(rec, tenant_id=self.tenant)

    def test_the_column_exists(self):
        self.assertTrue(hasattr(Decision, "policy_version"))

    def test_policy_version_round_trips(self):
        self._add(policy_version=7)
        got = self.storage.get_decision("d-lineage", tenant_id=self.tenant)
        self.assertEqual(got.get("policy_version"), 7)

    def test_camelCase_is_accepted_like_bundle_hash_is(self):
        """The SDK path sends camelCase; policy_version must not be the one field that differs."""
        self._add(id="d-camel", policyVersion=3)
        self.assertEqual(self.storage.get_decision("d-camel", tenant_id=self.tenant).get("policy_version"), 3)

    def test_absent_means_NULL_not_zero(self):
        """A decision recorded before this column existed does not know its version. Zero would
        be a value; None is the truth."""
        self._add(id="d-none")
        self.assertIsNone(self.storage.get_decision("d-none", tenant_id=self.tenant).get("policy_version"))

    def test_it_is_returned_by_the_read_path(self):
        self._add(id="d-read", policy_version=11)
        rows = self.storage.list_decisions(tenant_id=self.tenant)
        row = next(r for r in rows if r["id"] == "d-read")
        self.assertIn("policy_version", row)
        self.assertEqual(row["policy_version"], 11)


class BothDecisionPathsRecordItTest(unittest.TestCase):
    """Source-level, because exercising both engines needs a seeded workspace and the point is
    that ONE of them used to omit it. A field that only the fast path records is the same
    divergence class as `decision_id` disappearing under FAST_DECIDE."""

    APP = APP_ROOT / "app"

    def test_the_executor_records_the_policy_version(self):
        src = (self.APP / "executor.py").read_text(encoding="utf-8")
        self.assertIn('"policy_version": ctx.policy_version', src)
        self.assertIn("policy_version=policy.get(\"version\")", src)

    def test_the_fast_path_records_the_policy_version(self):
        src = (self.APP / "fast_decide.py").read_text(encoding="utf-8")
        self.assertIn('"policy_version": decision.get("policy_version")', src)

    def test_the_executor_does_not_invent_a_bundle_hash(self):
        """It never loads a bundle. Recording one would be a fabrication, and the honest NULL is
        the whole reason this test exists rather than a symmetrical fix."""
        src = (self.APP / "executor.py").read_text(encoding="utf-8")
        self.assertNotIn('"bundle_hash"', src)


if __name__ == "__main__":
    unittest.main()
