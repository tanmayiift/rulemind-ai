"""The findings three adversarial reviewers raised against the P01 implementation.

Kept in its own module deliberately: every test here exists because a reviewer either reproduced
a fail-open in the shipped tree or applied a mutation that all 46 tests in
tests/test_connector_fetch.py survived. That is the provenance, and it is worth being able to
read it later.

  FAIL-CLOSED LENS
    - `_sources_in_use` walked only top-level `policy["steps"]`, so a connector step nested in a
      branch, a loop or a sub-workflow was never fetched. Nothing then recorded an error, so
      neither fail-closed guard fired and `_execute_connector` answered the step with
      `sample_payload` at status ok. Reproduced end-to-end as a live `approve`. A conditional
      pull -- "only hit the bureau when amount > X" -- is the ordinary reason a connector step
      sits inside a branch, so this was the common case, not the exotic one.
    - An unmapped 200 whose body is `{}` became a SUCCESSFUL fetch with an empty payload: every
      variable of the source goes None, the gate flips, and no guard fires.
    - Every test stubbed `_send`, so `_classify_status` and the rest of the transport were
      unpinned: "a non-2xx is a failed fetch" could be deleted with all 46 still green.

  BUDGET LENS
    - `build_pinned_request` (blocking `socket.getaddrinfo`, no timeout of its own) ran BEFORE
      the deadline was consulted, so the clamp bounded the connect and left resolution unbounded:
      4013ms measured against a 300ms budget, while blocking the event loop.
    - Every budget assertion checked a helper's return value or that a deadline object was
      passed. None measured elapsed time: injecting `sleep(2.0)` took the suite from 0.3s to
      62.6s with 46/46 still passing.
    - A budget that expired is OUR clock, and it was being charged to the downstream's health --
      five decisions against an endpoint answering 200 drove the breaker open.

  SECURITY LENS
    - `_Request.secrets` held `resolved | connector_secret_values(connector)` and was never read.
      `{{secrets.*}}` resolves against a namespace flattened across every connector in the
      tenant, so a value from ANOTHER connector reached the error string, the persisted `url`,
      the step trace and the 502 body unredacted.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.fast_decide as fast_decide  # noqa: E402
import app.main as app_main  # noqa: E402
from app import circuit_breaker, connector_fetch  # noqa: E402
from app.connector_probe import ConnectorMisconfigured  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402

from tests.test_connector_fetch import (  # noqa: E402
    FETCHED_SCORE, LIVE_SOURCE, SAMPLE_SCORE, SOURCE_ID, VARIABLE_CODE, VARIABLE_ID,
    _connector, _fetch_config, _raiser, _responder,
)


class _Fixture(unittest.TestCase):
    """Same shape as test_connector_fetch._Base + DecisionTimeFetchTest, but the POLICY is
    supplied per test — these are all about where the connector step sits."""

    def setUp(self):
        self.storage = app_main.storage
        connector_fetch.reset_cache()
        circuit_breaker.reset_all()
        self.tenant_id = self.storage.create_tenant("p01r-{0}".format(time.time_ns()))["id"]
        self.storage.create_variable(
            {"id": VARIABLE_ID, "name": "score", "category": "risk", "source_id": SOURCE_ID,
             "code": VARIABLE_CODE, "status": "prod"},
            tenant_id=self.tenant_id)

    def tearDown(self):
        fast_decide.invalidate(self.tenant_id)

    def connector(self, config=None):
        self.storage.create_connector(_connector(config or _fetch_config()), tenant_id=self.tenant_id)
        fast_decide.invalidate(self.tenant_id)

    def run_policy(self, steps, send=None, payload=None):
        policy = {"id": "p01r_policy", "name": "P01R", "status": "prod",
                  "steps": steps, "defaultOutcome": "review"}
        executor = PolicyExecutor(self.storage)
        run = executor.execute(policy, payload or {}, self.tenant_id, source=LIVE_SOURCE)
        if send is not None:
            with mock.patch.object(connector_fetch, "_send", send):
                return asyncio.run(run)
        return asyncio.run(run)


OUTCOME = {"id": "s_out", "type": "outcome", "ref_id": "approve"}
CONNECTOR_STEP = {"id": "s_conn", "type": "connector", "ref_id": SOURCE_ID}


class ANestedConnectorStepIsStillFetchedTest(_Fixture):
    """The reproduced fail-open. Each of these placements must behave exactly as the top-level
    one: the source is fetched, and when the fetch fails the decision does NOT approve on
    sample data."""

    def assert_not_decided_on_sample(self, ctx):
        self.assertNotEqual(
            ctx.variables.get(VARIABLE_ID), SAMPLE_SCORE,
            "the decision was made on sample_payload for a fetch-configured connector")
        self.assertEqual(ctx.outcome, "review", "a dead source produced a live approval")

    def test_inside_a_branch(self):
        self.connector()
        boom = connector_fetch._TransportFailed("timeout", "no response")
        ctx = self.run_policy([
            {"id": "s_br", "type": "branch", "config": {
                "branches": [{"condition": None, "steps": [CONNECTOR_STEP]}]}},
            OUTCOME,
        ], send=_raiser(boom))
        self.assert_not_decided_on_sample(ctx)

    def test_inside_a_branch_default(self):
        self.connector()
        boom = connector_fetch._TransportFailed("timeout", "no response")
        ctx = self.run_policy([
            {"id": "s_br", "type": "branch", "config": {
                "branches": [{"condition": "1 == 2", "steps": []}],
                "default": [CONNECTOR_STEP]}},
            OUTCOME,
        ], send=_raiser(boom))
        self.assert_not_decided_on_sample(ctx)

    def test_inside_a_loop(self):
        self.connector()
        boom = connector_fetch._TransportFailed("timeout", "no response")
        ctx = self.run_policy([
            {"id": "s_loop", "type": "loop", "config": {
                "over": "$.items", "as": "item", "steps": [CONNECTOR_STEP]}},
            OUTCOME,
        ], send=_raiser(boom))
        self.assert_not_decided_on_sample(ctx)

    def test_inside_a_sub_workflow(self):
        self.connector()
        self.storage.create_policy(
            {"id": "p01r_child", "name": "child", "status": "prod",
             "steps": [CONNECTOR_STEP], "defaultOutcome": "review"},
            tenant_id=self.tenant_id)
        boom = connector_fetch._TransportFailed("timeout", "no response")
        ctx = self.run_policy([
            {"id": "s_wf", "type": "workflow", "ref_id": "p01r_child"},
            OUTCOME,
        ], send=_raiser(boom))
        self.assert_not_decided_on_sample(ctx)

    def test_a_nested_step_is_fetched_at_all_not_merely_failed_safely(self):
        """The positive control. Without it every test above would pass just as happily against
        a build that refuses every nested connector outright."""
        self.connector()
        send = _responder()
        ctx = self.run_policy([
            {"id": "s_br", "type": "branch", "config": {
                "branches": [{"condition": None, "steps": [CONNECTOR_STEP]}]}},
            OUTCOME,
        ], send=send)
        self.assertEqual(len(send.calls), 1, "the nested connector step was never fetched")
        self.assertEqual(ctx.variables[VARIABLE_ID], FETCHED_SCORE)
        self.assertEqual(ctx.outcome, "approve")

    # The source above also has a VARIABLE, and `_sources_in_use` pulls in every computed
    # variable's source independently of the step walk -- so those tests pass with the walk
    # removed. The walk is the ONLY thing that reaches a source referenced purely by a connector
    # step, which is exactly the case the reviewer described: a step whose data a later step
    # reads, with no variable of its own.

    SIDECAR = "p01r_sidecar"

    def sidecar_connector(self):
        self.storage.create_connector(
            _connector(_fetch_config(base_url="https://sidecar.example.com/x"),
                       connector_id=self.SIDECAR),
            tenant_id=self.tenant_id)
        fast_decide.invalidate(self.tenant_id)

    def test_a_nested_source_with_no_variable_is_still_fetched(self):
        self.connector()
        self.sidecar_connector()
        send = _responder()
        self.run_policy([
            {"id": "s_br", "type": "branch", "config": {"branches": [{"condition": None, "steps": [
                {"id": "s_side", "type": "connector", "ref_id": self.SIDECAR}]}]}},
            OUTCOME,
        ], send=send)
        fetched = {call["request"].url for call in send.calls}
        self.assertTrue(
            any("sidecar.example.com" in url for url in fetched),
            "a connector step nested in a branch, whose source has no variable, was never "
            "fetched -- the step walk is flat again. Fetched: {0}".format(sorted(fetched)))

    def test_a_nested_source_with_no_variable_fails_the_decision_closed(self):
        self.connector()
        self.sidecar_connector()
        boom = connector_fetch._TransportFailed("timeout", "no response")
        ctx = self.run_policy([
            {"id": "s_br", "type": "branch", "config": {"branches": [{"condition": None, "steps": [
                {"id": "s_side", "type": "connector", "ref_id": self.SIDECAR}]}]}},
            OUTCOME,
        ], send=_raiser(boom))
        self.assertEqual(ctx.outcome, "review")

    def test_a_sub_workflows_own_source_with_no_variable_is_fetched(self):
        self.connector()
        self.sidecar_connector()
        self.storage.create_policy(
            {"id": "p01r_child2", "name": "child2", "status": "prod",
             "steps": [{"id": "s_side", "type": "connector", "ref_id": self.SIDECAR}],
             "defaultOutcome": "review"},
            tenant_id=self.tenant_id)
        send = _responder()
        self.run_policy([
            {"id": "s_wf", "type": "workflow", "ref_id": "p01r_child2"},
            OUTCOME,
        ], send=send)
        self.assertTrue(
            any("sidecar.example.com" in call["request"].url for call in send.calls),
            "a connector step inside a SUB-WORKFLOW was never fetched")

    def test_a_cycle_between_sub_workflows_does_not_hang_the_walk(self):
        """The collector follows `workflow` steps, so it needs the cycle guard the executor has.
        A spin here happens during payload assembly, before any budget check has run."""
        self.connector()
        for pid, other in (("p01r_a", "p01r_b"), ("p01r_b", "p01r_a")):
            self.storage.create_policy(
                {"id": pid, "name": pid, "status": "prod",
                 "steps": [{"id": "w", "type": "workflow", "ref_id": other}],
                 "defaultOutcome": "review"},
                tenant_id=self.tenant_id)
        executor = PolicyExecutor(self.storage)
        sources = executor._sources_in_use(
            {"steps": [{"id": "w", "type": "workflow", "ref_id": "p01r_a"}]}, self.tenant_id, None)
        self.assertIsInstance(sources, set)


class SampleDataIsGatedOnTheConnectorNotThePayloadTest(_Fixture):
    """Defence in depth for the above. The failure guard only fires when a fetch was ATTEMPTED;
    if a source is never reached at all there is no error to find, and the old `if not payload:`
    would answer a fetch-configured source with demo data anyway."""

    def test_a_fetch_configured_source_never_receives_sample_data(self):
        self.connector()
        executor = PolicyExecutor(self.storage)
        catalog = executor._catalog_for(self.tenant_id)
        connectors = {c["id"]: c for c in catalog["connectors"].values()} \
            if isinstance(catalog["connectors"], dict) else {}
        connectors = catalog["connectors"] if not connectors else connectors

        class _Ctx:
            payload = {}
            source_fetch_errors = {}
        result = executor._execute_connector(
            {"id": "s", "type": "connector", "ref_id": SOURCE_ID}, _Ctx(), connectors)
        self.assertEqual(result["status"], "failed",
                         "a fetch-configured source with no retrieved data reported ok")
        self.assertEqual(result["payload_keys"], [])

    def test_a_connector_that_never_opted_in_still_gets_its_sample(self):
        """The behaviour that must NOT change: on the authoring surfaces this pre-fill is what
        the test console relies on."""
        self.connector({"base_url": "https://p01.example.com/score"})  # no fetch_at_decision
        executor = PolicyExecutor(self.storage)
        catalog = executor._catalog_for(self.tenant_id)

        class _Ctx:
            payload = {}
            source_fetch_errors = {}
        result = executor._execute_connector(
            {"id": "s", "type": "connector", "ref_id": SOURCE_ID}, _Ctx(), catalog["connectors"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["payload_keys"], ["score"])


class AnEmptyBodyIsNotASuccessfulFetchTest(unittest.TestCase):
    """A 200 carrying `{}` -- a partner's "no record found", a stub endpoint, an emptied error
    envelope. Returning it makes every variable of the source None, which is the fail-open in a
    success costume. The mapped path already refused an all-None result; the identity path did
    not, and the identity path is the one an author gets by leaving response_mapping blank."""

    def test_an_unmapped_empty_object_is_refused(self):
        with self.assertRaises(ConnectorMisconfigured) as caught:
            connector_fetch.apply_response_mapping(None, "{}")
        self.assertEqual(caught.exception.code, "empty_response_body")

    def test_an_unmapped_non_empty_object_still_passes(self):
        self.assertEqual(
            connector_fetch.apply_response_mapping(None, '{"score": 7}'), {"score": 7})

    def test_a_mapped_response_that_matches_nothing_is_still_refused(self):
        with self.assertRaises(ConnectorMisconfigured) as caught:
            connector_fetch.apply_response_mapping('{"score": "$.data.score"}', '{"other": 1}')
        self.assertEqual(caught.exception.code, "response_did_not_match_mapping")


class TheTransportIsActuallyTestedTest(unittest.TestCase):
    """Six mutations inside `_send` survived all 46 tests because every one of them stubbed
    `_send` itself. These drive the real function with the egress guard and the HTTP client
    stubbed one layer lower."""

    def setUp(self):
        circuit_breaker.reset_all()

    def _request(self, timeout_ms=2000, retries=0):
        return connector_fetch._Request(
            method="GET", url="https://p01.example.com/score", headers={},
            body=None, timeout_ms=timeout_ms, retries=retries, fingerprint="fp-test",
            display_url="https://p01.example.com/score", secrets=set())

    class _Pinned:
        url = "https://93.184.216.34/score"
        host_header = "p01.example.com"
        sni_hostname = "p01.example.com"

    def _send_with(self, response=None, error=None, request=None):
        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *a):
                return False
            async def request(self_inner, *a, **k):
                if error is not None:
                    raise error
                return response
        with mock.patch.object(connector_fetch, "build_pinned_request", return_value=self._Pinned()):
            with mock.patch.object(connector_fetch.httpx, "AsyncClient", _Client):
                return asyncio.run(connector_fetch._send(request or self._request(), None))

    class _Response:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

    def test_a_2xx_returns_the_body(self):
        status, text = self._send_with(self._Response(200, '{"data": {"score": 300}}'))
        self.assertEqual(status, 200)
        self.assertIn("300", text)

    def test_a_500_with_a_well_formed_body_is_a_failed_fetch(self):
        """Not "any response is data". A 500 whose body happens to parse is still a failure, and
        this is one of the mutations that survived: deleting the status check left 46/46 green."""
        with self.assertRaises(connector_fetch._TransportFailed):
            self._send_with(self._Response(500, '{"data": {"score": 300}}'))

    def test_a_401_is_a_failed_fetch(self):
        with self.assertRaises(connector_fetch._TransportFailed):
            self._send_with(self._Response(401, '{"error": "bad key"}'))

    def test_a_302_is_a_failed_fetch_rather_than_being_followed(self):
        """follow_redirects=False is load-bearing: chasing a 3xx connects to a host that never
        went through vet_url, undoing the pinning."""
        with self.assertRaises(connector_fetch._TransportFailed):
            self._send_with(self._Response(302, ""))

    def test_the_pinned_url_is_what_is_connected_to_and_host_is_restored(self):
        seen = {}

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *a):
                return False
            async def request(self_inner, method, url, **kwargs):
                seen["url"] = url
                seen["headers"] = kwargs.get("headers")
                return TheTransportIsActuallyTestedTest._Response(200, '{"a": 1}')

        with mock.patch.object(connector_fetch, "build_pinned_request", return_value=self._Pinned()):
            with mock.patch.object(connector_fetch.httpx, "AsyncClient", _Client):
                asyncio.run(connector_fetch._send(self._request(), None))
        self.assertEqual(seen["url"], self._Pinned.url, "connected to the name, not the vetted IP")
        self.assertEqual(seen["headers"]["Host"], self._Pinned.host_header)

    def test_a_body_larger_than_the_cap_is_truncated(self):
        big = '{"pad": "' + ("x" * (connector_fetch.MAX_BODY_BYTES + 5000)) + '"}'
        _, text = self._send_with(self._Response(200, big))
        self.assertLessEqual(len(text), connector_fetch.MAX_BODY_BYTES)

    def test_an_exhausted_budget_starts_no_request_at_all(self):
        deadline = time.perf_counter() - 1
        with mock.patch.object(connector_fetch, "build_pinned_request") as resolve:
            with self.assertRaises(connector_fetch._TransportFailed) as caught:
                asyncio.run(connector_fetch._send(self._request(), deadline))
        resolve.assert_not_called()
        self.assertEqual(caught.exception.outcome, "timeout")
        self.assertIn("budget", caught.exception.reason)


class NameResolutionIsInsideTheBudgetTest(unittest.TestCase):
    """The budget lens's headline finding, and it needs a WALL-CLOCK assertion: every previous
    budget test checked a helper's return value or that a deadline was passed along, and a
    `sleep(2.0)` injected into the fetch left all 46 passing while the suite ran 190x longer."""

    def _request(self):
        return connector_fetch._Request(
            method="GET", url="https://p01.example.com/score", headers={}, body=None,
            timeout_ms=8000, retries=0, fingerprint="fp-test",
            display_url="https://p01.example.com/score", secrets=set())

    def test_a_slow_resolver_cannot_outrun_the_remaining_budget(self):
        """Timed INSIDE the loop, deliberately.

        asyncio.wait_for cancels the awaiting task but cannot kill the worker thread --
        getaddrinfo runs to completion in the executor either way, and asyncio.run() then waits
        for it during shutdown. That is a property of the platform, not of this fix, and timing
        around asyncio.run() would measure it instead of the thing under test. What the budget
        promises is that the DECISION stops waiting, so that is what is measured: how long
        `_send` takes to raise, from inside the loop.
        """
        def glacial_resolve(url):
            time.sleep(3.0)
            raise AssertionError("resolution should have been abandoned")

        outcome = {}

        async def scenario():
            deadline = time.perf_counter() + 0.30
            started = time.perf_counter()
            with mock.patch.object(connector_fetch, "build_pinned_request", glacial_resolve):
                try:
                    await connector_fetch._send(self._request(), deadline)
                except connector_fetch._TransportFailed as failed:
                    outcome["error"] = failed
            outcome["elapsed_ms"] = (time.perf_counter() - started) * 1000

        asyncio.run(scenario())
        self.assertIn("error", outcome, "a 3s resolve against a 300ms budget did not fail")
        self.assertLess(
            outcome["elapsed_ms"], 1500,
            "the fetch waited out a 3s resolve against a 300ms budget ({0:.0f}ms) -- "
            "resolution is outside the budget again".format(outcome["elapsed_ms"]))
        self.assertEqual(outcome["error"].outcome, "timeout")

    def test_resolution_does_not_block_the_event_loop(self):
        """It is synchronous and it is on the async path, so a bare call stalls every OTHER
        request in the worker, not just this decision."""
        ticks = []

        def slow_resolve(url):
            time.sleep(0.40)
            raise AssertionError("should have been abandoned")

        async def scenario():
            async def heartbeat():
                for _ in range(20):
                    await asyncio.sleep(0.02)
                    ticks.append(1)
            beat = asyncio.create_task(heartbeat())
            with mock.patch.object(connector_fetch, "build_pinned_request", slow_resolve):
                try:
                    await connector_fetch._send(self._request(), time.perf_counter() + 0.25)
                except connector_fetch._TransportFailed:
                    pass
            beat.cancel()

        asyncio.run(scenario())
        self.assertGreater(
            len(ticks), 3,
            "the event loop made no progress during name resolution ({0} ticks) -- it is "
            "blocking again".format(len(ticks)))


class TheBreakerIsNotChargedForOurOwnClockTest(_Fixture):
    """A tenant with a tight evaluation_budget_ms was tripping the breaker on a partner that
    answered every request correctly -- and the breaker then withholds a healthy integration
    from every other decision in the workspace."""

    def test_a_budget_timeout_does_not_trip_the_breaker(self):
        self.connector()
        expired = connector_fetch._TransportFailed(
            "timeout", "evaluation budget exhausted before the fetch could run")
        for _ in range(6):
            self.run_policy([CONNECTOR_STEP, OUTCOME], send=_raiser(expired))
        breaker = circuit_breaker.get_breaker(
            "connector_fetch:{0}:{1}".format(self.tenant_id, SOURCE_ID))
        self.assertNotEqual(
            breaker.state, circuit_breaker.OPEN,
            "our own expired budget was counted against the downstream's health")

    def test_a_real_downstream_failure_still_trips_it(self):
        """The control: without this the test above passes against a build that never records
        a failure at all."""
        self.connector()
        boom = connector_fetch._TransportFailed("connect_error", "connection refused")
        for _ in range(6):
            self.run_policy([CONNECTOR_STEP, OUTCOME], send=_raiser(boom))
        breaker = circuit_breaker.get_breaker(
            "connector_fetch:{0}:{1}".format(self.tenant_id, SOURCE_ID))
        self.assertEqual(breaker.state, circuit_breaker.OPEN)


class CrossConnectorSecretsAreRedactedTest(unittest.TestCase):
    """`{{secrets.*}}` resolves against a namespace flattened across every connector in the
    tenant, so a bureau connector's request_mapping can carry the PAYMENTS connector's token.
    `redact_connector_secrets(x, connector)` alone masks only THIS connector's secret-named keys,
    so that value passed through untouched -- and not only in `error`: `url` rides into the
    persisted execution, the error event, the step trace and the 502 body."""

    OTHER_SECRET = "OTHER-CONNECTOR-TOKEN-9f2b"

    def _failure(self, **extra):
        connector = _connector(_fetch_config())
        return connector_fetch._failure(
            connector, SOURCE_ID, "connect_error",
            "GET https://p01.example.com/score?token={0} failed".format(self.OTHER_SECRET),
            time.perf_counter(),
            secrets={self.OTHER_SECRET},
            **extra)

    def test_a_secret_from_another_connector_is_redacted_from_the_error(self):
        self.assertNotIn(self.OTHER_SECRET, self._failure()["error"])

    def test_it_is_redacted_from_the_url_too_not_only_the_error(self):
        result = self._failure(
            url="https://p01.example.com/score?token={0}".format(self.OTHER_SECRET),
            method="GET")
        self.assertNotIn(self.OTHER_SECRET, str(result["url"]))
        self.assertNotIn(self.OTHER_SECRET, str(result))

    def test_the_trace_entry_is_redacted_with_the_same_set(self):
        result = self._failure(
            url="https://p01.example.com/score?token={0}".format(self.OTHER_SECRET),
            method="GET")
        entry = connector_fetch.trace_entry(
            result, _connector(_fetch_config()), secrets={self.OTHER_SECRET})
        self.assertNotIn(self.OTHER_SECRET, str(entry))

    def test_the_connectors_own_secret_is_still_redacted(self):
        """The pre-existing behaviour, kept: this is what passed before and must keep passing."""
        connector = _connector(_fetch_config())
        result = connector_fetch._failure(
            connector, SOURCE_ID, "connect_error",
            "auth failed with SUPERSECRET123", time.perf_counter())
        self.assertNotIn("SUPERSECRET123", result["error"])


if __name__ == "__main__":
    unittest.main()
