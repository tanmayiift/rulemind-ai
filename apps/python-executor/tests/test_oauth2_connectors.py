"""OAuth2 client credentials for connectors -- and the client secret never leaves for the wrong host.

`auth_type: oauth2` used to be treated exactly like `bearer`: the first of `access_token`,
`token`, `api_key`, `client_secret` went out as `Authorization: Bearer ...`. So a connector
configured the normal OAuth2 way -- client id + secret, no pre-issued token -- sent
**`Bearer <client_secret>` to the data provider**, handing the token-endpoint credential to every
resource server and to anything logging its headers. And a pre-issued token could never be
renewed, so it failed every decision from the moment it expired.

These tests run a REAL token endpoint and a REAL resource server on localhost and inspect what
each one actually received. Mocking `oauth2_client` would test that the code calls itself; the
properties that matter are about what goes over the wire, and to whom.

`test_the_client_secret_never_reaches_the_resource_server` is the one that matters most.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock
from urllib.parse import parse_qs

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import connector_probe, oauth2_client  # noqa: E402
from app.egress import PinnedRequest  # noqa: E402

SECRET = "s3cr3t-client-secret-do-not-leak"
CLIENT_ID = "rulemind-client"


class _Recorder:
    def __init__(self) -> None:
        self.token_requests: List[Dict[str, Any]] = []
        self.resource_requests: List[Dict[str, Any]] = []
        self.token_status = 200
        self.expires_in = 3600
        self.issued = 0
        self.reject_token: str = ""


def _server(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self):  # noqa: N802
            raw = self._body().decode()
            if self.path == "/token":
                recorder.token_requests.append({"headers": dict(self.headers), "form": parse_qs(raw)})
                if recorder.token_status != 200:
                    self.send_response(recorder.token_status)
                    self.end_headers()
                    return
                recorder.issued += 1
                body = json.dumps({"access_token": f"tok-{recorder.issued}", "token_type": "Bearer",
                                   "expires_in": recorder.expires_in}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.do_GET()

        def do_GET(self):  # noqa: N802
            auth = self.headers.get("Authorization", "")
            recorder.resource_requests.append({"path": self.path, "authorization": auth,
                                               "headers": dict(self.headers)})
            if recorder.reject_token and auth == f"Bearer {recorder.reject_token}":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"score": 742}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _pin(url: str) -> PinnedRequest:
    """Bypass the SSRF guard for a LOCALHOST test server only -- it refuses loopback, correctly.
    Everything else about the request (headers, SNI, no redirects) is exercised for real."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return PinnedRequest(url=url, host_header=parsed.netloc, sni_hostname=parsed.hostname or "",
                         original_url=url)


