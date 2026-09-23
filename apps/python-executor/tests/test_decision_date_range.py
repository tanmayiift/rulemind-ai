"""Decisions must be findable by date, and audit reads must not fall back to tenant #1.

Two defects in one file, because they were two lines apart.

**No date filter existed anywhere.** `list_decisions` filtered on client/user/policy/outcome
plus limit/offset, and `list_audit_events` on event/entity type — neither on `created_at`. The
only ordering is `created_at DESC`, so an eight-month-old decision was reachable only by paging
every row written since. The Decision Explorer did not even do that: it fetched the default 200
and filtered in the browser, so the screen a regulator question lands on could see the most
recent 200 decisions and nothing older. "Show me everything in Q1" was not expressible.

**`audit_decisions` and `audit_errors` called storage with no tenant.** They sit directly beneath
a docstring on `audit_events` that explains the storage fallback chain ends at
`self.default_tenant_id` — *the first tenant in the table* — and that "an audit read reaching it
would serve one tenant's change history to another". The lesson was applied to that one endpoint
and not to its two immediate neighbours.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
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


class DecisionDateRangeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "dates.db"))
        self.tenant = self.storage.default_tenant_id
        self.now = datetime.utcnow().replace(microsecond=0)
        for days, did in ((300, "old"), (30, "mid"), (0, "new")):
            self.storage.add_decision(
                {"id": did, "policy_id": "p1", "outcome": "reject", "payload": {},
                 "computed_variables": {}, "rule_results": [], "trace": [], "latency_ms": 1,
                 "source": "api", "created_at": self.now - timedelta(days=days)},
                tenant_id=self.tenant)

    def _ids(self, **kw):
        return {d["id"] for d in self.storage.list_decisions(tenant_id=self.tenant, **kw)}

    def test_without_a_range_everything_comes_back(self):
        self.assertEqual(self._ids(), {"old", "mid", "new"})

    def test_from_excludes_older(self):
        self.assertEqual(self._ids(created_from=self.now - timedelta(days=60)), {"mid", "new"})

    def test_to_excludes_newer(self):
        self.assertEqual(self._ids(created_to=self.now - timedelta(days=15)), {"old", "mid"})

    def test_a_window_selects_only_what_is_inside_it(self):
        """The eight-month-old decision, reachable directly rather than by paging to it."""
        self.assertEqual(
            self._ids(created_from=self.now - timedelta(days=400),
                      created_to=self.now - timedelta(days=200)), {"old"})

    def test_the_range_is_half_open_so_adjacent_windows_tile(self):
        """[from, to). Two back-to-back windows must cover every row exactly once — a closed
        upper bound double-counts whatever sits on the boundary."""
        boundary = self.now - timedelta(days=30)
        first = self._ids(created_to=boundary)
        second = self._ids(created_from=boundary)
        self.assertEqual(first | second, {"old", "mid", "new"})
        self.assertEqual(first & second, set())

    def test_iso_strings_are_accepted(self):
        """Routers hand these over as query strings, not datetimes."""
        iso = (self.now - timedelta(days=60)).isoformat()
        self.assertEqual(self._ids(created_from=iso), {"mid", "new"})


class AuditReadsAreTenantScopedTest(unittest.TestCase):
    """The fallback chain ends at the first tenant. These two routes relied on it.

    HONEST NOTE ON WHAT THESE PROVE. The HTTP tests below pass with OR without the explicit
    `tenant_id=` argument, and I checked: removing it does not fail them. That is not a defect
    in the fix, it is the nature of the hazard — on an authenticated request the middleware sets
    the tenant contextvar, so the fallback chain resolves correctly either way. The fallback
    only bites when the contextvar is LOST: a background thread, a ThreadPoolExecutor worker, an
    asyncio.run outside a request. None of those are reachable through TestClient.

    So these HTTP tests verify isolation holds; they do not verify the argument. The argument is
    defence-in-depth, and the two tests that actually discriminate are `test_storage_falls_back_
    to_the_first_tenant` (pins the hazard so it cannot be forgotten) and the source-level check
    that the routes pass a tenant. Leaving a test that looks like it proves the fix, and does
    not, would be the same failure this whole round is about."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "scope.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.a = app_main.storage.default_tenant_id          # the FIRST tenant — the fallback
        self.headers_a = {"x-api-key": app_main.storage.default_api_key or ""}
        self.b = app_main.storage.create_tenant("Second Co")["id"]
        self.headers_b = {"x-api-key":
                          app_main.storage.generate_api_key_for_tenant(self.b)["plaintext"]}
        app_main.storage.add_decision(
            {"id": "belongs-to-a", "policy_id": "p1", "outcome": "reject", "payload": {},
             "computed_variables": {}, "rule_results": [], "trace": [], "latency_ms": 1,
             "source": "api"}, tenant_id=self.a)

    def test_tenant_b_cannot_see_tenant_a_decisions(self):
        rows = self.client.get("/api/v1/audit/decisions", headers=self.headers_b).json()
        self.assertEqual([r["id"] for r in rows if r["id"] == "belongs-to-a"], [],
                         "the first tenant's decisions leaked to another tenant")

    def test_tenant_a_still_sees_its_own(self):
        rows = self.client.get("/api/v1/audit/decisions", headers=self.headers_a).json()
        self.assertIn("belongs-to-a", [r["id"] for r in rows])

    def test_the_date_range_is_reachable_over_http(self):
        res = self.client.get("/api/v1/audit/decisions",
                              params={"created_from": "2000-01-01T00:00:00"},
                              headers=self.headers_a)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("belongs-to-a", [r["id"] for r in res.json()])

    def test_errors_are_tenant_scoped_too(self):
        res = self.client.get("/api/v1/audit/errors", headers=self.headers_b)
        self.assertEqual(res.status_code, 200, res.text)

    def test_storage_falls_back_to_the_first_tenant(self):
        """The hazard itself, pinned. With no argument and no contextvar, storage resolves to
        `default_tenant_id` -- the FIRST tenant -- and returns its rows rather than raising.
        That is why the routes must pass a tenant explicitly, and why this is a comment in
        insights.py rather than a schema guarantee. Stage 5.5 is the structural answer (RLS)."""
        from app import context

        token = context.set_current_tenant_id(None)
        try:
            rows = app_main.storage.list_decisions()
        finally:
            context.reset_current_tenant_id(token)
        self.assertIn("belongs-to-a", [r["id"] for r in rows],
                      "storage no longer falls back to the first tenant -- if this is now an "
                      "error, that is an improvement and this test should assert the raise")

    def test_every_insights_route_passes_a_tenant_explicitly(self):
        """Source-level, because the HTTP tests above cannot discriminate (see the class
        docstring).

        Written as a SWEEP of the file rather than a list of the two routes I first noticed,
        because the defect was never "these two routes". `insights.py` carries a comment
        explaining that the storage fallback chain ends at the FIRST tenant, and FIVE of its
        routes sat around that comment without passing one: /audit/decisions, /audit/errors,
        /audit/decisions/count, /audit/promotions and /analytics/rejection-drivers. Fixing the
        two and leaving three is the pattern this round keeps finding."""
        src = (APP_ROOT / "app" / "routers" / "insights.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        import re

        untenanted = [
            call for call in re.findall(r"main\.storage\.\w+\([^)]*", code)
            if "tenant_id=" not in call
        ]
        self.assertEqual(untenanted, [],
                         "these storage calls resolve the tenant through the fallback chain, "
                         "whose last link is the first tenant in the table: %s" % untenanted)


if __name__ == "__main__":
    unittest.main()
