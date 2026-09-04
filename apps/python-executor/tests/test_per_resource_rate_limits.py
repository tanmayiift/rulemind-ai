"""One tenant-wide bucket let cheap reads starve production decisioning (P18, phase 3).

`TenantContextMiddleware` called `rate_limit.check(tenant)` with no path argument, so every
authenticated route drew from ONE bucket keyed on the tenant id. Two consequences, in opposite
directions, and both of them are the limiter working exactly as written:

  * A dashboard tab polling `GET /api/v1/policies` in a loop spends the same allowance as
    `POST /api/v1/decide`. A misbehaving browser could therefore 429 that tenant's production
    decision traffic -- the thing the customer is actually paying for -- and nothing in the
    response would say which caller had spent the budget.
  * The converse. A decision limit sized for machine traffic (5000/min on the enterprise plan)
    handed the same 5000/min to `/api/v1/ai`, which calls a paid provider on every request.
    `ai.estimate_cost` prices Opus at $5/$25 per MTok and o1 at $15/$60; a stuck retry loop
    there shows up on the customer's provider invoice, not in our latency graph.

`app/rate_limit.py` already had everything needed except the split: per-tenant/plan/env limit
resolution, a Redis Lua token bucket, a bounded local fallback, and a `cost=` argument nobody
passed anything but 1.0 to. This file tests the split that was added on top:

  1. `classify(method, path)` -- an explicit prefix map, shaped like `rbac.required_capability`,
     including the part that matters: an unmapped WRITE fails tight instead of inheriting the
     largest budget in the table.
  2. per-class buckets, sized as a share of the tenant's configured limit.
  3. the tenant-wide bucket kept as an OUTER bound, because the shares sum to 2.05x.
  4. `request_cost` -- a batch of N payloads is not one unit.

Determinism note: these tests force the documented in-process fallback by patching
`redis_client` to None, and save/restore REDIS_URL with an explicit `else` branch. A bare pop
here leaks a missing (or dead) REDIS_URL into the rest of the process and breaks unrelated
tests in the full run while passing alone -- which is precisely what happened in
tests/test_redis_resilience.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
import app.runtime as runtime  # noqa: E402
from app import rate_limit  # noqa: E402
from app.storage import Storage  # noqa: E402


class _Isolated(unittest.TestCase):
    """No Redis, empty buckets, and no rate-limit env bleeding in or out.

    The buckets are process-global module state and the limits are read from `os.environ` on
    every call, so without this a previous module decides this module's results -- and this
    module decides the next one's.
    """

    ENV_KEYS = (
        "RATE_LIMIT_ENABLED", "RATE_LIMIT_PER_CLASS", "RATE_LIMIT_AGGREGATE",
        "RATE_LIMIT_RPM", "RATE_LIMIT_RPM_STANDARD", "RATE_LIMIT_BURST_MULTIPLIER",
        "RATE_LIMIT_WINDOW_SECONDS", "RATE_LIMIT_EXEMPT_PATHS", "RATE_LIMIT_FAIL_OPEN",
        "RATE_LIMIT_BATCH_UNIT_BYTES", "RATE_LIMIT_MAX_REQUEST_COST",
    ) + tuple(
        "RATE_LIMIT_RPM_CLASS_{0}".format(c.upper()) for c in rate_limit.RESOURCE_CLASSES
    ) + tuple(
        "RATE_LIMIT_CLASS_SHARE_{0}".format(c.upper()) for c in rate_limit.RESOURCE_CLASSES
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self._saved_redis_url = os.environ.pop("REDIS_URL", None)
        self._patches = [
            mock.patch("app.rate_limit.redis_client", return_value=None),
            mock.patch("app.runtime.redis_client", return_value=None),
        ]
        for patch in self._patches:
            patch.start()
        runtime.reset_redis_client_for_tests()
        rate_limit.reset_local_state()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # The `else` branch is not optional: with no saved value, a bare `if` leaves this
        # module's absent-or-dead REDIS_URL in os.environ for every later test in the process.
        if self._saved_redis_url is not None:
            os.environ["REDIS_URL"] = self._saved_redis_url
        else:
            os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()
        rate_limit.reset_local_state()

    def drain(self, tenant, method, path, attempts=200):
        """(allowed, rejected) after firing `attempts` requests at one path."""
        outcomes = [rate_limit.check_request(tenant, method, path).allowed
                    for _ in range(attempts)]
        return outcomes.count(True), outcomes.count(False)


class ClassMapTests(unittest.TestCase):
    """The map itself. Pure function of (method, path) -- no buckets involved."""

    def test_the_money_paths_are_separated(self):
        cases = {
            ("POST", "/api/v1/decide"): rate_limit.DECISION,
            ("POST", "/sdk/v1/decide"): rate_limit.DECISION,
            ("GET", "/api/v1/policies"): rate_limit.READ,
            ("PUT", "/api/v1/policies/pol_1"): rate_limit.WRITE,
            ("POST", "/api/v1/ai/generate-rule"): rate_limit.AI,
            ("GET", "/api/v1/audit/decisions"): rate_limit.EXPORT,
            ("POST", "/api/v1/reports/rep_1/run"): rate_limit.EXPORT,
            ("POST", "/api/v1/access/keys"): rate_limit.ADMIN,
            ("POST", "/scim/v2/Users"): rate_limit.ADMIN,
        }
        for (method, path), expected in cases.items():
            self.assertEqual(rate_limit.classify(method, path), expected,
                             "{0} {1}".format(method, path))

    def test_a_read_and_a_decision_do_not_share_a_class(self):
        """The headline, stated as a property of the map rather than of the buckets."""
        self.assertNotEqual(rate_limit.classify("GET", "/api/v1/policies"),
                            rate_limit.classify("POST", "/api/v1/decide"))

    def test_ai_is_its_own_class_because_it_costs_real_money(self):
        for path in ("/api/v1/ai/generate-policy", "/api/v1/ai/explain-decision",
                     "/api/v1/ai/config", "/api/v1/ai/budget"):
            self.assertEqual(rate_limit.classify("POST", path), rate_limit.AI, path)
        self.assertLess(rate_limit.DEFAULT_CLASS_SHARE[rate_limit.AI],
                        rate_limit.DEFAULT_CLASS_SHARE[rate_limit.DECISION],
                        "the paid-provider class must not be as generous as decisioning")

    def test_an_unmapped_write_fails_tight_instead_of_inheriting_a_budget(self):
        """rbac.required_capability was changed to fail CLOSED on an unmapped write after
        /scim/v2 reached policy_maker purely by falling off the end of the table. Same shape
        here: a new mutating prefix must not silently get the largest budget in the map, and
        must not get no bucket at all."""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(rate_limit.classify(method, "/api/v1/brand-new-surface"),
                             rate_limit.UNCLASSIFIED, method)
        self.assertEqual(rate_limit.DEFAULT_CLASS_SHARE[rate_limit.UNCLASSIFIED],
                         min(rate_limit.DEFAULT_CLASS_SHARE.values()),
                         "unmapped writes must land on the tightest share, not a middling one")

    def test_an_unmapped_read_is_still_a_read(self):
        """Mirrors rbac's first line (GET -> READ). A GET has no side effects, and the
        aggregate bucket still bounds it."""
        self.assertEqual(rate_limit.classify("GET", "/api/v1/brand-new-surface"),
                         rate_limit.READ)

    def test_every_class_is_a_known_class(self):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", ""):
            for path in ("/", "/api/v1", "/api/v1/", "/nonsense", "/api/v1/decide", "", "//"):
                self.assertIn(rate_limit.classify(method, path), rate_limit.RESOURCE_CLASSES,
                              "{0} {1} produced a class with no configured budget".format(method, path))

    def test_prefix_matching_respects_path_segments(self):
        """A bare str.startswith would file /api/v1/aiming under /api/v1/ai and hand a brand
        new endpoint the tightest budget in the table because of a shared substring."""
        self.assertNotEqual(rate_limit.classify("POST", "/api/v1/aiming"), rate_limit.AI)
        self.assertNotEqual(rate_limit.classify("POST", "/api/v1/decidedly"), rate_limit.DECISION)
        self.assertEqual(rate_limit.classify("POST", "/api/v1/ai/test"), rate_limit.AI)

    def test_evaluation_subroutes_of_authoring_urls_are_decisions(self):
        """/policies/{id}/execute and /models/{id}/predict run the engine. Charging them to
        the authoring budget would put the expensive half of authoring in the cheap bucket."""
        for path in ("/api/v1/policies/p1/execute", "/api/v1/models/m1/predict",
                     "/api/v1/rules/r1/test", "/api/v1/decision-tables/t1/evaluate",
                     "/api/v1/policies/p1/backtest", "/api/v1/variables/test-draft"):
            self.assertEqual(rate_limit.classify("POST", path), rate_limit.DECISION, path)

    def test_a_connector_test_is_not_charged_to_decisioning(self):
        """It opens an outbound connection, so it is expensive -- but billing a dashboard's
        'test connection' button to the DECISION budget is the starvation this split exists
        to prevent, only with the roles swapped."""
        self.assertEqual(rate_limit.classify("POST", "/api/v1/connectors/c1/test"),
                         rate_limit.WRITE)


class ClassBudgetTests(_Isolated):
    """Each class gets its own bucket, sized off the tenant's configured limit."""

    def tenant(self, rpm=100, **config):
        return {"id": "t-budget", "plan": "standard",
                "config": dict({"rate_limit_rpm": rpm}, **config)}

    def test_a_read_flood_cannot_starve_decisioning(self):
        """THE bug. Exhaust the read budget completely, then decide anyway."""
        tenant = self.tenant(rpm=100)
        allowed, rejected = self.drain(tenant, "GET", "/api/v1/policies", attempts=200)
        self.assertGreater(rejected, 0, "the read budget was never exhausted, so this proves nothing")
        self.assertTrue(rate_limit.check_request(tenant, "POST", "/api/v1/decide").allowed,
                        "{0} rejected reads left the tenant unable to run a decision".format(rejected))
        self.assertLess(allowed, 200)

    def test_the_reverse_direction_exhausts_the_shared_ceiling_not_the_read_bucket(self):
        """The other direction, and the honest limit of what a split can promise.

        Decisions carry a class share of 1.0 -- a tenant that does nothing but decide must be
        able to spend its whole contracted rate deciding, or the plan number is a lie -- so
        saturating decisions also saturates the aggregate, and the dashboard is refused along
        with everything else. That is the tenant sitting at 100% of its plan, not one surface
        starving another: the READ bucket itself is untouched, which is exactly why the
        refusal names the tenant ceiling and not the read class. An operator who wants
        guaranteed dashboard headroom during saturation sets
        RATE_LIMIT_CLASS_SHARE_DECISION below 1.0 and keeps the difference in reserve.
        """
        tenant = self.tenant(rpm=100)
        _, rejected = self.drain(tenant, "POST", "/api/v1/decide", attempts=400)
        self.assertGreater(rejected, 0, "the decision budget was never exhausted")

        refused = rate_limit.check_request(tenant, "GET", "/api/v1/policies")
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.scope, rate_limit.TENANT,
                         "decision traffic drained the READ bucket -- the classes are not "
                         "actually independent")

    def test_lifting_the_ceiling_shows_the_read_bucket_really_is_full(self):
        """The same state as above, with the aggregate switched off: reads sail through, so
        nothing about the decision flood ever reached the read class bucket."""
        tenant = self.tenant(rpm=100)
        self.drain(tenant, "POST", "/api/v1/decide", attempts=400)
        os.environ["RATE_LIMIT_AGGREGATE"] = "0"
        self.assertTrue(rate_limit.check_request(tenant, "GET", "/api/v1/policies").allowed)

    def test_ai_is_bounded_far_below_the_tenant_limit(self):
        tenant = self.tenant(rpm=1000)
        self.assertLess(rate_limit.resolve_class_limit(tenant, rate_limit.AI),
                        rate_limit.resolve_limit(tenant) / 10.0,
                        "a 1000/min tenant could still make 1000 paid provider calls a minute")

    def test_budgets_scale_with_the_tenant_limit(self):
        small = rate_limit.resolve_class_limit(self.tenant(rpm=100), rate_limit.WRITE)
        large = rate_limit.resolve_class_limit(self.tenant(rpm=1000), rate_limit.WRITE)
        self.assertEqual(large, small * 10,
                         "upgrading a plan must raise every class budget, not just the aggregate")

    def test_no_class_is_rounded_down_to_nothing(self):
        """5% of a tiny limit is a fraction of a request. A floor is what keeps a small plan
        from having a class it can never use."""
        tenant = self.tenant(rpm=1)
        for resource_class in rate_limit.RESOURCE_CLASSES:
            self.assertGreaterEqual(rate_limit.resolve_class_limit(tenant, resource_class),
                                    rate_limit.CLASS_FLOOR_RPM, resource_class)

    def test_an_operator_can_set_an_absolute_class_limit(self):
        os.environ["RATE_LIMIT_RPM_CLASS_AI"] = "3"
        self.assertEqual(rate_limit.resolve_class_limit(self.tenant(rpm=1000), rate_limit.AI), 3,
                         "an explicit absolute override outranks the floor -- it is an operator "
                         "saying so, not a rounding accident")

    def test_an_operator_can_reshape_a_share(self):
        os.environ["RATE_LIMIT_CLASS_SHARE_EXPORT"] = "0.5"
        self.assertEqual(rate_limit.resolve_class_limit(self.tenant(rpm=100), rate_limit.EXPORT), 50)

    def test_per_tenant_class_config_beats_env(self):
        os.environ["RATE_LIMIT_RPM_CLASS_AI"] = "3"
        tenant = self.tenant(rpm=1000, rate_limit_class_rpm={"ai": 42})
        self.assertEqual(rate_limit.resolve_class_limit(tenant, rate_limit.AI), 42)

    def test_malformed_class_config_does_not_crash(self):
        for bad in ({"ai": "abc"}, {"ai": None}, {"ai": -1}, "not-a-dict", None):
            tenant = self.tenant(rpm=100, rate_limit_class_rpm=bad)
            self.assertGreater(rate_limit.resolve_class_limit(tenant, rate_limit.AI), 0, repr(bad))

    def test_an_unmapped_write_is_limited_not_unlimited(self):
        tenant = self.tenant(rpm=1000)
        allowed, rejected = self.drain(tenant, "POST", "/api/v1/brand-new-surface", attempts=300)
        self.assertGreater(rejected, 0,
                           "a path nobody mapped accepted {0} requests unchecked".format(allowed))

    def test_collapsing_back_to_one_bucket_is_supported(self):
        """RATE_LIMIT_PER_CLASS=0 is the escape hatch for an operator who finds the split
        wrong for their traffic. It must restore the previous behaviour exactly."""
        os.environ["RATE_LIMIT_PER_CLASS"] = "0"
        tenant = self.tenant(rpm=6)
        allowed = [rate_limit.check_request(tenant, "GET", "/api/v1/policies").allowed
                   for _ in range(4)]
        allowed += [rate_limit.check_request(tenant, "POST", "/api/v1/decide").allowed
                    for _ in range(4)]
        self.assertEqual(allowed[:6], [True] * 6)
        self.assertEqual(allowed[6:], [False] * 2,
                         "with the split off, reads and decisions share one bucket again")


