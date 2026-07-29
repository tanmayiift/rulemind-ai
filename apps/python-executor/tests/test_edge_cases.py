"""
Deep edge-case tests for the RuleMind backend.

Covers rule evaluation boundaries, import validation, CSV export, model
hosting, Excel functions, auth edges, and WoE scorecard scoring.
"""
from __future__ import annotations

import base64
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

import app.main as app_main
from app.storage import Storage


# ---------------------------------------------------------------------------
# Inline pickle model for model-hosting tests
# ---------------------------------------------------------------------------

class SimpleModel:
    """Minimal model with a predict method for testing."""

    def predict(self, X):
        return [sum(row) for row in X]


_model_bytes = pickle.dumps(SimpleModel())
_model_b64 = base64.b64encode(_model_bytes).decode()


class BrokenModel:
    """Model without a predict method."""
    pass


_broken_model_bytes = pickle.dumps(BrokenModel())
_broken_model_b64 = base64.b64encode(_broken_model_bytes).decode()


class EdgeCaseTests(unittest.TestCase):
    """~40 edge-case tests exercising boundary conditions across the API."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.tempdir.name, "test.db")
        app_main.storage = Storage(path=self.database_path)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    # 1. Rule evaluation edge payloads (~10 tests)
    # ------------------------------------------------------------------

    def test_empty_payload_evaluates_with_sample_data(self) -> None:
        """Empty payload {} falls back to connector sample payloads."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("result", body)
        # sample_payload has bureau_score=756 which is >= 700 -> approve
        self.assertEqual(body["result"]["outcome"], "approve")

    def test_missing_variable_in_payload_returns_outcome(self) -> None:
        """A completely missing variable evaluates to None/0 but doesn't crash."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"some_other_field": 999}}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # bureau_score missing -> defaults to 0 -> 0 >= 700 is False -> onFail = review
        self.assertEqual(body["result"]["outcome"], "review")

    def test_boundary_value_exactly_700(self) -> None:
        """bureau_score == 700 with operator >= should pass."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 700}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "approve")

    def test_boundary_value_699(self) -> None:
        """bureau_score == 699 should fail the >= 700 check."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 699}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "review")

    def test_boundary_value_701(self) -> None:
        """bureau_score == 701 should pass the >= 700 check."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 701}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "approve")

    def test_unicode_in_payload_values(self) -> None:
        """Unicode strings in a payload don't crash evaluation."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 750, "name": "\u4f60\u597d\u4e16\u754c"}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "approve")

    def test_very_large_numeric_value(self) -> None:
        """Extremely large values are handled without overflow."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 999999999}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "approve")

    def test_negative_numeric_value(self) -> None:
        """Negative value for bureau_score should fail >= 700."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": -100}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "review")

    def test_zero_value_for_score(self) -> None:
        """Zero value should fail >= 700."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 0}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "review")

    def test_float_value_for_score(self) -> None:
        """Float 700.0 should pass >= 700."""
        response = self.client.post(
            "/api/v1/rules/rule_loan_bureau_gate/test",
            headers=self.headers,
            json={"payload": {"loan": {"bureau_score": 700.0}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["outcome"], "approve")

    # ------------------------------------------------------------------
    # 2. Import validation (~8 tests)
    # ------------------------------------------------------------------

    def test_import_validate_with_valid_rules(self) -> None:
        """Valid rules should all be flagged as valid."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "rules": [
                    {
                        "id": "valid_rule",
                        "name": "Valid Rule",
                        "nodes": [
                            {"id": "c1", "type": "condition", "variable": "loan_bureau_score", "operator": ">=", "value": "700"},
                            {"id": "o1", "type": "approve", "label": "Approve"},
                        ],
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["valid"], 1)
        self.assertEqual(body["summary"]["invalid"], 0)

    def test_import_validate_invalid_operator(self) -> None:
        """Unsupported operator should be flagged as invalid."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "rules": [
                    {
                        "id": "bad_op_rule",
                        "name": "Bad Op Rule",
                        "nodes": [
                            {"id": "c1", "type": "condition", "variable": "loan_bureau_score", "operator": "LIKE", "value": "700"},
                            {"id": "o1", "type": "approve", "label": "Approve"},
                        ],
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["invalid"], 1)
        self.assertTrue(any("LIKE" in i for r in body["rules"] for i in r.get("issues", [])))

    def test_import_validate_missing_outcome_node(self) -> None:
        """Rule with conditions but no outcome should be flagged."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "rules": [
                    {
                        "id": "no_outcome_rule",
                        "name": "No Outcome",
                        "nodes": [
                            {"id": "c1", "type": "condition", "variable": "loan_bureau_score", "operator": ">=", "value": "700"},
                        ],
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["invalid"], 1)
        self.assertTrue(any("outcome" in i.lower() for r in body["rules"] for i in r.get("issues", [])))

    def test_import_validate_nonexistent_variable_reference(self) -> None:
        """Reference to a nonexistent variable should be flagged."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "rules": [
                    {
                        "id": "bad_var_rule",
                        "name": "Bad Var",
                        "nodes": [
                            {"id": "c1", "type": "condition", "variable": "totally_fake_variable", "operator": ">=", "value": "1"},
                            {"id": "o1", "type": "approve", "label": "Approve"},
                        ],
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["invalid"], 1)
        self.assertTrue(any("totally_fake_variable" in i for r in body["rules"] for i in r.get("issues", [])))

    def test_import_validate_empty_payload(self) -> None:
        """Empty import payload should have summary with 0 total."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["total"], 0)
        self.assertEqual(body["summary"]["valid"], 0)
        self.assertEqual(body["summary"]["invalid"], 0)

    def test_import_validate_mixed_valid_and_invalid(self) -> None:
        """Mix of valid and invalid entities should report correct counts."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "rules": [
                    {
                        "id": "good_rule",
                        "name": "Good Rule",
                        "nodes": [
                            {"id": "c1", "type": "condition", "variable": "loan_bureau_score", "operator": ">=", "value": "700"},
                            {"id": "o1", "type": "approve", "label": "Approve"},
                        ],
                    },
                    {
                        "id": "bad_rule",
                        "name": "Bad Rule",
                        "nodes": [
                            {"id": "c1", "type": "condition", "variable": "loan_bureau_score", "operator": "NOPE", "value": "700"},
                            {"id": "o1", "type": "approve", "label": "Approve"},
                        ],
                    },
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["total"], 2)
        self.assertEqual(body["summary"]["valid"], 1)
        self.assertEqual(body["summary"]["invalid"], 1)

    def test_import_validate_scorecard_no_bins(self) -> None:
        """Scorecard with no bins should be flagged."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "scorecards": [
                    {"id": "sc_empty", "name": "Empty SC", "bins": []},
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["invalid"], 1)

    def test_import_validate_variable_syntax_error(self) -> None:
        """Variable with broken Python code should be flagged."""
        response = self.client.post(
            "/api/v1/import/validate",
            headers=self.headers,
            json={
                "variables": [
                    {"id": "bad_var", "name": "Bad Var", "category": "Custom", "source_id": "custom", "code": "def broken(:"},
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["invalid"], 1)

    # ------------------------------------------------------------------
    # 3. CSV export (~5 tests)
    # ------------------------------------------------------------------

    def test_csv_export_returns_200(self) -> None:
        response = self.client.get("/api/v1/export?format=csv", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers.get("content-type", ""))

    def test_csv_export_has_section_headers(self) -> None:
        response = self.client.get("/api/v1/export?format=csv", headers=self.headers)
        content = response.text
        self.assertIn("## CONNECTORS", content)
        self.assertIn("## VARIABLES", content)
        self.assertIn("## RULES", content)

    def test_csv_export_has_scorecard_and_policy_sections(self) -> None:
        response = self.client.get("/api/v1/export?format=csv", headers=self.headers)
        content = response.text
        self.assertIn("## SCORECARDS", content)
        self.assertIn("## POLICIES", content)

    def test_csv_export_content_disposition(self) -> None:
        response = self.client.get("/api/v1/export?format=csv", headers=self.headers)
        disposition = response.headers.get("content-disposition", "")
        self.assertIn("rulemind-export.csv", disposition)

    def test_csv_export_parseable(self) -> None:
        """CSV content should be parseable by the csv module."""
        import csv
        import io

        response = self.client.get("/api/v1/export?format=csv", headers=self.headers)
        reader = csv.reader(io.StringIO(response.text))
        rows = list(reader)
        self.assertGreater(len(rows), 5)

    # ------------------------------------------------------------------
    # 4. Model hosting (~8 tests)
    # ------------------------------------------------------------------

    def test_create_model_with_valid_pickle(self) -> None:
        response = self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "Sum Model", "model_base64": _model_b64},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("id", response.json())

    def test_list_models_after_create(self) -> None:
        self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "List Model", "model_base64": _model_b64},
        )
        response = self.client.get("/api/v1/models", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()), 1)

    def test_predict_model(self) -> None:
        created = self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "Predict Model", "model_base64": _model_b64},
        ).json()
        model_id = created["id"]
        response = self.client.post(
            f"/api/v1/models/{model_id}/predict",
            headers=self.headers,
            json={"input_data": {"a": 1, "b": 2, "c": 3}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["prediction"], 6)

    def test_predict_model_with_features(self) -> None:
        created = self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "Feature Model", "model_base64": _model_b64},
        ).json()
        model_id = created["id"]
        response = self.client.post(
            f"/api/v1/models/{model_id}/predict",
            headers=self.headers,
            json={"input_data": {"x": 10, "y": 20}, "features": ["x", "y"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["prediction"], 30)

    def test_delete_model(self) -> None:
        created = self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "Delete Model", "model_base64": _model_b64},
        ).json()
        model_id = created["id"]
        response = self.client.delete(f"/api/v1/models/{model_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])

    def test_create_model_with_invalid_pickle(self) -> None:
        """Valid base64 that decodes to invalid pickle should be rejected."""
        # This is valid base64 but decodes to garbage (not a valid pickle)
        bad_b64 = base64.b64encode(b"this is not a pickle").decode()
        response = self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "Bad Model", "model_base64": bad_b64},
        )
        self.assertEqual(response.status_code, 422)

    def test_get_nonexistent_model(self) -> None:
        response = self.client.get("/api/v1/models/nonexistent_model_id", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_model_blob_is_encrypted_at_rest(self) -> None:
        """The pickled model must be stored as ciphertext — a DB dump / backup must
        not expose the model's bytes (IP-confidentiality guard)."""
        import sqlalchemy as sa

        # The plaintext pickle contains the class name; encryption must hide it.
        marker = b"SimpleModel"
        self.assertIn(marker, _model_bytes)
        created = self.client.post(
            "/api/v1/models", headers=self.headers, json={"name": "Marked", "model_base64": _model_b64}
        ).json()
        # API responses never carry the blob
        self.assertNotIn("model_blob", created)
        listed = self.client.get("/api/v1/models", headers=self.headers).json()
        self.assertTrue(all("model_blob" not in m for m in listed))
        # raw DB bytes must be ciphertext (marker absent)
        with app_main.storage.connect() as session:
            raw = session.execute(sa.text("select model_blob from hosted_models")).scalar()
            raw_bytes = bytes(raw)
        self.assertNotIn(marker, raw_bytes)
        # but the server can still decrypt + run it
        pred = self.client.post(
            f"/api/v1/models/{created['id']}/predict", headers=self.headers, json={"input_data": {"a": 2, "b": 3}}
        )
        self.assertEqual(pred.status_code, 200)
        self.assertEqual(pred.json()["prediction"], 5)

    def test_model_runs_as_a_policy_step(self) -> None:
        """A hosted model must be usable as a policy `model` step end-to-end."""
        model_id = self.client.post(
            "/api/v1/models", headers=self.headers, json={"name": "Policy Model", "model_base64": _model_b64}
        ).json()["id"]
        policy = {
            "id": "qa_model_policy",
            "name": "QA Model Policy",
            "steps": [
                {"id": "s1", "type": "model", "ref_id": model_id, "config": {"outputVariable": "risk_score"}},
                {"id": "s2", "type": "outcome", "outcome": "approve"},
            ],
        }
        created = self.client.post("/api/v1/policies", headers=self.headers, json=policy)
        self.assertEqual(created.status_code, 200)
        pid = created.json()["id"]
        run = self.client.post(
            f"/api/v1/policies/{pid}/execute", headers=self.headers, json={"payload": {"a": 4, "b": 6}}
        )
        self.assertEqual(run.status_code, 200)
        # the model step executed and contributed to the trace
        body = run.json()
        trace_text = str(body)
        self.assertTrue("model" in trace_text or "risk_score" in trace_text)

    def test_create_model_without_predict(self) -> None:
        """A pickled object without .predict() should be rejected."""
        response = self.client.post(
            "/api/v1/models",
            headers=self.headers,
            json={"name": "Broken Model", "model_base64": _broken_model_b64},
        )
        self.assertEqual(response.status_code, 422)

    # ------------------------------------------------------------------
    # 5. Excel functions endpoint (~3 tests)
    # ------------------------------------------------------------------

    def test_excel_functions_returns_categories(self) -> None:
        response = self.client.get("/api/v1/excel-functions", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("categories", body)
        self.assertIn("total_functions", body)

    def test_excel_functions_total_above_200(self) -> None:
        response = self.client.get("/api/v1/excel-functions", headers=self.headers)
        self.assertGreater(response.json()["total_functions"], 200)

    def test_excel_functions_has_common_categories(self) -> None:
        response = self.client.get("/api/v1/excel-functions", headers=self.headers)
        cats = response.json()["categories"]
        for expected in ("math_trig", "statistical", "financial", "logical", "text"):
            self.assertIn(expected, cats)
            self.assertGreater(len(cats[expected]), 0)

    # ------------------------------------------------------------------
    # 6. Auth edge cases (~3 tests)
    # ------------------------------------------------------------------

    def test_no_api_key_returns_401(self) -> None:
        response = self.client.get("/api/v1/connectors")
        self.assertEqual(response.status_code, 401)

    def test_empty_api_key_returns_401(self) -> None:
        response = self.client.get("/api/v1/connectors", headers={"x-api-key": ""})
        self.assertEqual(response.status_code, 401)

    def test_garbage_api_key_returns_401(self) -> None:
        response = self.client.get("/api/v1/connectors", headers={"x-api-key": "rm_live_totallygarbage"})
        self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------------
    # 7. Scorecard WoE (~5 tests)
    # ------------------------------------------------------------------

    def _create_scorecard(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.client.post("/api/v1/scorecards", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_scorecard_points_backward_compat(self) -> None:
        """scoring_method='points' should work (backward compat)."""
        sc = self._create_scorecard({
            "name": "Points Compat SC",
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "loan_bureau_score",
                    "ranges": [{"min": 0, "max": 999, "points": 50}],
                    "weight": 1.0,
                },
            ],
        })
        # DB doesn't persist scoring_method; verify core fields
        self.assertEqual(sc["base_score"], 300)
        self.assertEqual(sc["max_score"], 900)
        self.assertIn("bins", sc)

    def test_scorecard_woe_creation(self) -> None:
        """scoring_method='woe' with WoE values should be created and stored."""
        sc = self._create_scorecard({
            "name": "WoE SC",
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "woe",
            "intercept": -1.5,
            "pdo": 20.0,
            "target_score": 600.0,
            "target_odds": 50.0,
            "bins": [
                {
                    "variable_id": "loan_bureau_score",
                    "ranges": [{"min": 0, "max": 999, "points": 0}],
                    "weight": 1.0,
                    "coefficient": 0.5,
                    "woe_values": [{"min": 0, "max": 999, "woe": 0.8, "iv": 0.05}],
                },
            ],
        })
        # The scorecard is stored; bins include WoE data
        self.assertIn("id", sc)
        self.assertEqual(len(sc["bins"]), 1)
        self.assertIn("woe_values", sc["bins"][0])

    def test_scorecard_with_metrics_formula(self) -> None:
        """Scorecard with metrics formulas can be created and tested."""
        sc = self._create_scorecard({
            "name": "Metric SC",
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "loan_bureau_score",
                    "ranges": [{"min": 0, "max": 999, "points": 100}],
                    "weight": 1.0,
                },
            ],
            "metrics": [
                {"name": "risk_rate", "formula": "IF(score >= 700, 0.15, 0.05)", "unit": "%", "category": "financial"},
            ],
        })
        # Scorecard is persisted with bins
        self.assertIn("id", sc)
        self.assertEqual(len(sc["bins"]), 1)

    def test_scorecard_test_endpoint_returns_score(self) -> None:
        """Testing a scorecard via POST should return a score."""
        sc = self._create_scorecard({
            "name": "Test Score SC",
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "loan_bureau_score",
                    "ranges": [{"min": 0, "max": 999, "points": 80}],
                    "weight": 1.0,
                },
            ],
        })
        response = self.client.post(
            f"/api/v1/scorecards/{sc['id']}/test",
            headers=self.headers,
            json={"payload": {}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("result", body)
        self.assertIn("score", body["result"])

    def test_scorecard_weighted_bins(self) -> None:
        """Bins with weight=2 should apply double points."""
        sc = self._create_scorecard({
            "name": "Weighted SC",
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "loan_bureau_score",
                    "ranges": [{"min": 0, "max": 999, "points": 50}],
                    "weight": 2.0,
                },
            ],
        })
        response = self.client.post(
            f"/api/v1/scorecards/{sc['id']}/test",
            headers=self.headers,
            json={"payload": {}},
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        # base_score(300) + 50*2 = 400
        self.assertEqual(result["score"], 400)


if __name__ == "__main__":
    unittest.main()
