"""Onboarding journey — unit + smoke + regression.

Covers the self-serve flow: signup (public) → dev key → verify (gated on a real
decision) → AI opt-in → prod key (gated on verification), plus the guard rails.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)  # apikey mode so issued keys resolve to their tenant

import app.main as app_main
from app.storage import Storage


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "onboard.db"))
        self.client = TestClient(app_main.app)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _signup(self, company="Acme Lending"):
        return self.client.post("/api/v1/onboarding/signup",
                                json={"company": company, "contact_email": "a@acme.com", "use_case": "loans"}).json()

    # ---- smoke: the happy path end to end ----
    def test_full_journey(self) -> None:
        s = self._signup()
        self.assertEqual(s["environment"], "dev")
        self.assertTrue(s["api_key"])
        H = {"x-api-key": s["api_key"]}
        # workspace was seeded so a decision is possible immediately
        self.assertEqual(self.client.get("/api/v1/policies", headers=H).status_code, 200)
        # make the first decision, then verify
        self.assertEqual(self.client.post("/api/v1/decide", headers=H,
                         json={"policyId": "policy_instant_personal_loan", "payload": {}}).status_code, 200)
        self.assertTrue(self.client.post("/api/v1/onboarding/verify", headers=H).json()["steps"]["verified"])
        self.client.post("/api/v1/onboarding/ai", headers=H, json={"opted_in": False})
        prod = self.client.post("/api/v1/onboarding/request-prod", headers=H).json()
        self.assertEqual(prod["environment"], "prod")
        self.assertTrue(prod["status"]["complete"])
        # the prod key works
        self.assertEqual(self.client.get("/api/v1/policies", headers={"x-api-key": prod["api_key"]}).status_code, 200)

    # ---- unit/regression: prod key is gated on verification ----
    def test_prod_key_blocked_until_verified(self) -> None:
        s = self._signup()
        H = {"x-api-key": s["api_key"]}
        self.assertEqual(self.client.post("/api/v1/onboarding/request-prod", headers=H).status_code, 409)

    def test_verify_requires_a_decision(self) -> None:
        s = self._signup()
        H = {"x-api-key": s["api_key"]}
        self.assertEqual(self.client.post("/api/v1/onboarding/verify", headers=H).status_code, 409)

    def test_prod_key_issued_once(self) -> None:
        s = self._signup()
        H = {"x-api-key": s["api_key"]}
        self.client.post("/api/v1/decide", headers=H, json={"policyId": "policy_instant_personal_loan", "payload": {}})
        self.client.post("/api/v1/onboarding/verify", headers=H)
        self.assertEqual(self.client.post("/api/v1/onboarding/request-prod", headers=H).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/onboarding/request-prod", headers=H).status_code, 409)  # only once

    # ---- regression: signup is isolated per workspace ----
    def test_signups_are_isolated(self) -> None:
        a = self._signup("Alpha Corp")
        b = self._signup("Beta Corp")
        self.assertNotEqual(a["tenant_id"], b["tenant_id"])
        self.assertNotEqual(a["api_key"], b["api_key"])
        # each key only sees its own workspace onboarding state
        sa = self.client.get("/api/v1/onboarding/status", headers={"x-api-key": a["api_key"]}).json()
        self.assertEqual(sa["onboarding"]["org"]["company"], "Alpha Corp")

    # ---- unit: AI opt-in is recorded but does not enable AI without a key ----
    def test_ai_optin_does_not_enable_without_key(self) -> None:
        s = self._signup()
        H = {"x-api-key": s["api_key"]}
        view = self.client.post("/api/v1/onboarding/ai", headers=H, json={"opted_in": True}).json()
        self.assertTrue(view["steps"]["ai"])          # choice recorded
        self.assertFalse(view["ai_configured"])         # but no key → AI stays off

    # ---- unit: signup endpoint is public (no key needed) ----
    def test_signup_is_public(self) -> None:
        resp = self.client.post("/api/v1/onboarding/signup", json={"company": "Public Co", "contact_email": "p@co.com"})
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
