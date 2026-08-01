"""Real-time decision fan-out bus (backs the live SSE feed).

With Redis, decisions are PUBLISHed to a per-tenant channel and SSE consumers SUBSCRIBE — no DB
polling, cross-replica. Without Redis it degrades to a no-op publisher + DB-poll consumer. These
tests cover the no-Redis behavior (the CI environment has no Redis) and the frame projection.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import decision_bus  # noqa: E402


class DecisionBusTests(unittest.TestCase):
    def test_compact_frame_projects_only_feed_fields(self):
        frame = decision_bus.compact_frame(
            {
                "id": "d1",
                "policy_id": "p1",
                "outcome": "approve",
                "source": "api",
                "latency_ms": 12,
                "experiment_variant": "champion",
                "created_at": "2026-08-01T00:00:00Z",
                # heavy fields that must NOT be streamed:
                "payload": {"ssn": "x"},
                "trace": [{"step": 1}],
                "computed_variables": {"a": 1},
            }
        )
        self.assertEqual(
            set(frame.keys()),
            {"id", "policy_id", "outcome", "source", "latency_ms", "experiment_variant", "created_at"},
        )
        self.assertNotIn("payload", frame)
        self.assertNotIn("trace", frame)

    def test_channel_is_per_tenant(self):
        self.assertEqual(decision_bus.channel_for("t1"), "rulemind:decisions:t1")
        self.assertNotEqual(decision_bus.channel_for("t1"), decision_bus.channel_for("t2"))

    def test_publish_without_redis_is_a_safe_noop(self):
        saved = os.environ.pop("REDIS_URL", None)
        try:
            # Must not raise even with a weird payload; returns None.
            self.assertIsNone(decision_bus.publish_decision("t1", {"id": "d", "outcome": "approve"}))
            self.assertIsNone(decision_bus.publish_decision("", {}))
            self.assertFalse(decision_bus.has_redis())
        finally:
            if saved is not None:
                os.environ["REDIS_URL"] = saved

    def test_subscribe_without_redis_yields_nothing(self):
        import asyncio

        saved = os.environ.pop("REDIS_URL", None)
        try:
            async def drain():
                out = []
                async for item in decision_bus.subscribe_decisions("t1"):
                    out.append(item)
                return out

            self.assertEqual(asyncio.run(drain()), [])
        finally:
            if saved is not None:
                os.environ["REDIS_URL"] = saved


if __name__ == "__main__":
    unittest.main()
