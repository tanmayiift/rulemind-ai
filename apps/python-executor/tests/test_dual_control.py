"""Two-person control (maker != checker) for promotion to production.

When a workspace enables dual control, promoting an asset dev -> uat -> prod must involve two
different authenticated members: the person who approves the prod promotion cannot be the same one
who promoted it to UAT. The ledger records the *authenticated* actor (not a client-supplied label),
so the check is trustworthy.
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

import app.main as app_main  # noqa: E402
from app.context import tenant_scope  # noqa: E402
from app.storage import Storage  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class DualControlTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "dc.db"))
        self.tenant = app_main.storage.default_tenant_id
        # A seeded rule, reset to dev + a passing test state so it can be promoted dev->uat->prod.
        rule = app_main.storage.list_rules(tenant_id=self.tenant)[0]
        self.rule_id = rule["id"]
        app_main.storage.update_rule(
            self.rule_id, {"status": "dev", "last_test_result": {"passed": True}}, tenant_id=self.tenant
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _set_dual_control(self, enabled: bool):
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine = dict(settings.get("engine_config", {}) or {})
        engine["require_dual_control"] = enabled
        app_main.storage.update_settings({"engine_config": engine}, tenant_id=self.tenant)

    def _promote(self, actor: str, label: str):
        with tenant_scope(self.tenant, actor):
            return app_main.promote_entity("rule", self.rule_id, label, "reason")

    def test_same_actor_blocked_from_self_approving_prod(self):
        self._set_dual_control(True)
        self._promote("member:alice", "Alice")  # dev -> uat (maker)
        with self.assertRaises(HTTPException) as ctx:
            self._promote("member:alice", "Alice")  # uat -> prod (same actor) -> blocked
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Dual control", ctx.exception.detail)
        # Asset must NOT have advanced to prod.
        self.assertEqual(app_main.storage.get_rule(self.rule_id, tenant_id=self.tenant)["status"], "uat")

    def test_different_actor_can_approve_prod(self):
        self._set_dual_control(True)
        self._promote("member:alice", "Alice")  # dev -> uat (maker)
        promoted = self._promote("member:bob", "Bob")  # uat -> prod (checker) -> allowed
        self.assertEqual(promoted["status"], "prod")

    def test_disabled_allows_single_actor(self):
        self._set_dual_control(False)  # default posture
        self._promote("member:alice", "Alice")  # dev -> uat
        promoted = self._promote("member:alice", "Alice")  # uat -> prod, same actor, allowed
        self.assertEqual(promoted["status"], "prod")

    def test_ledger_records_authenticated_actor_not_label(self):
        self._set_dual_control(True)
        self._promote("member:alice", "Totally Bob")  # client label lies; actor is alice
        maker = app_main.storage.last_promotion_actor("rule", self.rule_id, "uat", tenant_id=self.tenant)
        self.assertEqual(maker, "member:alice", "ledger must record the authenticated actor")


if __name__ == "__main__":
    unittest.main()
