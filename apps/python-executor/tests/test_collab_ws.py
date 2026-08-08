"""Collaborative editor — WebSocket live-sync + presence + time-travel (the live-collab layer).

Uses FastAPI's TestClient websocket support to connect TWO clients to the same document and asserts:
edits broadcast, presence updates, concurrent edits converge to the same state on both clients, and
the time-travel REST endpoints return past versions.
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.collab_hub import CollabHub  # noqa: E402


class CollabWebSocketTests(unittest.TestCase):
    def setUp(self):
        # Fresh hub per test so docs don't bleed across cases.
        import app.routers.collab as collab_router
        self._orig_hub = collab_router.hub
        collab_router.hub = CollabHub()
        self.hub = collab_router.hub
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        import app.routers.collab as collab_router
        collab_router.hub = self._orig_hub

    def _drain(self, ws, want_type, max_msgs=10):
        for _ in range(max_msgs):
            msg = ws.receive_json()
            if msg.get("type") == want_type:
                return msg
        raise AssertionError("did not receive a {0} message".format(want_type))

    def _drain_edit_with(self, ws, field, value, max_msgs=12):
        """Read messages until an 'edit' whose merged doc has field==value. Tolerates the sender's
        own echo and interleaved presence frames (the server broadcasts each edit to everyone)."""
        for _ in range(max_msgs):
            msg = ws.receive_json()
            if msg.get("type") == "edit" and msg.get("doc", {}).get(field) == value:
                return msg
        raise AssertionError("did not receive an edit with {0}={1}".format(field, value))

    def test_two_clients_live_sync_presence_and_convergence(self):
        doc = "policy-doc-1"
        with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=alice") as alice:
            init_a = alice.receive_json()
            self.assertEqual(init_a["type"], "init")
            with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=bob") as bob:
                init_b = bob.receive_json()
                self.assertEqual(init_b["type"], "init")
                # alice sees bob join (presence broadcast).
                pres = self._drain(alice, "presence")
                actors = {p["actor"] for p in pres["presence"]}
                self.assertEqual(actors, {"alice", "bob"})

                # alice edits 'name'; bob receives the merged edit.
                alice.send_json({"type": "edit", "changes": {"name": "Loan Policy v2"}})
                edit_b = self._drain_edit_with(bob, "name", "Loan Policy v2")
                self.assertEqual(edit_b["actor"], "alice")

                # bob edits a DIFFERENT field; alice receives it and both fields coexist (CRDT).
                bob.send_json({"type": "edit", "changes": {"threshold": 750}})
                edit_a = self._drain_edit_with(alice, "threshold", 750)
                self.assertEqual(edit_a["doc"]["name"], "Loan Policy v2")  # not lost

        # Both clients converged on the same authoritative state.
        state = self.hub.state(doc)
        self.assertEqual(state["doc"], {"name": "Loan Policy v2", "threshold": 750})

    def test_concurrent_same_field_edits_converge_deterministically(self):
        doc = "policy-doc-2"
        with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=alice") as alice:
            alice.receive_json()
            with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=bob") as bob:
                bob.receive_json()
                self._drain(alice, "presence")
                # Both edit the SAME field; server assigns increasing Lamport ts -> last one wins,
                # and BOTH clients end up agreeing on that single value (no split-brain).
                alice.send_json({"type": "edit", "changes": {"threshold": 700}})
                self._drain(bob, "edit")
                bob.send_json({"type": "edit", "changes": {"threshold": 800}})
                self._drain(alice, "edit")
        self.assertEqual(self.hub.state(doc)["doc"]["threshold"], 800)

    def test_time_travel_history_and_restore(self):
        doc = "policy-doc-3"
        with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=alice") as alice:
            alice.receive_json()
            alice.send_json({"type": "edit", "changes": {"threshold": 700}})
            self._drain(alice, "edit")
            alice.send_json({"type": "edit", "changes": {"threshold": 750}})
            self._drain(alice, "edit")
            # Restore to version 1 (threshold 700); server re-applies it as a NEW forward version.
            alice.send_json({"type": "restore", "version": 1})
            restored = self._drain(alice, "edit")
            self.assertEqual(restored["doc"]["threshold"], 700)

        # REST time-travel endpoints see the full timeline.
        hist = self.client.get(f"/api/v1/collab/{doc}/history", headers=self.headers).json()["history"]
        versions = [h["version"] for h in hist]
        self.assertEqual(versions, [0, 1, 2, 3])  # seed + 2 edits + 1 restore
        as_of_1 = self.client.get(f"/api/v1/collab/{doc}/as-of/1", headers=self.headers).json()
        self.assertEqual(as_of_1["doc"]["threshold"], 700)
        as_of_2 = self.client.get(f"/api/v1/collab/{doc}/as-of/2", headers=self.headers).json()
        self.assertEqual(as_of_2["doc"]["threshold"], 750)

    def test_presence_drops_on_disconnect(self):
        doc = "policy-doc-4"
        with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=alice") as alice:
            alice.receive_json()
            with self.client.websocket_connect(f"/ws/v1/collab/{doc}?actor=bob") as bob:
                bob.receive_json()
                self._drain(alice, "presence")  # bob joined
            # bob disconnected -> alice gets a presence update with only alice.
            pres = self._drain(alice, "presence")
            self.assertEqual({p["actor"] for p in pres["presence"]}, {"alice"})


if __name__ == "__main__":
    unittest.main()
