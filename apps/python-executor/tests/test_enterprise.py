from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import app.main as app_main
from app.main import BatchSimulationRequest, ConnectorUpdateRequest, PromoteRequest, RuleUpsertRequest, TestPayloadRequest
from app.storage import Storage


class RuleMindEnterpriseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.tempdir.name, "rulemind-test.db")
        app_main.storage = Storage(path=self.database_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_v2_rule_tree_evaluates_and_returns_group_results(self) -> None:
        created = app_main.create_rule(
            RuleUpsertRequest(
                name="Nested Approval Tree",
                ruleFormat="v2",
                tree={
                    "type": "group",
                    "logic": "OR",
                    "children": [
                        {
                            "type": "group",
                            "logic": "AND",
                            "children": [
                                {"type": "condition", "variable": "bureau_score", "operator": ">=", "value": "700"},
                                {"type": "condition", "variable": "avg_balance", "operator": ">=", "value": "20000"},
                            ],
                            "onPass": "approve",
                            "onFail": "reject",
                        },
                        {
                            "type": "group",
                            "logic": "AND",
                            "children": [
                                {"type": "condition", "variable": "age", "operator": ">=", "value": "21"},
                                {"type": "not", "child": {"type": "condition", "variable": "device_risk", "operator": "==", "value": "1"}},
                            ],
                            "onPass": "approve",
                            "onFail": "reject",
                        },
                    ],
                    "onPass": "approve",
                    "onFail": "reject",
                },
                status="dev",
            )
        )

        self.assertEqual(created["rule_format"], "v2")
        self.assertTrue(created["tree"])
        self.assertGreaterEqual(len(created["nodes"]), 4)

        executed = app_main.test_rule(created["id"], TestPayloadRequest(payload={}))

        self.assertTrue(executed["result"]["passed"])
        self.assertEqual(executed["result"]["outcome"], "approve")
        self.assertGreaterEqual(len(executed["result"]["groupResults"]), 3)
        self.assertIn("ELSE", executed["expression"])

        graph = app_main.variable_graph()
        self.assertTrue(
            any(edge["from"] == "bureau_score" and edge["to"] == created["id"] for edge in graph["edges"]),
            "Flattened compatibility nodes should keep variable graph edges for v2 rules.",
        )

    def test_variable_graph_includes_seeded_rule_scorecard_and_policy_dependencies(self) -> None:
        graph = app_main.variable_graph()
        edges = {(edge["from"], edge["to"], edge["relation"]) for edge in graph["edges"]}

        self.assertIn(("bureau_score", "r1", "used_by_rule"), edges)
        self.assertIn(("bureau_score", "sc1", "used_by_scorecard"), edges)
        self.assertIn(("r1", "policy_pl_underwriting", "used_by_policy"), edges)
        self.assertIn(("sc1", "policy_pl_underwriting", "used_by_policy"), edges)

    def test_failed_rule_test_writes_audit_error_event(self) -> None:
        response = app_main.test_rule("r2", TestPayloadRequest(payload={}))
        self.assertFalse(response["result"]["passed"])

        errors = app_main.audit_errors()
        self.assertTrue(
            any(
                error["scope"] == "rules"
                and error["entity_type"] == "rule"
                and error["entity_id"] == "r2"
                and error["stage"] == "test"
                for error in errors
            )
        )

    def test_connector_secret_values_are_masked_and_not_stored_in_plaintext(self) -> None:
        updated = app_main.update_connector(
            "bureau",
            ConnectorUpdateRequest(
                config={
                    "auth_type": "api_key",
                    "base_url": "https://bureau.example",
                    "api_key": "rm_secret_value",
                    "webhook_url": "https://hooks.example/rulemind",
                }
            ),
        )

        self.assertEqual(updated["config"]["api_key"], "••••••••")
        self.assertEqual(updated["config"]["webhook_url"], "https://hooks.example/rulemind")

        with sqlite3.connect(self.database_path) as connection:
            encrypted_value = connection.execute(
                "select encrypted_config from connectors where tenant_id = ? and public_id = ?",
                (app_main.storage.default_tenant_id, "bureau"),
            ).fetchone()[0]

        self.assertNotIn("rm_secret_value", encrypted_value)
        self.assertNotIn("https://bureau.example", encrypted_value)

        masked = app_main.storage.get_connector("bureau")
        unmasked = app_main.storage.get_connector("bureau", include_secrets=True)
        self.assertEqual(masked["config"]["api_key"], "••••••••")
        self.assertEqual(unmasked["config"]["api_key"], "rm_secret_value")
        self.assertEqual(unmasked["config"]["base_url"], "https://bureau.example")

    def test_rule_promotion_requires_a_passing_test_result(self) -> None:
        created = app_main.create_rule(
            RuleUpsertRequest(
                name="Promotion Gate",
                nodes=[
                    {"id": "n1", "type": "condition", "variable": "bureau_score", "operator": ">=", "value": "700", "label": "Condition"},
                    {"id": "n2", "type": "approve", "label": "Approve"},
                ],
                status="dev",
            )
        )

        with self.assertRaises(app_main.HTTPException) as raised:
            app_main.promote_rule(created["id"], PromoteRequest(promoted_by="tests", reason="should be blocked"))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Latest test must pass", str(raised.exception.detail))

    def test_decide_batch_executes_multiple_payloads(self) -> None:
        response = app_main.batch_decide(
            BatchSimulationRequest(
                targetType="decide",
                targetId="policy_pl_underwriting",
                payloads=[
                    {},
                    {
                        "bureau": {"scores": [{"scoreName": "CIBILTUSC3", "score": "00620"}], "accounts": [], "enquiries": []},
                        "bank": {"summary": {"avgBalance": 9000}, "salary": [], "bounces": [{"amount": 500}]},
                        "kyc": {"nameMatchScore": 95, "age": 28, "panValid": True},
                        "device": {"lendingAppsCount": 1, "vpnDetected": False, "rootDetected": False},
                    },
                ],
            )
        )

        self.assertEqual(response["count"], 2)
        self.assertEqual(len(response["rows"]), 2)
        self.assertIn(response["rows"][0]["result"]["outcome"], {"approve", "review", "reject"})
        self.assertIn(response["rows"][1]["result"]["outcome"], {"approve", "review", "reject"})


if __name__ == "__main__":
    unittest.main()