class AggregateCapTests(_Isolated):
    """The tenant-wide bucket stays as an OUTER bound over the per-class ones."""

    def test_the_shares_deliberately_oversubscribe(self):
        """Documents WHY the aggregate cannot be dropped: a tenant that only decides should
        get its whole limit for deciding, so the shares must overlap."""
        self.assertGreater(sum(rate_limit.DEFAULT_CLASS_SHARE.values()), 1.0)
        self.assertEqual(rate_limit.DEFAULT_CLASS_SHARE[rate_limit.DECISION], 1.0)

    def test_running_every_class_at_once_still_cannot_exceed_the_tenant_limit(self):
        limit = 20
        tenant = {"id": "t-aggregate", "plan": "standard", "config": {"rate_limit_rpm": limit}}
        probes = [
            ("POST", "/api/v1/decide"),
            ("GET", "/api/v1/policies"),
            ("PUT", "/api/v1/policies/p1"),
            ("POST", "/api/v1/ai/generate-rule"),
            ("GET", "/api/v1/audit"),
            ("POST", "/api/v1/access/keys"),
            ("POST", "/api/v1/brand-new-surface"),
        ]
        headroom = sum(rate_limit.resolve_class_limit(tenant, rate_limit.classify(m, p))
                       for m, p in probes)
        self.assertGreater(headroom, limit,
                           "this test is vacuous unless the class budgets oversubscribe")

        total = sum(self.drain(tenant, method, path, attempts=60)[0] for method, path in probes)
        # +1 for tokens the bucket refilled while the loop ran.
        self.assertLessEqual(total, limit + 1,
                             "{0} requests got through a {1}/min tenant by spreading across "
                             "{2} classes -- the aggregate cap is not applied".format(
                                 total, limit, len(probes)))

    def test_a_class_over_its_own_limit_does_not_drain_the_aggregate(self):
        """Order matters. If the aggregate were consumed first, a client hammering one
        throttled class would still spend the tenant-wide budget and take every other class
        down with it -- the same starvation, one level up."""
        tenant = {"id": "t-order", "plan": "standard", "config": {"rate_limit_rpm": 60}}
        os.environ["RATE_LIMIT_RPM_CLASS_READ"] = "5"
        self.drain(tenant, "GET", "/api/v1/policies", attempts=500)
        aggregate = rate_limit.check_request(tenant, "POST", "/api/v1/decide")
        self.assertTrue(aggregate.allowed)
        self.assertGreaterEqual(aggregate.remaining, 50,
                                "495 rejected reads were still charged to the tenant-wide bucket")


