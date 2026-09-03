"""The collaborative-editing WebSocket must be authenticated and tenant-scoped (Tier 3.1).

Before this, /ws/v1/collab/{doc_id} called websocket.accept() with zero credentials and the hub
was keyed by doc_id ALONE with no notion of a tenant. So anyone on the internet could open
/ws/v1/collab/<a policy id>, receive a full document snapshot in the init frame, and send
edit/restore -- and because doc ids are caller-supplied (typically a policy id), two tenants
naming a document the same way shared one document: tenant A's editor showed tenant B's draft.

Two independent reasons the HTTP auth middleware could not have covered this:
  * Starlette's BaseHTTPMiddleware returns early for any non-HTTP ASGI scope, so a WebSocket
    never reaches the tenant middleware at all;
  * its prefix guard covers /api/v1, /sdk/v1 and /scim/v2 -- /ws/v1 was not in it.
So the check has to live inside the endpoint, and has to run BEFORE accept().
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

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import app.main as app_main  # noqa: E402
import app.routers.collab as collab_router  # noqa: E402
from app.collab_hub import CollabHub  # noqa: E402
from app.storage import Storage  # noqa: E402


class WebSocketAuthTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "ws.db"))
        self._orig_hub = collab_router.hub
        collab_router.hub = CollabHub()
        self.hub = collab_router.hub
        self.client = TestClient(app_main.app)
        self.api_key = app_main.storage.default_api_key or ""
        self.tenant = app_main.storage.default_tenant_id
        self._prev_auth = os.environ.get("AUTH_MODE")
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        collab_router.hub = self._orig_hub
        if self._prev_auth is None:
            os.environ.pop("AUTH_MODE", None)
        else:
            os.environ["AUTH_MODE"] = self._prev_auth
        self.client.close()
        self.tempdir.cleanup()

    def test_an_unauthenticated_connection_is_refused(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/v1/collab/policy-1") as ws:
                ws.receive_json()

    def test_an_invalid_api_key_is_refused(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/v1/collab/policy-1?api_key=nope") as ws:
                ws.receive_json()

    def test_no_document_snapshot_leaks_to_an_unauthenticated_client(self):
        """The specific harm: the FIRST frame the server sent was a full document."""
        self.hub.seed(self.tenant, "policy-1", {"secret_threshold": 12345})
        received = []
        try:
            with self.client.websocket_connect("/ws/v1/collab/policy-1") as ws:
                received.append(ws.receive_json())
        except WebSocketDisconnect:
            pass
        self.assertEqual(received, [], "an unauthenticated client received a document frame")

    def test_a_valid_api_key_connects(self):
        url = "/ws/v1/collab/policy-1?api_key={0}".format(self.api_key)
        with self.client.websocket_connect(url) as ws:
            init = ws.receive_json()
        self.assertEqual(init["type"], "init")

    def test_the_header_form_also_works(self):
        """Non-browser clients should use a header rather than putting a key in the query string
        (query strings land in access logs)."""
        with self.client.websocket_connect("/ws/v1/collab/policy-1",
                                           headers={"x-api-key": self.api_key}) as ws:
            self.assertEqual(ws.receive_json()["type"], "init")

    def test_the_session_cookie_authenticates(self):
        """The dashboard's real credential. Browsers send cookies on a same-origin WS handshake,
        which is why the web client needs no change to keep working."""
        from app.session_cookie import SESSION_COOKIE

        member = app_main.storage.create_member(
            tenant_id=self.tenant, email="editor@example.com", name="Editor", role="policy_maker")
        session = app_main.storage.create_member_session(self.tenant, member["id"])
        token = session.get("token") or session.get("session_token")
        self.assertTrue(token, "could not mint a member session")

        self.client.cookies.set(SESSION_COOKIE, token)
        try:
            with self.client.websocket_connect("/ws/v1/collab/policy-1") as ws:
                self.assertEqual(ws.receive_json()["type"], "init")
        finally:
            self.client.cookies.clear()

    def test_auth_mode_none_still_connects(self):
        """Parity with HTTP: disabling auth for local development must not break the editor."""
        os.environ["AUTH_MODE"] = "none"
        with self.client.websocket_connect("/ws/v1/collab/policy-1") as ws:
            self.assertEqual(ws.receive_json()["type"], "init")


class TenantScopingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "ws2.db"))
        self._orig_hub = collab_router.hub
        collab_router.hub = CollabHub()
        self.hub = collab_router.hub
        self.client = TestClient(app_main.app)
        self.first = app_main.storage.default_tenant_id
        self.first_key = app_main.storage.default_api_key or ""
        second = app_main.storage.create_tenant("Second Co")
        self.second = second["id"]
        self.second_key = app_main.storage.generate_api_key_for_tenant(self.second)["plaintext"]
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        collab_router.hub = self._orig_hub
        self.client.close()
        self.tempdir.cleanup()

    def test_the_same_doc_id_is_two_separate_documents(self):
        """doc ids are caller-supplied (usually a policy id), so a collision across tenants is
        the normal case, not an exotic one."""
        self.hub.seed(self.first, "shared-id", {"owner": "first"})
        self.hub.seed(self.second, "shared-id", {"owner": "second"})
        self.assertEqual(self.hub.state(self.first, "shared-id")["doc"]["owner"], "first")
        self.assertEqual(self.hub.state(self.second, "shared-id")["doc"]["owner"], "second")

    def test_an_edit_does_not_cross_tenants(self):
        import asyncio

        self.hub.seed(self.first, "shared-id", {"threshold": 700})
        self.hub.seed(self.second, "shared-id", {"threshold": 700})
        asyncio.run(self.hub.edit(self.second, "shared-id", "bob", {"threshold": 999}))
        self.assertEqual(self.hub.state(self.first, "shared-id")["doc"]["threshold"], 700,
                         "one tenant's edit changed another tenant's document")
        self.assertEqual(self.hub.state(self.second, "shared-id")["doc"]["threshold"], 999)

    def test_a_websocket_reads_only_its_own_tenants_document(self):
        self.hub.seed(self.first, "shared-id", {"owner": "first"})
        self.hub.seed(self.second, "shared-id", {"owner": "second"})
        url = "/ws/v1/collab/shared-id?api_key={0}".format(self.second_key)
        with self.client.websocket_connect(url) as ws:
            init = ws.receive_json()
        self.assertEqual(init["doc"]["owner"], "second",
                         "the WebSocket served another tenant's document")

    def test_the_rest_siblings_are_scoped_too(self):
        """state / history / as-of / seed were all equally unscoped."""
        self.hub.seed(self.first, "shared-id", {"owner": "first"})
        self.hub.seed(self.second, "shared-id", {"owner": "second"})
        res = self.client.get("/api/v1/collab/shared-id/state",
                              headers={"x-api-key": self.second_key})
        self.assertEqual(res.status_code, 200, res.text[:200])
        self.assertEqual(res.json()["doc"]["owner"], "second")

    def test_the_rest_siblings_require_auth(self):
        for path in ["/api/v1/collab/shared-id/state", "/api/v1/collab/shared-id/history",
                     "/api/v1/collab/shared-id/as-of/0"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)


class WriteAuthorizationTest(unittest.TestCase):
    """A read-only role may watch a session but must not be able to edit it."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "ws3.db"))
        self._orig_hub = collab_router.hub
        collab_router.hub = CollabHub()
        self.hub = collab_router.hub
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.viewer_key = app_main.storage.generate_api_key_for_tenant(
            self.tenant, role="viewer")["plaintext"]
        os.environ.pop("AUTH_MODE", None)

    def tearDown(self):
        collab_router.hub = self._orig_hub
        self.client.close()
        self.tempdir.cleanup()

    def test_a_viewer_cannot_edit_over_the_websocket(self):
        self.hub.seed(self.tenant, "doc-x", {"threshold": 700})
        url = "/ws/v1/collab/doc-x?api_key={0}".format(self.viewer_key)
        with self.client.websocket_connect(url) as ws:
            ws.receive_json()  # init
            ws.send_json({"type": "edit", "changes": {"threshold": 999}})
            reply = ws.receive_json()
        self.assertEqual(reply.get("type"), "error")
        self.assertEqual(self.hub.state(self.tenant, "doc-x")["doc"]["threshold"], 700,
                         "a viewer edited the document")

    def test_a_viewer_cannot_restore_over_the_websocket(self):
        import asyncio

        self.hub.seed(self.tenant, "doc-y", {"threshold": 700})
        asyncio.run(self.hub.edit(self.tenant, "doc-y", "alice", {"threshold": 800}))
        url = "/ws/v1/collab/doc-y?api_key={0}".format(self.viewer_key)
        with self.client.websocket_connect(url) as ws:
            ws.receive_json()
            ws.send_json({"type": "restore", "version": 0})
            reply = ws.receive_json()
        self.assertEqual(reply.get("type"), "error")
        self.assertEqual(self.hub.state(self.tenant, "doc-y")["doc"]["threshold"], 800)


if __name__ == "__main__":
    unittest.main()
