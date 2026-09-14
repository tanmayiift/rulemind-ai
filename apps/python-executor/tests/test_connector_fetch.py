"""Decision-time connector fetch (parity P01): it really fetches, and it fails CLOSED.

WHAT WAS BROKEN. `PolicyExecutor._execute_connector` was eight lines that never opened a socket.
When the caller supplied no data for a source it substituted that connector's `sample_payload`
and returned `{"status": "ok"}`. A sample_payload is a healthy, approving example, so a policy
whose bureau integration was dead did not fail -- it decided on demo data at HTTP 200. The
fail-open was not "no feature", it was "a wrong answer with a clean audit trail".

The single most important assertion in this file is therefore negative:
`test_a_failed_fetch_does_not_substitute_sample_data`. Everything else is scaffolding for it.

Egress note, same as tests/test_action_polling.py: `build_pinned_request` refuses loopback
("127.0.0.1 resolves to non-public address"), so these cannot point at a local HTTP server. The
seam is `connector_fetch._send` -- egress guard, connect, retry -- and the tests that are not
ABOUT egress stub it. The egress test deliberately does not, and uses a loopback URL to prove
the guard is on this path at all.
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
from app import circuit_breaker, connector_fetch, egress  # noqa: E402
from app.connector_probe import ConnectorMisconfigured  # noqa: E402
from app.executor import EvaluationBudgetExceeded, PolicyExecutor  # noqa: E402

SOURCE_ID = "p01src"
VARIABLE_ID = "p01_score"

# The demo value. If it ever shows up in a decision, sample substitution came back.
SAMPLE_SCORE = 999
# What the (stubbed) downstream actually answers with.
FETCHED_SCORE = 300

VARIABLE_CODE = (
    '@variable(source="{0}")\n'
    "def {1}(payload, variables, apis):\n"
    "    return int(payload.get(\"score\") or 0)\n"
).format(SOURCE_ID, VARIABLE_ID)

# A production decision source: not in _NO_FETCH_SOURCES (so it fetches) and not in
# _SHARED_CATALOG_SOURCES (so the catalog is read from the DB and a connector edit made by a
# test is visible immediately rather than after the 5s cross-request TTL).
LIVE_SOURCE = "webhook"


def _connector(config, connector_id=SOURCE_ID):
    return {
        "id": connector_id,
        "name": "P01 source",
        "schema_paths": ["score"],
        "sample_payload": {"score": SAMPLE_SCORE},
        "is_active": True,
        "config": config,
    }


def _fetch_config(**overrides):
    config = {
        "fetch_at_decision": True,
        "auth_type": "api_key",
        "base_url": "https://p01.example.com/score",
        "api_key": "SUPERSECRET123",
        "response_mapping": '{"score": "$.data.score"}',
        "timeout_ms": 2000,
    }
    config.update(overrides)
    return config


def _responder(body='{"data": {"score": 300}}', status=200):
    calls = []

    async def _send(request, deadline):
        calls.append({"request": request, "deadline": deadline})
        return status, body

    _send.calls = calls  # type: ignore[attr-defined]
    return _send


def _raiser(exc):
    async def _send(request, deadline):
        raise exc
    return _send


class _Base(unittest.TestCase):
    def setUp(self):
        self.storage = app_main.storage
        connector_fetch.reset_cache()
        circuit_breaker.reset_all()


# ── C2: the two mappings ───────────────────────────────────────────────────
class RequestMappingTest(_Base):
    """request_mapping: the decision payload -> the outbound request."""

    def _build(self, config, view=None):
        return connector_fetch._build_request(_connector(config), view or {"payload": {}, "secrets": {}})

    def test_absent_mapping_is_a_get_to_base_url_with_the_connector_credential(self):
        request = self._build(_fetch_config())
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url, "https://p01.example.com/score")
        self.assertIsNone(request.body)
        self.assertEqual(request.headers["X-API-Key"], "SUPERSECRET123")

    def test_path_query_and_body_are_templated_from_the_decision_context(self):
        request = self._build(
            _fetch_config(request_mapping=(
                '{"path": "/v2/{{payload.custom.pan}}",'
                ' "query": {"consent": "{{payload.custom.consent}}"},'
                ' "body": {"pan": "{{payload.custom.pan}}"}}')),
            view={"payload": {"custom": {"pan": "ABCDE1234F", "consent": "c-9"}}, "secrets": {}},
        )
        self.assertEqual(request.method, "POST", "a mapping with a body is not a GET")
        self.assertEqual(request.url, "https://p01.example.com/score/v2/ABCDE1234F?consent=c-9")
        self.assertEqual(request.body, {"pan": "ABCDE1234F"})

    def test_a_leading_slash_in_path_does_not_discard_the_base_url_path(self):
        """urljoin would turn ("https://h/v2/", "/score") into "https://h/score" -- silently
        pointing a bureau pull at a different endpoint than the author configured."""
        request = self._build(_fetch_config(
            base_url="https://p01.example.com/v2/", request_mapping='{"path": "/score"}'))
        self.assertEqual(request.url, "https://p01.example.com/v2/score")

    def test_malformed_request_mapping_raises_rather_than_being_ignored(self):
        for bad in ('{"path": ', '["not", "an", "object"]', '{"method": "DELETE"}', '{"query": 7}'):
            with self.subTest(mapping=bad):
                with self.assertRaises(ConnectorMisconfigured):
                    self._build(_fetch_config(request_mapping=bad))

    def test_author_tunables_are_clamped_to_the_platform_ceiling(self):
        request = self._build(_fetch_config(timeout_ms=600000, retries=99))
        self.assertEqual(request.timeout_ms, connector_fetch.FETCH_MAX_TIMEOUT_MS)
        self.assertEqual(request.retries, connector_fetch.FETCH_MAX_RETRIES)


class ResponseMappingTest(_Base):
    """response_mapping: the response -> the source payload the variables read."""

    def test_flat_field_to_jsonpath(self):
        payload = connector_fetch.apply_response_mapping(
            '{"score": "$.data.score", "hits": "$.data.enquiries[0].count"}',
            '{"data": {"score": 712, "enquiries": [{"count": 3}]}}')
        self.assertEqual(payload, {"score": 712, "hits": 3})

    def test_a_bare_path_is_accepted_like_a_rule_condition_writes_it(self):
        self.assertEqual(
            connector_fetch.apply_response_mapping('{"score": "data.score"}', '{"data": {"score": 5}}'),
            {"score": 5})

    def test_absent_mapping_uses_the_body_verbatim(self):
        self.assertEqual(connector_fetch.apply_response_mapping(None, '{"score": 41}'), {"score": 41})

    def test_malformed_mapping_fails_closed(self):
        cases = [
            ("unparseable json", "{not json", '{"a": 1}'),
            ("not an object", "[1,2]", '{"a": 1}'),
            ("path is not a string", '{"score": 7}', '{"a": 1}'),
            ("response is not json", '{"score": "$.a"}', "<html>502</html>"),
            ("response is not an object and nothing reshapes it", None, "[1,2,3]"),
            ("mapping matches nothing in the response", '{"score": "$.data.score"}', '{"other": 1}'),
        ]
        for label, mapping, body in cases:
            with self.subTest(case=label):
                with self.assertRaises(ConnectorMisconfigured):
                    connector_fetch.apply_response_mapping(mapping, body)

    def test_a_partly_matching_mapping_is_allowed_but_an_empty_one_is_not(self):
        """One optional field missing is normal; EVERY field missing means the mapping does not
        describe this response, and a payload of Nones is the fail-open, not a result."""
        payload = connector_fetch.apply_response_mapping(
            '{"score": "$.data.score", "band": "$.data.band"}', '{"data": {"score": 700}}')
        self.assertEqual(payload, {"score": 700, "band": None})


# ── C3: cache ──────────────────────────────────────────────────────────────
class CacheTest(_Base):
    """The tenant-isolation case here is the one that is a breach and not a bug."""

    def _fetch(self, tenant_id, body='{"data": {"score": 300}}', config=None, send=None):
        connector = _connector(config or _fetch_config(cache_ttl_ms=60000))
        send = send or _responder(body)
        with mock.patch.object(connector_fetch, "_send", send):
            result = asyncio.run(connector_fetch.fetch_source(
                connector, {"payload": {}, "secrets": {}}, tenant_id))
        return result, send

    def test_a_second_identical_fetch_is_served_from_cache(self):
        first, send_a = self._fetch("tenant-a")
        second, send_b = self._fetch("tenant-a")
        self.assertEqual(first["payload"], {"score": 300})
        self.assertTrue(second["cached"])
        self.assertEqual(second["payload"], {"score": 300})
        self.assertEqual(len(send_b.calls), 0, "the second fetch opened a connection anyway")

    def test_the_cache_is_not_shared_across_tenants(self):
        """Connector ids are seeded identically for every tenant ("bureau", "bank", "kyc"), so a
        cache keyed by connector id alone would hand tenant B the report pulled for tenant A."""
        self._fetch("tenant-a", body='{"data": {"score": 300}}')
        second, send_b = self._fetch("tenant-b", body='{"data": {"score": 811}}')
        self.assertFalse(second["cached"], "tenant B was served tenant A's cached response")
        self.assertEqual(second["payload"], {"score": 811})
        self.assertEqual(len(send_b.calls), 1)

    def test_the_cache_is_not_shared_across_subjects(self):
        """The URL and body carry the applicant. Two decisions about different people must not
        collide -- that is applicant A's bureau report answering applicant B's application."""
        config = _fetch_config(cache_ttl_ms=60000, request_mapping='{"path": "/{{payload.custom.pan}}"}')
        connector = _connector(config)
        send = _responder('{"data": {"score": 300}}')
        with mock.patch.object(connector_fetch, "_send", send):
            asyncio.run(connector_fetch.fetch_source(
                connector, {"payload": {"custom": {"pan": "AAAAA1111A"}}, "secrets": {}}, "t"))
            second = asyncio.run(connector_fetch.fetch_source(
                connector, {"payload": {"custom": {"pan": "BBBBB2222B"}}, "secrets": {}}, "t"))
        self.assertFalse(second["cached"])
        self.assertEqual(len(send.calls), 2)

    def test_an_entry_expires(self):
        config = _fetch_config(cache_ttl_ms=1)
        self._fetch("tenant-a", config=config)
        time.sleep(0.02)
        second, send_b = self._fetch("tenant-a", config=config, body='{"data": {"score": 411}}')
        self.assertFalse(second["cached"])
        self.assertEqual(second["payload"], {"score": 411})

    def test_caching_is_off_unless_the_connector_asks_for_it(self):
        first, _ = self._fetch("tenant-a", config=_fetch_config())
        second, send_b = self._fetch("tenant-a", config=_fetch_config())
        self.assertFalse(second["cached"])
        self.assertEqual(len(send_b.calls), 1)

    def test_the_ttl_is_clamped_and_garbage_means_do_not_cache(self):
        self.assertEqual(egress.clamp_cache_ttl_ms(10**9), egress.MAX_CACHE_TTL_MS)
        self.assertEqual(egress.clamp_cache_ttl_ms("soon"), 0)
        self.assertEqual(egress.clamp_cache_ttl_ms(-5), 0)
        self.assertEqual(connector_fetch.cache_ttl_ms(_connector(_fetch_config(cache_ttl_ms=10**9))),
                         egress.MAX_CACHE_TTL_MS)

    def test_a_cached_payload_cannot_be_mutated_through(self):
        first, _ = self._fetch("tenant-a")
        first["payload"]["score"] = "tampered"
        second, _ = self._fetch("tenant-a")
        self.assertEqual(second["payload"], {"score": 300})


# ── C3: budget ─────────────────────────────────────────────────────────────
class BudgetTest(_Base):
    def test_each_attempt_is_clamped_to_the_remaining_budget(self):
        deadline = time.perf_counter() + 0.05
        clamped = connector_fetch._attempt_timeout_s(8000, deadline)
        self.assertIsNotNone(clamped)
        self.assertLessEqual(clamped, 0.05)

    def test_no_budget_means_the_connector_timeout_stands(self):
        self.assertEqual(connector_fetch._attempt_timeout_s(2000, None), 2.0)

    def test_an_exhausted_budget_starts_no_attempt(self):
        self.assertIsNone(connector_fetch._attempt_timeout_s(2000, time.perf_counter() - 1))

    def test_a_fetch_with_no_budget_left_fails_instead_of_running(self):
        connector = _connector(_fetch_config(base_url="https://p01.example.com/score"))
        result = asyncio.run(connector_fetch.fetch_source(
            connector, {"payload": {}, "secrets": {}}, "t", deadline=time.perf_counter() - 1))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["payload"])


# ── C1: egress + redaction ─────────────────────────────────────────────────
class EgressAndRedactionTest(_Base):
    def test_a_loopback_target_is_refused_and_never_connects(self):
        """Not stubbed: this is the test that the SSRF guard is on the decision path at all."""
        connector = _connector(_fetch_config(base_url="http://127.0.0.1:9/pull"))
        result = asyncio.run(connector_fetch.fetch_source(
            connector, {"payload": {}, "secrets": {}}, "t"))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["payload"])
        self.assertIn("non-public address", result["error"])

    def test_a_secret_echoed_back_in_an_error_is_redacted(self):
        connector = _connector(_fetch_config())
        boom = connector_fetch._TransportFailed(
            "unreachable", "connection failed: https://p01.example.com/score?k=SUPERSECRET123")
        with mock.patch.object(connector_fetch, "_send", _raiser(boom)):
            result = asyncio.run(connector_fetch.fetch_source(
                connector, {"payload": {}, "secrets": {}}, "t"))
        self.assertNotIn("SUPERSECRET123", result["error"])
        self.assertIn("***REDACTED***", result["error"])

    def test_the_trace_entry_carries_key_names_not_the_customer_payload(self):
        connector = _connector(_fetch_config())
        with mock.patch.object(connector_fetch, "_send", _responder()):
            result = asyncio.run(connector_fetch.fetch_source(
                connector, {"payload": {}, "secrets": {}}, "t"))
        entry = connector_fetch.trace_entry(result, connector)
        self.assertNotIn("payload", entry)
        self.assertEqual(entry["payload_keys"], ["score"])


class OptInTest(_Base):
    def test_a_connector_without_the_flag_does_not_fetch(self):
        self.assertFalse(connector_fetch.fetch_enabled(_connector({"base_url": "https://x.example"})))

    def test_the_flag_is_read_from_free_text_and_anything_unrecognised_is_off(self):
        for raw, expected in (("true", True), ("1", True), ("yes", True),
                              ("false", False), ("", False), ("maybe", False), (None, False)):
            with self.subTest(raw=raw):
                self.assertIs(
                    connector_fetch.fetch_enabled(_connector({"fetch_at_decision": raw})), expected)

    def test_an_unrecognised_failure_mode_falls_back_to_the_safe_one(self):
        self.assertEqual(connector_fetch.on_fetch_failure(_connector(_fetch_config())), "review_gate")
        self.assertEqual(
            connector_fetch.on_fetch_failure(_connector(_fetch_config(on_fetch_failure="contnue"))),
            "review_gate")
        self.assertEqual(
            connector_fetch.on_fetch_failure(_connector(_fetch_config(on_fetch_failure="continue"))),
            "continue")


# ── C1 end to end, through the executor ────────────────────────────────────
class DecisionTimeFetchTest(_Base):
    """Full path: storage -> PolicyExecutor.execute -> variables."""

    def setUp(self):
        super().setUp()
        self.tenant_id = self.storage.create_tenant("p01-{0}".format(time.time_ns()))["id"]
        self.storage.create_variable(
            {"id": VARIABLE_ID, "name": "score", "category": "risk", "source_id": SOURCE_ID,
             "code": VARIABLE_CODE, "status": "prod"},
            tenant_id=self.tenant_id)
        self.policy = {
            "id": "p01_policy",
            "name": "P01",
            "status": "prod",
            "steps": [
                {"id": "s1", "type": "connector", "ref_id": SOURCE_ID},
                {"id": "s2", "type": "outcome", "ref_id": "approve"},
            ],
            "defaultOutcome": "review",
        }

    def tearDown(self):
        fast_decide.invalidate(self.tenant_id)

    def _connector_row(self, config):
        self.storage.create_connector(_connector(config), tenant_id=self.tenant_id)
        fast_decide.invalidate(self.tenant_id)

    def _run(self, payload=None, send=None, source=LIVE_SOURCE):
        executor = PolicyExecutor(self.storage)
        patch = mock.patch.object(connector_fetch, "_send", send) if send else None
        if patch:
            patch.start()
        try:
            return asyncio.run(executor.execute(
                policy=self.policy, payload=payload or {}, tenant_id=self.tenant_id, source=source))
        finally:
            if patch:
                patch.stop()

    def _fetch_errors(self):
        return [e for e in self.storage.list_error_events(tenant_id=self.tenant_id)
                if e.get("stage") == "source_fetch_error"]

    # -- the feature ------------------------------------------------------
    def test_a_configured_connector_is_fetched_and_its_data_reaches_the_variables(self):
        self._connector_row(_fetch_config())
        send = _responder()
        ctx = self._run(send=send)
        self.assertEqual(len(send.calls), 1, "the connector was never contacted")
        self.assertEqual(ctx.variables[VARIABLE_ID], FETCHED_SCORE)
        self.assertNotEqual(ctx.variables[VARIABLE_ID], SAMPLE_SCORE)
        self.assertEqual(ctx.outcome, "approve")
        self.assertEqual([e["outcome"] for e in ctx.source_fetches], ["fetched"])

    def test_a_caller_supplied_payload_still_wins(self):
        """The source-agnostic contract: whoever sends the data owns it. Fetching is only for
        the case where the caller did not."""
        self._connector_row(_fetch_config())
        send = _responder()
        ctx = self._run(payload={SOURCE_ID: {"score": 123}}, send=send)
        self.assertEqual(len(send.calls), 0, "a supplied source was overwritten by a fetch")
        self.assertEqual(ctx.variables[VARIABLE_ID], 123)
        self.assertEqual([e["outcome"] for e in ctx.source_fetches], ["caller_supplied"])

    def test_a_connector_without_fetch_configuration_is_untouched(self):
        self._connector_row({"base_url": "https://p01.example.com/score", "timeout_ms": 2000})
        send = _responder()
        ctx = self._run(send=send)
        self.assertEqual(len(send.calls), 0)
        self.assertEqual(ctx.source_fetches, [])
        # Exactly today's behaviour on a production source: nothing supplied, nothing seeded.
        self.assertEqual(ctx.variables[VARIABLE_ID], 0)
        self.assertEqual(ctx.outcome, "approve")

    # -- the fail-closed contract ----------------------------------------
    def test_a_failed_fetch_does_not_substitute_sample_data(self):
        """THE test. The sample scores 999; a dead source must not decide on it."""
        self._connector_row(_fetch_config())
        boom = connector_fetch._TransportFailed("timeout", "no response before the timeout")
        ctx = self._run(send=_raiser(boom))
        self.assertNotEqual(ctx.variables[VARIABLE_ID], SAMPLE_SCORE)
        self.assertEqual(ctx.variables[VARIABLE_ID], 0)
        self.assertEqual(ctx.payload.get(SOURCE_ID), {})

    def test_a_failed_fetch_sends_the_decision_to_review(self):
        self._connector_row(_fetch_config())
        boom = connector_fetch._TransportFailed("timeout", "no response before the timeout")
        ctx = self._run(send=_raiser(boom))
        self.assertEqual(ctx.outcome, "review", "an unreachable source produced a live approval")
        guard = [entry for entry in ctx.step_trace if entry.get("type") == "source_fetch_guard"]
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]["original_outcome"], "approve")
        self.assertEqual(guard[0]["failed_sources"], [SOURCE_ID])

    def test_the_connector_step_reports_the_failure_instead_of_ok(self):
        self._connector_row(_fetch_config())
        boom = connector_fetch._TransportFailed("timeout", "no response before the timeout")
        ctx = self._run(send=_raiser(boom))
        step = [e for e in ctx.step_trace if e.get("step", {}).get("type") == "connector"][0]
        self.assertEqual(step["result"]["status"], "failed")
        self.assertEqual(step["result"]["payload_keys"], [])

    def test_a_failed_fetch_is_recorded_as_an_observable_error(self):
        self._connector_row(_fetch_config())
        boom = connector_fetch._TransportFailed(
            "unreachable", "connection failed to https://p01.example.com/score?k=SUPERSECRET123")
        self._run(send=_raiser(boom))
        events = self._fetch_errors()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["entity_id"], SOURCE_ID)
        self.assertNotIn("SUPERSECRET123", str(events[0]))

    def test_a_blocked_egress_target_is_refused_and_recorded(self):
        self._connector_row(_fetch_config(base_url="http://169.254.169.254/latest/meta-data/"))
        ctx = self._run()
        self.assertEqual(ctx.outcome, "review")
        self.assertEqual(ctx.source_fetches[0]["outcome"], "blocked_by_egress_policy")
        events = self._fetch_errors()
        self.assertEqual(len(events), 1)

    def test_a_malformed_mapping_fails_closed_rather_than_raising_into_the_decision(self):
        self._connector_row(_fetch_config(response_mapping="{not json"))
        ctx = self._run(send=_responder())
        self.assertEqual(ctx.outcome, "review")
        self.assertEqual(ctx.variables[VARIABLE_ID], 0)
        self.assertEqual(ctx.source_fetches[0]["outcome"], "malformed_response_mapping")

    def test_continue_is_available_but_still_never_substitutes_sample_data(self):
        self._connector_row(_fetch_config(on_fetch_failure="continue"))
        boom = connector_fetch._TransportFailed("timeout", "no response before the timeout")
        ctx = self._run(send=_raiser(boom))
        self.assertEqual(ctx.outcome, "approve", "`continue` is opt-in and must still continue")
        self.assertNotEqual(ctx.variables[VARIABLE_ID], SAMPLE_SCORE)

    def test_abort_refuses_to_produce_a_decision_at_all(self):
        self._connector_row(_fetch_config(on_fetch_failure="abort"))
        boom = connector_fetch._TransportFailed("timeout", "no response before the timeout")
        with self.assertRaises(connector_fetch.SourceFetchFailed) as raised:
            self._run(send=_raiser(boom))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.source_id, SOURCE_ID)

    # -- budget + surfaces -------------------------------------------------
    def test_the_evaluation_budget_is_checked_before_the_fetch_starts(self):
        self._connector_row(_fetch_config())
        send = _responder()
        executor = PolicyExecutor(self.storage)
        original = executor._start_budget

        def expired(ctx):
            original(ctx)
            ctx.deadline = time.perf_counter() - 1  # budget already spent by the catalog reads

        executor._start_budget = expired
        with mock.patch.object(connector_fetch, "_send", send):
            with self.assertRaises(EvaluationBudgetExceeded):
                asyncio.run(executor.execute(
                    policy=self.policy, payload={}, tenant_id=self.tenant_id, source=LIVE_SOURCE))
        self.assertEqual(len(send.calls), 0, "a fetch was started with no budget to finish it")

    def test_the_remaining_budget_is_handed_to_the_fetch(self):
        self._connector_row(_fetch_config())
        send = _responder()
        self._run(send=send)
        self.assertIsNotNone(send.calls[0]["deadline"], "the fetch ran outside the evaluation budget")

    def test_a_backtest_does_not_fire_live_requests(self):
        """`_run_decision_batch` fans thousands of historical payloads across 32 threads. One
        click must not become thousands of real bureau pulls."""
        self._connector_row(_fetch_config())
        send = _responder()
        executor = PolicyExecutor(self.storage)
        with mock.patch.object(connector_fetch, "_send", send):
            asyncio.run(executor.execute(
                policy=self.policy, payload={}, tenant_id=self.tenant_id,
                source="simulation", simulate=True))
        self.assertEqual(len(send.calls), 0)

    def test_the_test_console_does_not_fire_live_requests(self):
        self._connector_row(_fetch_config())
        send = _responder()
        ctx = self._run(send=send, source="test_console")
        self.assertEqual(len(send.calls), 0)
        # And it keeps its sample pre-fill, unchanged.
        self.assertEqual(ctx.variables[VARIABLE_ID], SAMPLE_SCORE)

    def test_a_source_nothing_reads_is_not_fetched(self):
        """No variable declares `unread` as its source and no step names it, so contacting it
        would be a request made on behalf of nothing."""
        self.storage.create_connector(_connector(_fetch_config(), connector_id="unread"),
                                      tenant_id=self.tenant_id)
        self._connector_row(_fetch_config())
        send = _responder()
        ctx = self._run(send=send)
        self.assertEqual(len(send.calls), 1)
        self.assertEqual([e["source"] for e in ctx.source_fetches], [SOURCE_ID])

    def test_the_fast_path_declines_a_tenant_that_fetches(self):
        """Fact: fast_decide builds every source payload from sample_payload. Serving a
        fetch-configured tenant there would make FAST_DECIDE=1 revert this whole contract."""
        self.assertTrue(fast_decide.fast_path_eligible(self.storage, self.policy, self.tenant_id))
        self._connector_row(_fetch_config())
        self.assertFalse(fast_decide.fast_path_eligible(self.storage, self.policy, self.tenant_id))


class TenantIsolationTest(_Base):
    """A connector is a tenant-scoped entity, and so is everything derived from it."""

    def test_a_fetch_reads_the_calling_tenants_connector_only(self):
        storage = self.storage
        tenant_a = storage.create_tenant("p01-a-{0}".format(time.time_ns()))["id"]
        tenant_b = storage.create_tenant("p01-b-{0}".format(time.time_ns()))["id"]
        storage.create_connector(_connector(_fetch_config(api_key="KEY-A")), tenant_id=tenant_a)
        storage.create_connector(
            _connector(_fetch_config(api_key="KEY-B", base_url="https://b.example.com/score")),
            tenant_id=tenant_b)

        executor = PolicyExecutor(storage)
        got_b = executor._connector_with_secrets(tenant_b, SOURCE_ID)
        self.assertEqual(got_b["config"]["api_key"], "KEY-B")
        self.assertEqual(got_b["config"]["base_url"], "https://b.example.com/score")

    def test_one_tenants_dead_source_does_not_short_circuit_anothers(self):
        """The breaker is keyed by tenant AND connector. Seeded connector ids ("bureau",
        "kyc") are identical across tenants, so a bare-id key would let tenant A's outage
        take tenant B's healthy integration out of service."""
        connector = _connector(_fetch_config())
        boom = connector_fetch._TransportFailed("timeout", "down")
        with mock.patch.object(connector_fetch, "_send", _raiser(boom)):
            for _ in range(6):
                asyncio.run(connector_fetch.fetch_source(
                    connector, {"payload": {}, "secrets": {}}, "tenant-a"))
        opened = asyncio.run(connector_fetch.fetch_source(
            connector, {"payload": {}, "secrets": {}}, "tenant-a"))
        self.assertEqual(opened["outcome"], "circuit_open")

        send = _responder()
        with mock.patch.object(connector_fetch, "_send", send):
            healthy = asyncio.run(connector_fetch.fetch_source(
                connector, {"payload": {}, "secrets": {}}, "tenant-b"))
        self.assertEqual(healthy["status"], "ok")
        self.assertEqual(len(send.calls), 1)


if __name__ == "__main__":
    unittest.main()