class RequestCostTests(_Isolated):
    """`cost=` existed and nothing ever passed anything but 1.0."""

    def test_a_plain_request_costs_one(self):
        self.assertEqual(rate_limit.request_cost("POST", "/api/v1/decide", "512"), 1.0)
        self.assertEqual(rate_limit.request_cost("GET", "/api/v1/policies", "999999"), 1.0)

    def test_a_batch_costs_more_than_a_single(self):
        single = rate_limit.request_cost("POST", "/api/v1/decide", "20000")
        batch = rate_limit.request_cost("POST", "/api/v1/decide/batch", "20000")
        self.assertEqual(single, 1.0)
        self.assertGreater(batch, 20.0,
                           "a 20KB batch of payloads bought a whole backtest for one token")

    def test_cost_grows_with_the_batch(self):
        small = rate_limit.request_cost("POST", "/api/v1/decide/batch", "2048")
        large = rate_limit.request_cost("POST", "/api/v1/decide/batch", "20480")
        self.assertGreater(large, small)

    def test_an_undeclared_length_is_not_free(self):
        """A chunked upload sends no Content-Length. Charging it 1.0 would be a one-header
        bypass of every batch cost."""
        self.assertGreaterEqual(rate_limit.request_cost("POST", "/api/v1/decide/batch", None),
                                rate_limit._BULK_BASE_COST)
        self.assertGreaterEqual(rate_limit.request_cost("POST", "/api/v1/import", "not-a-number"),
                                rate_limit._BULK_BASE_COST)

    def test_cost_is_capped_so_a_batch_is_never_permanently_rejected(self):
        """A cost larger than the bucket can ever hold is not a limit, it is a permanent 429:
        tokens never reach cost, so the caller is refused however long it waits."""
        tenant = {"id": "t-cost", "plan": "standard", "config": {"rate_limit_rpm": 10}}
        huge = rate_limit.request_cost("POST", "/api/v1/decide/batch", str(500 * 1024 * 1024))
        decision = rate_limit.check_request(tenant, "POST", "/api/v1/decide/batch", cost=huge)
        self.assertTrue(decision.allowed,
                        "a large batch against a small budget can never be served, at any rate")

    def test_a_batch_really_does_consume_more_of_the_budget(self):
        tenant = {"id": "t-spend", "plan": "standard", "config": {"rate_limit_rpm": 1000}}
        before = rate_limit.check_request(tenant, "POST", "/api/v1/decide").remaining
        after = rate_limit.check_request(
            tenant, "POST", "/api/v1/decide/batch",
            cost=rate_limit.request_cost("POST", "/api/v1/decide/batch", "20480")).remaining
        self.assertGreater(before - after, 10, "the batch was charged like a single decision")


