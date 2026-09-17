"""Sweep: the runtime/deploy-board endpoints that no test mentioned.

`app/routers/runtime.py` holds six routes nothing in `tests/` referenced: /api/v1/test/variables,
/api/v1/test/batch, /api/v1/test/action, /api/v1/deploy/status, /api/v1/deploy/promote and
/api/v1/bootstrap. Two of them are worth pinning down.

* **/api/v1/test/action coerces console input with bare builtins.** It is the Postman-style
  console for an action step: the caller hands over a raw `action` dict (`action: Dict[str, Any]`
  -- no schema at all) and the handler does `int(action["timeoutMs"])` and `dict(headers)` on it.
  A field left blank in the UI (`null`) or typed as text ("5s") is a 500, and a 500 on a *test*
  console is the worst possible answer: the surface exists to tell an author what is wrong with
  their step, and instead it reports that the server is broken. The url check that would have
  caught a malformed action sits three lines BELOW the `int()`.

* **/api/v1/deploy/promote is a loop with no transaction, and promotion is a state MACHINE.**
  Each item advances one stage (`next_status`: dev->uat->prod). The loop raises on the first bad
  item, keeping everything it already promoted, and answers 404/409 with no record of what got
  through. The operator's only move is to fix the batch and re-submit -- at which point the items
  that already succeeded advance a SECOND stage. Select four assets on the deploy board, have one
  of them fail the "latest test must pass" gate, press Promote twice, and the other three are in
  production. Batch promotion is the surface where that mistake costs the most.

Tenant scoping on the deploy board (/deploy/status, /bootstrap, /test/variables) is covered too:
none of those handlers passes a tenant down, so they rest entirely on the storage fallback chain.
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
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class _RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "runtime.db"))
        self.client = TestClient(app_main.app)
        self.tenant_a = app_main.storage.default_tenant_id
        self.headers_a = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_b = app_main.storage.create_tenant("Sweep Runtime Co")["id"]
        self.headers_b = {
            "x-api-key": app_main.storage.generate_api_key_for_tenant(self.tenant_b)["plaintext"]
        }

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()


class TestActionMalformedInputTest(_RuntimeCase):
    """POST /api/v1/test/action must answer 4xx for a malformed action, never 500."""

    def _post(self, action):
        return self.client.post("/api/v1/test/action", headers=self.headers_a, json={"action": action})

    def test_a_non_numeric_timeout_is_a_client_error(self):
        """`int("5s")` -> ValueError -> 500. The field is free text in the console."""
        res = self._post({"url": "https://example.com", "timeoutMs": "5s"})
        self.assertLess(res.status_code, 500,
                        "timeoutMs='5s' answered {0}".format(res.status_code))
        self.assertEqual(res.status_code, 422)

    def test_a_null_timeout_is_a_client_error(self):
        """`int(None)` -> TypeError -> 500. A cleared numeric field serialises as null."""
        res = self._post({"url": "https://example.com", "timeoutMs": None})
        self.assertLess(res.status_code, 500,
                        "timeoutMs=null answered {0}".format(res.status_code))

    def test_non_object_headers_are_a_client_error(self):
        """`dict("Authorization: x")` -> ValueError -> 500. Pasting a raw header line is the
        single most likely way a human gets this field wrong."""
        res = self._post({"url": "https://example.com", "headers": "Authorization: x"})
        self.assertLess(res.status_code, 500,
                        "headers as a string answered {0}".format(res.status_code))
        self.assertEqual(res.status_code, 422)

    def test_a_missing_url_is_still_422(self):
        """Regression guard: the existing validation must survive the hardening."""
        res = self._post({})
        self.assertEqual(res.status_code, 422)
        self.assertIn("url", res.json()["detail"].lower())

    def test_an_internal_url_is_still_blocked_by_egress_policy(self):
        """Regression guard for the SSRF gate: it must not be short-circuited by the new
        validation running earlier."""
        res = self._post({"url": "http://127.0.0.1:8080/api/v1/bootstrap"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("egress", str(res.json()["detail"]).lower())


class DeployPromoteBatchTest(_RuntimeCase):
    """POST /api/v1/deploy/promote — a partially applied batch must not be silently kept."""

    def _rule(self, name: str, passing: bool = True):
        rule = app_main.storage.create_rule(
            {"id": name, "name": name, "status": "dev", "nodes": [],
             "last_test_result": {"passed": passing}},
            tenant_id=self.tenant_a)
        return rule["id"]

    def _status(self, rule_id: str) -> str:
        return app_main.storage.get_rule(rule_id, tenant_id=self.tenant_a)["status"]

    def _promote(self, ids):
        return self.client.post(
            "/api/v1/deploy/promote", headers=self.headers_a,
            json={"items": [{"entity_type": "rule", "entity_id": i} for i in ids]})

    def test_a_batch_with_a_missing_item_promotes_nothing(self):
        """The first item succeeds, the second 404s, and the first stays promoted.

        Nothing in the 404 body says so, so the deploy board shows a failure over a batch that
        half-applied.
        """
        good = self._rule("sweep-good")
        res = self._promote([good, "no-such-rule"])
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self._status(good), "dev",
                         "a rejected batch left '{0}' promoted to {1}".format(good, self._status(good)))

    def test_retrying_a_failed_batch_cannot_double_promote(self):
        """The consequence, stated as the operator's actual next move.

        A batch that half-applies and reports only an error invites exactly one response: fix
        the bad item and press Promote again. On the retry the survivors advance a SECOND stage
        -- dev->uat on the first attempt, uat->prod on the second -- so re-submitting a batch
        that "failed" is how an untested asset reaches production.
        """
        good = self._rule("sweep-retry")
        self._promote([good, "no-such-rule"])          # attempt 1: 404
        self._promote([good, "no-such-rule"])          # attempt 2: the operator retries
        self.assertNotEqual(self._status(good), "prod",
                            "retrying a failed batch promoted '{0}' to production".format(good))

    def test_the_same_asset_twice_in_one_batch_advances_one_stage(self):
        """Found while fixing the partial-apply: the loop has no duplicate check either.

        Listing an asset twice walks it dev -> uat -> prod inside a single request -- the
        two-stage path the deploy board exists to make deliberate, collapsed into one click.
        """
        rule_id = self._rule("sweep-dupe")
        self._promote([rule_id, rule_id])
        self.assertNotEqual(self._status(rule_id), "prod",
                            "listing '{0}' twice promoted it straight to production".format(rule_id))

    def test_a_clean_batch_still_promotes_every_item(self):
        """Regression guard: atomicity must not cost the endpoint its actual job."""
        ids = [self._rule("sweep-clean-1"), self._rule("sweep-clean-2")]
        res = self._promote(ids)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["promoted"]), 2)
        for rule_id in ids:
            self.assertEqual(self._status(rule_id), "uat")

    def test_an_unpromotable_item_is_reported_not_swallowed(self):
        """A rule whose latest test failed is a 409 from ensure_promotable; the batch must
        surface it rather than promoting around it."""
        blocked = self._rule("sweep-blocked", passing=False)
        good = self._rule("sweep-alongside")
        res = self._promote([good, blocked])
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self._status(good), "dev")


class DeployBoardTenantScopeTest(_RuntimeCase):
    """The three read surfaces that pass no tenant down must still answer per-tenant."""

    def setUp(self):
        super().setUp()
        app_main.storage.create_rule(
            {"id": "a-only", "name": "a-only", "status": "dev", "nodes": []},
            tenant_id=self.tenant_a)

    def test_deploy_status_is_per_tenant(self):
        board = self.client.get("/api/v1/deploy/status", headers=self.headers_b).json()
        self.assertEqual(board["dev"]["rules"], [],
                         "tenant B's deploy board listed tenant A's dev rules")

    def test_bootstrap_is_per_tenant(self):
        body = self.client.get("/api/v1/bootstrap", headers=self.headers_b).json()
        self.assertEqual(body["rules"], [], "tenant B's bootstrap payload carried tenant A's rules")
        self.assertEqual(body["variables"], [])

    def test_test_variables_is_per_tenant(self):
        """POST /api/v1/test/variables runs every variable in the workspace and returns each
        computed value -- a cross-tenant leak here is a leak of computed customer data, not
        just of names."""
        body = self.client.post("/api/v1/test/variables", headers=self.headers_b, json={}).json()
        self.assertEqual(body["results"], [])

    def test_deploy_status_still_shows_this_tenants_assets(self):
        """Regression guard for the three above."""
        board = self.client.get("/api/v1/deploy/status", headers=self.headers_a).json()
        self.assertEqual([r["name"] for r in board["dev"]["rules"]], ["a-only"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
