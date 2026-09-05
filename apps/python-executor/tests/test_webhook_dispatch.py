"""Outbound webhook subscriptions: matching, signing, and guarded delivery (parity P03).

The product's only outbound call was the ad-hoc `alertUrl` on a monitor step: one URL wired into
one step of one policy, fired once, best-effort, exception swallowed into `result["alertError"]`.
Nothing could say "tell this URL whenever ANY policy in this workspace is promoted", nothing was
signed, and nothing retried.

The two properties worth the most attention here are both about NOT sending:

  - `event_matches` fails CLOSED in the delivery sense. An unparseable filter means "do not
    deliver", never "deliver everything". These payloads carry decision data to a third party, so
    over-delivery is the worse failure and a filter nobody can evaluate must not become a
    firehose. That is the opposite polarity from `connector_fetch`, where a failure must not let
    a decision proceed -- and the difference is the direction of the dependency.

  - `deliver` goes through the SSRF-guarded egress path. A subscription URL is author-supplied
    and the request body is workspace data, so it is the same threat model as a connector fetch:
    resolve, range-check, pin to the vetted IP, never follow a redirect.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import webhook_dispatch as wd  # noqa: E402
from app.auth import verify_hmac_signature  # noqa: E402


def subscription(**overrides):
    base = {
        "id": "sub_1",
        "url": "https://hooks.example.com/rulemind",
        "event_types": ["decision.rejected"],
        "filters": [],
        "secret": "shhh",
        "is_active": True,
        "timeout_ms": 2000,
        "retries": 0,
    }
    base.update(overrides)
    return base


def event(**overrides):
    base = {
        "type": "decision.rejected",
        "tenant_id": "t1",
        "created_at": "2026-09-05T00:00:00Z",
        "data": {"outcome": "reject", "policy_id": "pol_loan", "score": 410},
    }
    base.update(overrides)
    return base


class EventMatchingTest(unittest.TestCase):

    def test_a_subscribed_type_matches(self):
        self.assertTrue(wd.event_matches(subscription(), event()))

    def test_an_unsubscribed_type_does_not(self):
        self.assertFalse(wd.event_matches(subscription(), event(type="slo_breach")))

    def test_an_inactive_subscription_never_matches(self):
        self.assertFalse(wd.event_matches(subscription(is_active=False), event()))

    def test_filters_are_anded(self):
        sub = subscription(filters=[
            {"path": "data.policy_id", "operator": "==", "value": "pol_loan"},
            {"path": "data.score", "operator": "<", "value": 500},
        ])
        self.assertTrue(wd.event_matches(sub, event()))
        self.assertFalse(wd.event_matches(sub, event(data={"policy_id": "pol_loan", "score": 900})))

    def test_filters_use_logic_compare_not_a_second_operator_set(self):
        """`in`, `>=` and `exists` are logic.py's -- the set the five engines are held to by the
        parity corpus, and the one the rule-condition builder emits."""
        for condition, expected in (
            ({"path": "data.outcome", "operator": "in", "value": ["reject", "review"]}, True),
            ({"path": "data.score", "operator": ">=", "value": 400}, True),
            ({"path": "data.score", "operator": ">=", "value": 500}, False),
            ({"path": "data.missing", "operator": "exists"}, False),
        ):
            with self.subTest(condition=condition):
                self.assertEqual(
                    wd.event_matches(subscription(filters=[condition]), event()), expected)

    def test_a_bare_path_and_a_jsonpath_both_work(self):
        for path in ("data.outcome", "$.data.outcome"):
            with self.subTest(path=path):
                self.assertTrue(wd.event_matches(
                    subscription(filters=[{"path": path, "operator": "==", "value": "reject"}]),
                    event()))

    def test_a_malformed_filter_does_not_deliver(self):
        """Fails closed in the delivery sense. The temptation is to ignore a filter you cannot
        parse, which silently turns one subscription into a firehose of decision data."""
        for bad in ([{"operator": "=="}], ["not an object"], [{"path": ""}],
                    [{"path": "data.outcome", "operator": "≈", "value": "reject"}]):
            with self.subTest(bad=bad):
                self.assertFalse(wd.event_matches(subscription(filters=bad), event()))

    def test_a_filter_whose_comparison_RAISES_does_not_deliver(self):
        """The `except` guard, reached directly.

        The malformed-filter cases above are all caught by `compare` RETURNING False for input it
        does not like -- so they pass with the guard deleted, which a mutation proved. What the
        guard is actually for is a comparison that THROWS: a value type `compare` cannot coerce,
        or a future operator whose implementation raises on bad input. Mocking it is the honest
        way to reach that branch, because the point is that the branch exists at all.
        """
        with mock.patch.object(wd, "compare", side_effect=TypeError("not orderable")):
            self.assertFalse(wd.event_matches(
                subscription(filters=[{"path": "data.score", "operator": ">", "value": "abc"}]),
                event()))

    def test_a_raising_comparison_does_not_take_down_the_caller(self):
        """It returns False rather than propagating: dispatch walks every subscription, and one
        bad filter must not stop the others being evaluated."""
        with mock.patch.object(wd, "compare", side_effect=TypeError("not orderable")):
            wd.event_matches(subscription(filters=[{"path": "a", "operator": ">", "value": 1}]),
                             event())  # must not raise

    def test_no_filters_means_every_event_of_that_type(self):
        self.assertTrue(wd.event_matches(subscription(filters=[]), event()))
        self.assertTrue(wd.event_matches(subscription(filters=None), event()))


class SubscriptionValidationTest(unittest.TestCase):
    """Rejected at authoring time rather than stored and silently inert."""

    def test_an_unknown_event_type_is_refused(self):
        with self.assertRaises(wd.SubscriptionInvalid) as caught:
            wd.validate_event_types(["decision.rejcted"])
        self.assertIn("unknown event type", str(caught.exception))

    def test_the_error_names_the_valid_choices(self):
        """A closed list is only kinder than free text if the error says what the list is."""
        with self.assertRaises(wd.SubscriptionInvalid) as caught:
            wd.validate_event_types(["nope"])
        self.assertIn("decision.rejected", str(caught.exception))

    def test_an_empty_selection_is_refused(self):
        for raw in ([], None, "decision.rejected"):
            with self.subTest(raw=raw):
                with self.assertRaises(wd.SubscriptionInvalid):
                    wd.validate_event_types(raw)

    def test_duplicates_are_collapsed(self):
        self.assertEqual(
            wd.validate_event_types(["slo_breach", "slo_breach"]), ["slo_breach"])

    def test_a_filter_without_a_path_is_refused(self):
        with self.assertRaises(wd.SubscriptionInvalid):
            wd.validate_filters([{"operator": "==", "value": 1}])

    def test_filters_default_to_empty_rather_than_failing(self):
        self.assertEqual(wd.validate_filters(None), [])
        self.assertEqual(wd.validate_filters([]), [])

    def test_the_operator_defaults_to_equality(self):
        self.assertEqual(wd.validate_filters([{"path": "data.outcome", "value": "reject"}])[0]["operator"], "==")


class SigningTest(unittest.TestCase):
    """One HMAC scheme in both directions: `webhooks.py` VERIFIES inbound with the same helper,
    so a partner integrating both ways writes one implementation."""

    def test_the_signature_verifies_with_the_inbound_verifier(self):
        request = wd.build_request(subscription(), event(), "dlv_1")
        self.assertTrue(verify_hmac_signature(
            "shhh", request["body"], request["headers"][wd.SIGNATURE_HEADER]))

    def test_a_wrong_secret_does_not_verify(self):
        request = wd.build_request(subscription(), event(), "dlv_1")
        self.assertFalse(verify_hmac_signature(
            "other", request["body"], request["headers"][wd.SIGNATURE_HEADER]))

    def test_the_signature_covers_the_exact_bytes_sent(self):
        """Signing a re-serialised copy is how a receiver rejects a valid message: json.dumps is
        not guaranteed to reproduce byte-identical output once ordering or separators differ."""
        request = wd.build_request(subscription(), event(), "dlv_1")
        reserialised = json.dumps(json.loads(request["body"])).encode("utf-8")
        self.assertNotEqual(reserialised, request["body"],
                            "the fixture no longer distinguishes the two encodings")
        self.assertFalse(verify_hmac_signature(
            "shhh", reserialised, request["headers"][wd.SIGNATURE_HEADER]))

    def test_an_unsigned_subscription_sends_no_signature_header(self):
        request = wd.build_request(subscription(secret=""), event(), "dlv_1")
        self.assertNotIn(wd.SIGNATURE_HEADER, request["headers"])

    def test_the_delivery_carries_its_type_and_id_for_dedupe(self):
        request = wd.build_request(subscription(), event(), "dlv_42")
        self.assertEqual(request["headers"][wd.EVENT_HEADER], "decision.rejected")
        self.assertEqual(request["headers"][wd.DELIVERY_HEADER], "dlv_42")
        self.assertEqual(json.loads(request["body"])["id"], "dlv_42")

    def test_the_body_carries_the_event_and_nothing_undeclared(self):
        body = json.loads(wd.build_request(subscription(), event(), "dlv_1")["body"])
        self.assertEqual(sorted(body), ["created_at", "data", "id", "tenant_id", "type"])
        self.assertEqual(body["data"]["policy_id"], "pol_loan")


class _Pinned:
    url = "https://93.184.216.34/rulemind"
    host_header = "hooks.example.com"
    sni_hostname = "hooks.example.com"


class _Response:
    def __init__(self, status):
        self.status_code = status
        self.text = ""


def _client_returning(*statuses, record=None):
    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self_inner):
            return self_inner
        async def __aexit__(self_inner, *a):
            return False
        async def post(self_inner, url, **kwargs):
            if record is not None:
                record.append({"url": url, **kwargs})
            status = statuses[min(calls["n"], len(statuses) - 1)]
            calls["n"] += 1
            if isinstance(status, Exception):
                raise status
            return _Response(status)

    _Client.calls = calls
    return _Client


class DeliveryTest(unittest.TestCase):

    def deliver(self, sub, client, resolve=None):
        with mock.patch.object(wd, "build_pinned_request",
                               resolve or mock.Mock(return_value=_Pinned())):
            with mock.patch.object(wd.httpx, "AsyncClient", client):
                return asyncio.run(wd.deliver(sub, event(), "dlv_1"))

    def test_a_2xx_is_delivered(self):
        result = self.deliver(subscription(), _client_returning(200))
        self.assertTrue(result["delivered"])
        self.assertEqual(result["attempts"], 1)

    def test_a_5xx_is_retried_then_reported(self):
        client = _client_returning(500)
        result = self.deliver(subscription(retries=2), client)
        self.assertFalse(result["delivered"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(client.calls["n"], 3)

    def test_a_retry_that_succeeds_stops_retrying(self):
        client = _client_returning(500, 200)
        result = self.deliver(subscription(retries=2), client)
        self.assertTrue(result["delivered"])
        self.assertEqual(client.calls["n"], 2)

    def test_a_transport_error_is_a_report_not_an_exception(self):
        """Every caller is either a fire-and-forget hook on the decision path or a scheduler
        drain. An exception in either turns "a subscriber is down" into "a decision failed" or
        "the drain stopped at the first bad row"."""
        result = self.deliver(subscription(), _client_returning(ConnectionError("refused")))
        self.assertFalse(result["delivered"])
        self.assertIn("refused", result["error"])

    def test_it_connects_to_the_vetted_ip_and_restores_the_host_header(self):
        seen = []
        self.deliver(subscription(), _client_returning(200, record=seen))
        self.assertEqual(seen[0]["url"], _Pinned.url, "connected to the name, not the vetted IP")
        self.assertEqual(seen[0]["headers"]["Host"], _Pinned.host_header)

    def test_a_blocked_target_is_refused_and_not_retried(self):
        """A blocked target is a configuration decision, not a transient failure -- retrying it
        just repeats the same refusal on a timer."""
        blocked = mock.Mock(side_effect=wd.EgressBlocked("127.0.0.1 resolves to non-public address"))
        client = _client_returning(200)
        result = self.deliver(subscription(retries=5), client, resolve=blocked)
        self.assertFalse(result["delivered"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(client.calls["n"], 0, "a blocked target was still contacted")

    def test_redirects_are_never_followed(self):
        """Load-bearing, not hygiene: a 3xx would connect to a host that never passed `vet_url`,
        undoing the pinning two lines above it. Asserted on the client CONSTRUCTION because a
        redirect chased inside httpx never surfaces as a separate call to assert on."""
        constructed = []

        class _RecordingClient:
            def __init__(self_inner, *a, **kwargs):
                constructed.append(kwargs)
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *a):
                return False
            async def post(self_inner, url, **kwargs):
                return _Response(200)

        self.deliver(subscription(), _RecordingClient)
        self.assertEqual(len(constructed), 1)
        self.assertIs(constructed[0].get("follow_redirects"), False,
                      "the delivery client follows redirects, which undoes the IP pinning")

    def test_a_subscription_with_no_url_never_opens_a_connection(self):
        client = _client_returning(200)
        result = self.deliver(subscription(url=""), client)
        self.assertFalse(result["delivered"])
        self.assertEqual(client.calls["n"], 0)

    def test_retries_are_clamped_by_the_platform_ceiling(self):
        client = _client_returning(500)
        result = self.deliver(subscription(retries=10 ** 6), client)
        self.assertLessEqual(result["attempts"], wd.clamp_retries(10 ** 6) + 1)


if __name__ == "__main__":
    unittest.main()