class HeaderCoherenceTests(_Isolated):
    """Two budgets, one RateLimit-* triple. It has to say which one it describes."""

    def tenant(self, rpm=100):
        return {"id": "t-headers", "plan": "standard", "config": {"rate_limit_rpm": rpm}}

    def test_the_headers_name_the_budget_they_describe(self):
        decision = rate_limit.check_request(self.tenant(), "POST", "/api/v1/ai/generate-rule")
        self.assertEqual(decision.headers().get("RateLimit-Scope"), rate_limit.AI,
                         "'Remaining: 3' is unactionable if the client cannot tell three of what")

    def test_the_advertised_budget_is_the_one_that_will_reject_first(self):
        """Advertising the aggregate unconditionally would tell a caller it has 900 requests
        left while its class bucket rejects the next one."""
        tenant = self.tenant(rpm=1000)
        decision = rate_limit.check_request(tenant, "POST", "/api/v1/ai/generate-rule")
        self.assertEqual(decision.limit, rate_limit.resolve_class_limit(tenant, rate_limit.AI))
        self.assertLess(decision.limit, rate_limit.resolve_limit(tenant))

    def test_the_aggregate_is_advertised_when_it_is_the_binding_one(self):
        """A decision draws on a class budget equal to the whole tenant limit, so once the
        aggregate has been spent elsewhere it becomes the tighter of the two."""
        tenant = self.tenant(rpm=40)
        self.drain(tenant, "GET", "/api/v1/policies", attempts=200)
        decision = rate_limit.check_request(tenant, "POST", "/api/v1/decide")
        self.assertEqual(decision.headers().get("RateLimit-Scope"), rate_limit.TENANT)

    def test_a_rejection_reports_the_budget_that_rejected_it(self):
        os.environ["RATE_LIMIT_RPM_CLASS_AI"] = "1"
        tenant = self.tenant(rpm=1000)
        rate_limit.check_request(tenant, "POST", "/api/v1/ai/generate-rule")
        rejected = rate_limit.check_request(tenant, "POST", "/api/v1/ai/generate-rule")
        self.assertFalse(rejected.allowed)
        headers = rejected.headers()
        self.assertEqual(headers["RateLimit-Scope"], rate_limit.AI)
        self.assertEqual(headers["RateLimit-Limit"], "1")
        self.assertIn("Retry-After", headers)

    def test_the_kill_switch_still_advertises_nothing(self):
        """RATE_LIMIT_ENABLED=0 must not start emitting a scope header for a budget that is
        not being enforced. tests/test_rate_limit_config.py asserts this dict exactly."""
        os.environ["RATE_LIMIT_ENABLED"] = "0"
        decision = rate_limit.check_request(self.tenant(), "POST", "/api/v1/decide")
        self.assertEqual(decision.headers(), {"RateLimit-Limit": "0", "RateLimit-Remaining": "0",
                                              "RateLimit-Reset": "0"})

    def test_pre_auth_budgets_do_not_claim_to_be_tenant_budgets(self):
        """They are keyed on the client IP; there is no tenant yet to describe."""
        from app import middleware

        decision = rate_limit.check({"config": {"rate_limit_rpm": 5}},
                                    key="preauth:signup:10.0.0.1", scope="preauth:signup")
        self.assertEqual(decision.headers()["RateLimit-Scope"], "preauth:signup")
        self.assertIsNotNone(middleware._pre_auth_limit("/api/v1/onboarding/signup"))