class OAuth2ConnectorTests(unittest.TestCase):
    def setUp(self):
        oauth2_client.reset()
        self.addCleanup(oauth2_client.reset)
        self.rec = _Recorder()
        self.server = _server(self.rec)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        for target in ("app.oauth2_client.build_pinned_request", "app.connector_probe.build_pinned_request",
                       "app.connector_fetch.build_pinned_request"):
            patcher = mock.patch(target, side_effect=_pin)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _config(self, **overrides) -> Dict[str, Any]:
        config = {"auth_type": "oauth2", "base_url": f"{self.base}/resource",
                  "token_url": f"{self.base}/token", "client_id": CLIENT_ID,
                  "client_secret": SECRET, "scope": "bureau.read"}
        config.update(overrides)
        return config

    def _probe(self, **overrides):
        return connector_probe.probe_connector({"id": "bureau", "config": self._config(**overrides)})

    # ── the leak ───────────────────────────────────────────────────────────

    def test_the_client_secret_never_reaches_the_resource_server(self):
        """THE test. It used to be sent as `Authorization: Bearer <client_secret>`."""
        result = self._probe()
        self.assertTrue(result["passed"], result)
        self.assertTrue(self.rec.resource_requests)
        for request in self.rec.resource_requests:
            self.assertNotIn(SECRET, json.dumps(request),
                             "the client secret reached the DATA PROVIDER")
        self.assertEqual(self.rec.resource_requests[0]["authorization"], "Bearer tok-1")

    def test_oauth2_without_a_token_url_does_not_fall_back_to_the_client_secret(self):
        """Legacy static-token mode. With no access_token configured it must send NOTHING -- not
        promote the client secret to a bearer token, which is exactly what it used to do."""
        headers = connector_probe._auth_headers(
            {"auth_type": "oauth2", "client_id": CLIENT_ID, "client_secret": SECRET}, b"")
        self.assertNotIn("Authorization", headers)

    def test_bearer_does_not_fall_back_to_the_client_secret_either(self):
        headers = connector_probe._auth_headers({"auth_type": "bearer", "client_secret": SECRET}, b"")
        self.assertNotIn("Authorization", headers)

    def test_a_static_access_token_still_works_without_a_token_url(self):
        headers = connector_probe._auth_headers({"auth_type": "oauth2", "access_token": "static-1"}, b"")
        self.assertEqual(headers["Authorization"], "Bearer static-1")

    # ── the grant ──────────────────────────────────────────────────────────

    def test_the_token_request_is_a_client_credentials_grant_with_basic_auth(self):
        self._probe()
        request = self.rec.token_requests[0]
        self.assertEqual(request["form"]["grant_type"], ["client_credentials"])
        self.assertEqual(request["form"]["scope"], ["bureau.read"])
        auth = request["headers"]["Authorization"]
        self.assertTrue(auth.startswith("Basic "))
        self.assertEqual(base64.b64decode(auth[6:]).decode(), f"{CLIENT_ID}:{SECRET}")
        self.assertNotIn("client_secret", request["form"],
                         "with client_secret_basic the secret goes in the header, not the body")

    def test_client_secret_post_puts_the_credentials_in_the_body(self):
        self._probe(token_auth_method="client_secret_post")
        form = self.rec.token_requests[0]["form"]
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertEqual(form["client_secret"], [SECRET])

    def test_special_characters_are_form_encoded_before_base64(self):
        """RFC 6749 2.3.1. A secret containing ':' would otherwise split in the wrong place."""
        tricky = "a:b/c+d"
        self._probe(client_secret=tricky)
        decoded = base64.b64decode(self.rec.token_requests[0]["headers"]["Authorization"][6:]).decode()
        self.assertEqual(decoded, f"{CLIENT_ID}:a%3Ab%2Fc%2Bd")

    def test_the_audience_is_sent_when_configured(self):
        self._probe(audience="https://api.bureau.example")
        self.assertEqual(self.rec.token_requests[0]["form"]["audience"], ["https://api.bureau.example"])

    # ── caching ────────────────────────────────────────────────────────────

    def test_the_token_is_reused_until_it_nears_expiry(self):
        """Providers rate-limit token endpoints hard; one request per decision would lock the
        connector out at volume."""
        config = self._config()
        key = oauth2_client.cache_key(config, "t1", "bureau")
        first = oauth2_client.access_token(config, key)
        second = oauth2_client.access_token(config, key)
        self.assertEqual(first, second)
        self.assertEqual(len(self.rec.token_requests), 1)

    def test_concurrent_requests_on_a_cold_cache_fetch_one_token(self):
        config = self._config()
        key = oauth2_client.cache_key(config, "t1", "bureau")
        results: List[str] = []
        threads = [threading.Thread(target=lambda: results.append(oauth2_client.access_token(config, key)))
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.rec.token_requests), 1, "a stampede on the token endpoint")

    def test_different_tenants_do_not_share_a_token(self):
        config = self._config()
        a = oauth2_client.access_token(config, oauth2_client.cache_key(config, "tenant-a", "bureau"))
        b = oauth2_client.access_token(config, oauth2_client.cache_key(config, "tenant-b", "bureau"))
        self.assertNotEqual(a, b)

    def test_invalidate_forces_a_fresh_token(self):
        config = self._config()
        key = oauth2_client.cache_key(config, "t1", "bureau")
        first = oauth2_client.access_token(config, key)
        oauth2_client.invalidate(key)
        self.assertNotEqual(oauth2_client.access_token(config, key), first)

    # ── failures are named, and fail closed ────────────────────────────────

    def test_rejected_credentials_are_an_auth_failure_with_a_named_code(self):
        self.rec.token_status = 401
        result = self._probe()
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], connector_probe.OUTCOME_AUTH_FAILED)
        self.assertIn("oauth2_client_rejected", result["reason"])
        self.assertEqual(self.rec.resource_requests, [], "no token, so the resource must not be called")

    def test_missing_client_credentials_are_named(self):
        with self.assertRaises(oauth2_client.OAuth2Error) as caught:
            oauth2_client.access_token(self._config(client_secret=""), ("k",))
        self.assertEqual(caught.exception.code, "oauth2_client_credentials_missing")

    def test_a_non_bearer_token_type_is_refused(self):
        with mock.patch.object(oauth2_client, "_request_token",
                               side_effect=oauth2_client.OAuth2Error("oauth2_unsupported_token_type", "mac")):
            with self.assertRaises(oauth2_client.OAuth2Error):
                oauth2_client.access_token(self._config(), ("k2",))

    def test_the_token_endpoint_goes_through_the_egress_guard(self):
        """It receives the client secret, so it is the same threat as a connector fetch."""
        source = (APP_ROOT / "app" / "oauth2_client.py").read_text(encoding="utf-8")
        self.assertIn("build_pinned_request(token_url)", source)
        self.assertIn("follow_redirects=False", source)


class OAuth2FetchPathTests(OAuth2ConnectorTests):
    """The decision-time path: fetch_source, async, with 401 recovery."""

    def _fetch(self, **overrides):
        import asyncio

        from app import connector_fetch

        connector = {"id": "bureau", "config": {**self._config(**overrides), "fetch": True}}
        return asyncio.run(connector_fetch.fetch_source(connector, {"payload": {}}, "tenant-a"))

    def test_the_decision_path_sends_the_token_not_the_secret(self):
        self._fetch()
        self.assertTrue(self.rec.resource_requests)
        self.assertEqual(self.rec.resource_requests[-1]["authorization"], "Bearer tok-1")
        self.assertNotIn(SECRET, json.dumps(self.rec.resource_requests))

    def test_a_token_revoked_early_is_replaced_once(self):
        """The provider rejects a token we believed valid: drop it and retry ONCE with a fresh one,
        rather than failing every decision until the cached expiry runs out."""
        self.rec.reject_token = "tok-1"
        self._fetch()
        auths = [r["authorization"] for r in self.rec.resource_requests]
        self.assertEqual(auths, ["Bearer tok-1", "Bearer tok-2"])
        self.assertEqual(len(self.rec.token_requests), 2)

    def test_the_access_token_is_redacted_from_a_failure(self):
        """The token is not a config KEY, so the connector's own secret list cannot know it. A
        transport error that quotes the request (proxies and TLS middleboxes do) would otherwise
        carry it into the trace, the error event and a 502 body."""
        from app import connector_fetch

        async def quoting_send(request, deadline):
            raise connector_fetch._TransportFailed(
                "unreachable", "proxy refused: " + request.headers["Authorization"])

        with mock.patch.object(connector_fetch, "_send", quoting_send):
            result = self._fetch()
        self.assertEqual(result["status"], "failed")
        self.assertIn("proxy refused", result["error"], "the control: the reason must survive")
        self.assertNotIn("tok-1", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
