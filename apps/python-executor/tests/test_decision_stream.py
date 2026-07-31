"""Live decision feed over Server-Sent Events — GET /api/v1/decisions/stream.

The stream opens with a recent backlog (oldest-first), then pushes each new decision as a
`decision` event while advancing a monotonic created_at cursor so nothing is re-sent. The
`?once=1` mode emits the current backlog and closes, which is what these tests exercise (an
infinite live stream isn't terminable under TestClient); `?after=` proves the cursor filter.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402


def _parse_sse(text: str):
    """Parse an SSE payload into a list of (event, data) tuples."""
    events = []
    event_name = "message"
    data_lines = []
    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif line == "":
            if data_lines:
                events.append((event_name, "\n".join(data_lines)))
            event_name = "message"
            data_lines = []
    if data_lines:
        events.append((event_name, "\n".join(data_lines)))
    return events


class DecisionStreamTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id
        prod = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)
        self.policy = prod[0]

    def _emit_decision(self, outcome: str = "approve") -> str:
        r = self.client.post(
            "/sdk/v1/decide",
            headers=self.headers,
            json={"policyId": self.policy["id"], "payload": {"amount": 1000}},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json().get("executionId") or r.json().get("execution_id") or ""

    def test_stream_is_event_stream_with_backlog(self):
        self._emit_decision()
        r = self.client.get("/api/v1/decisions/stream?once=1", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("content-type", "").startswith("text/event-stream"))
        self.assertEqual(r.headers.get("cache-control"), "no-cache")
        events = _parse_sse(r.text)
        decision_events = [e for e in events if e[0] == "decision"]
        self.assertGreater(len(decision_events), 0, "backlog should include at least the decision just emitted")

    def test_far_future_cursor_yields_empty_backlog(self):
        self._emit_decision()
        empty = self.client.get(
            "/api/v1/decisions/stream?once=1&after=2999-01-01T00:00:00Z", headers=self.headers
        )
        self.assertEqual([e for e in _parse_sse(empty.text) if e[0] == "decision"], [])

    def test_after_cursor_surfaces_strictly_newer_decisions(self):
        import json as _json

        # Emit A, read the backlog, take A's (second-precision) created_at as a resume cursor.
        self._emit_decision()
        first = self.client.get("/api/v1/decisions/stream?once=1", headers=self.headers)
        events = [e for e in _parse_sse(first.text) if e[0] == "decision"]
        cursor = _json.loads(events[-1][1])["created_at"]

        # A strictly-later decision (next second) must surface when resuming after that cursor.
        time.sleep(1.05)  # created_at has second precision; force a strictly later timestamp
        self._emit_decision("review")
        resumed = self.client.get(
            "/api/v1/decisions/stream?once=1&after={0}".format(cursor), headers=self.headers
        )
        new_events = [e for e in _parse_sse(resumed.text) if e[0] == "decision"]
        self.assertGreater(len(new_events), 0)

    def test_requires_auth(self):
        r = self.client.get("/api/v1/decisions/stream?once=1")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