class MiddlewareIntegrationTests(_Isolated):
    """End to end through TenantContextMiddleware, which is where the single bucket lived."""

    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "perclass.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        policies = self.client.get("/api/v1/policies", headers=self.headers)
        self.assertEqual(policies.status_code, 200, policies.text[:300])
        self.policy_id = policies.json()[0]["id"]
        rate_limit.reset_local_state()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()
        super().tearDown()

    def _decide(self):
        return self.client.post("/api/v1/decide", headers=self.headers,
                                json={"policyId": self.policy_id, "payload": {"age": 30}})

    def test_a_polling_dashboard_no_longer_429s_production_decisioning(self):
        """The whole point, over HTTP.

        The tenant gets 8 requests/minute and reads get 3 of them. Twelve GETs therefore burn
        exactly 3 tokens of the aggregate and are refused 9 times, leaving 5 for decisions.
        With one shared bucket the same twelve GETs drain all 8 and the decision 429s.
        """
        os.environ["RATE_LIMIT_RPM"] = "8"
        os.environ["RATE_LIMIT_RPM_CLASS_READ"] = "3"
        rate_limit.reset_local_state()

        codes = [self.client.get("/api/v1/policies", headers=self.headers).status_code
                 for _ in range(12)]
        self.assertEqual(codes[:3], [200] * 3, "the read budget was not honoured in full")
        self.assertIn(429, codes, "the read budget was never enforced")

        decided = self._decide()
        self.assertEqual(decided.status_code, 200,
                         "a dashboard poll loop took production decisioning offline: {0}".format(
                             decided.text[:300]))

    def test_the_429_body_and_headers_name_the_budget(self):
        os.environ["RATE_LIMIT_RPM_CLASS_READ"] = "2"
        rate_limit.reset_local_state()
        for _ in range(3):
            response = self.client.get("/api/v1/policies", headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("RateLimit-Scope"), rate_limit.READ)
        self.assertIn(rate_limit.READ, response.json()["detail"],
                      "the 429 does not say which budget ran out, so it reads as a total outage")
        self.assertIn("Retry-After", response.headers)

    def test_every_response_advertises_the_binding_budget(self):
        response = self.client.get("/api/v1/policies", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("RateLimit-Remaining", response.headers)
        self.assertIn(response.headers.get("RateLimit-Scope"),
                      (rate_limit.READ, rate_limit.TENANT))

    def test_health_checks_are_still_exempt(self):
        os.environ["RATE_LIMIT_RPM_CLASS_READ"] = "1"
        rate_limit.reset_local_state()
        codes = [self.client.get("/api/v1/health").status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 6, "a 429 on /health reads as an outage to a load balancer")


if __name__ == "__main__":
    unittest.main()
