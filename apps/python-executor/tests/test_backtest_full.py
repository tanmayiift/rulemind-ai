"""Full-population streaming backtest (opt-in `full=True`) — TC-ANL-002.

Proves the full mode scans the ENTIRE decision population (beyond the 2000-row sample cap),
via a memory-bounded keyset-paginated generator, while sample mode stays bounded — and that both
are deterministic.
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

from app.backtest import backtest_policy  # noqa: E402
from app.storage import Storage  # noqa: E402


class BacktestFullModeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.st = Storage(path=os.path.join(self.td.name, "bt.db"))
        self.tn = self.st.default_tenant_id
        # A policy + a compiled bundle to backtest against.
        policy = {"id": "POL-BT", "name": "bt", "defaultOutcome": "review",
                  "steps": [{"id": "s", "type": "outcome", "outcome": "approve"}]}
        self.st.create_policy(policy, tenant_id=self.tn)
        # Save a minimal compiled bundle directly (the full compile pipeline requires a
        # production-promoted policy; backtest only needs bundle["content"] = {policy, rules}).
        self.st.save_bundle({
            "version": 1,
            "content": {"policy": policy, "rules": {}},
            "encrypted_content": "x", "signature": "x", "checksum": "x",
        }, tenant_id=self.tn)
        # Seed 2,500 decisions — deliberately MORE than the 2000-row sample cap.
        self.n = 2500
        for i in range(self.n):
            self.st.add_decision(
                {"id": f"bt-{i:05d}", "policy_id": "POL-BT", "outcome": "reject",
                 "payload": {"score": 500 + (i % 400)}, "computed_variables": {"score": 500 + (i % 400)}},
                tenant_id=self.tn,
            )

    def tearDown(self):
        self.td.cleanup()

    def test_streaming_generator_yields_entire_population_once(self):
        seen = [d["id"] for d in self.st.iter_policy_decisions("POL-BT", tenant_id=self.tn, page_size=500)]
        self.assertEqual(len(seen), self.n)              # every row, across 5 pages
        self.assertEqual(len(set(seen)), self.n)         # no dup, no gap (keyset cursor correct)

    def test_full_mode_scans_whole_population_beyond_sample_cap(self):

        sampled = backtest_policy(self.st, "POL-BT", tenant_id=self.tn, sample=2000)
        full = backtest_policy(self.st, "POL-BT", tenant_id=self.tn, full=True, page_size=500)
        self.assertEqual(sampled["mode"], "sample")
        self.assertLessEqual(sampled["scanned"], 2000)   # sample mode is bounded by the cap
        self.assertEqual(full["mode"], "full")
        self.assertEqual(full["scanned"], self.n)        # full mode replays ALL 2,500 (> cap)

    def test_full_mode_is_deterministic(self):

        a = backtest_policy(self.st, "POL-BT", tenant_id=self.tn, full=True)
        b = backtest_policy(self.st, "POL-BT", tenant_id=self.tn, full=True)
        self.assertEqual(a["transition_matrix"], b["transition_matrix"])
        self.assertEqual(a["replayed_distribution"], b["replayed_distribution"])


if __name__ == "__main__":
    unittest.main()
