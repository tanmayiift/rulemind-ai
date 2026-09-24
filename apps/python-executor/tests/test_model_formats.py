"""Model formats a credit shop actually has (plan 6.3): ONNX, PMML, XGBoost JSON, LightGBM.

`model_executor` was pickle only. The fixtures in tests/fixtures/models were produced by
generate_fixtures.py: one dataset, fitted by each library, and `expected.json` holds what each
NATIVE library predicted for the probe rows. So every assertion here is "RuleMind's loader agrees
with the library that produced the model" -- not RuleMind agreeing with itself.

These run through the real isolated worker pool (execute_model / validate_model), the same path
the /models endpoints, the `model` policy step and evaluations use.
"""
from __future__ import annotations

import base64
import json
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import model_executor  # noqa: E402
from app.model_evaluation import score_rows_with_model  # noqa: E402
from app.pmml import PMMLModel, PMMLUnsupported  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

FIXTURES = APP_ROOT / "tests" / "fixtures" / "models"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
PROBES = EXPECTED["probes"]
FEATURES = EXPECTED["features"]


def _blob(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _proba1(result):
    probabilities = result["probabilities"]
    return float(probabilities[-1])


class FormatDetectionTest(unittest.TestCase):
    def test_each_fixture_is_detected_from_its_bytes(self):
        for name, fmt in (("logistic.pkl", "pickle"), ("logistic.onnx", "onnx"), ("logistic.pmml", "pmml"),
                          ("xgboost.json", "xgboost_json"), ("lightgbm.txt", "lightgbm_text")):
            self.assertEqual(model_executor.detect_format(_blob(name)), fmt, name)

    def test_the_format_is_never_taken_from_a_declared_type(self):
        """Uploading an ONNX file as model_type "sklearn" must still load it as ONNX -- and,
        the direction that matters, nothing labelled ONNX can reach pickle.loads."""
        self.assertEqual(model_executor.detect_format(b"\x08\x07" + b"\x00" * 10), "onnx")


class NativeParityTest(unittest.TestCase):
    """Each format scored through the isolated worker equals its library's own prediction."""

    def _assert_matches(self, blob_name, expected_key, tolerance=1e-5):
        blob = _blob(blob_name)
        for row, want in zip(PROBES, EXPECTED[expected_key], strict=True):
            result = model_executor.execute_model(blob, row, features=FEATURES)
            self.assertIsNone(result.get("error"), result.get("error"))
            self.assertAlmostEqual(_proba1(result), want, delta=tolerance, msg=(blob_name, row))

    def test_onnx_matches_scikit_learn(self):
        self._assert_matches("logistic.onnx", "sklearn_logistic_proba1")

    def test_pmml_regression_matches_scikit_learn(self):
        self._assert_matches("logistic.pmml", "sklearn_logistic_proba1")

    def test_xgboost_json_matches_xgboost(self):
        self._assert_matches("xgboost.json", "xgboost_proba1")

    def test_lightgbm_text_matches_lightgbm(self):
        self._assert_matches("lightgbm.txt", "lightgbm_proba1", tolerance=1e-9)

    def test_pickle_still_works(self):
        self._assert_matches("logistic.pkl", "sklearn_logistic_proba1")

    def test_a_named_booster_scores_by_name_not_position(self):
        """The caller lists features in a different order. A positional loader would put
        dti's value in the bureau_score column and return a confident wrong score."""
        blob = _blob("xgboost.json")
        shuffled = list(reversed(FEATURES))
        result = model_executor.execute_model(blob, PROBES[0], features=shuffled)
        self.assertAlmostEqual(_proba1(result), EXPECTED["xgboost_proba1"][0], delta=1e-5)
        self.assertEqual(result["input_features"], FEATURES)

    def test_a_missing_feature_is_the_callers_error(self):
        row = {k: v for k, v in PROBES[0].items() if k != "dti"}
        for name in ("logistic.onnx", "xgboost.json", "lightgbm.txt", "logistic.pmml"):
            result = model_executor.execute_model(_blob(name), row, features=FEATURES)
            self.assertIsNone(result.get("prediction"), name)
            self.assertIn("dti", result["error"], name)


class BatchScoringTest(unittest.TestCase):
    def test_batch_equals_row_by_row(self):
        blob = _blob("xgboost.json")
        batch = model_executor.execute_model_batch(blob, PROBES, features=FEATURES)
        self.assertIsNone(batch["error"])
        self.assertEqual(len(batch["predictions"]), len(PROBES))
        for got, want in zip(batch["probabilities"], EXPECTED["xgboost_proba1"], strict=True):
            self.assertAlmostEqual(got[-1], want, delta=1e-5)

    def test_batches_are_chunked_and_reassembled_in_order(self):
        rows = PROBES * 3
        with mock.patch.object(model_executor, "BATCH_CHUNK_ROWS", 4):
            batch = model_executor.execute_model_batch(_blob("lightgbm.txt"), rows, features=FEATURES)
        want = [round(w, 9) for w in EXPECTED["lightgbm_proba1"] * 3]
        self.assertEqual([round(p[-1], 9) for p in batch["probabilities"]], want)
        # Both lists: a reassembly bug in one of them pairs every score with the wrong row.
        self.assertEqual([round(p, 9) for p in batch["predictions"]], want)

    def test_a_bad_row_names_its_index(self):
        rows = [dict(PROBES[0]), {**PROBES[1], "dti": "high"}]
        batch = model_executor.execute_model_batch(_blob("logistic.onnx"), rows, features=FEATURES)
        self.assertIn("Row 1", batch["error"])

    def test_evaluation_scores_through_the_batch_path_in_one_worker_call(self):
        rows = [{**row, "label": i % 2} for i, row in enumerate(PROBES)]
        with mock.patch.object(model_executor, "_run_isolated", wraps=model_executor._run_isolated) as spy:
            scored = score_rows_with_model(_blob("logistic.onnx"), rows, FEATURES, "label")
        self.assertEqual(spy.call_count, 1, "evaluation went back to one worker round trip per row")
        for got, want in zip(scored, EXPECTED["sklearn_logistic_proba1"], strict=True):
            self.assertAlmostEqual(got["score"], want, delta=1e-5)


class PickleSwitchTest(unittest.TestCase):
    def test_pickle_can_be_refused_at_upload_and_at_scoring(self):
        with mock.patch.object(model_executor, "ALLOW_PICKLE", False):
            self.assertFalse(model_executor.validate_model(_blob("logistic.pkl"))["valid"])
            result = model_executor.execute_model(_blob("logistic.pkl"), PROBES[0], features=FEATURES)
            self.assertIn("MODEL_ALLOW_PICKLE", result["error"])
            # The other formats are unaffected.
            self.assertTrue(model_executor.validate_model(_blob("logistic.onnx"))["valid"])

    def test_the_refusal_happens_before_the_worker(self):
        """A refused pickle must never reach pickle.loads, even in the isolated worker."""
        with mock.patch.object(model_executor, "ALLOW_PICKLE", False), \
                mock.patch.object(model_executor, "_run_isolated") as isolated:
            model_executor.execute_model(_blob("logistic.pkl"), PROBES[0], features=FEATURES)
            model_executor.validate_model(_blob("logistic.pkl"))
        isolated.assert_not_called()


SCORECARD = b"""<?xml version="1.0"?>
<PMML xmlns="http://www.dmg.org/PMML-4_4" version="4.4">
  <DataDictionary>
    <DataField name="age" optype="continuous" dataType="double"/>
    <DataField name="residence" optype="categorical" dataType="string"/>
    <DataField name="score" optype="continuous" dataType="double"/>
  </DataDictionary>
  <Scorecard functionName="regression" initialScore="100" useReasonCodes="true"
             reasonCodeAlgorithm="pointsBelow" baselineMethod="other">
    <MiningSchema>
      <MiningField name="age"/><MiningField name="residence"/>
      <MiningField name="score" usageType="predicted"/>
    </MiningSchema>
    <Characteristics>
      <Characteristic name="age_band" reasonCode="RC_AGE" baselineScore="20">
        <Attribute partialScore="5"><SimplePredicate field="age" operator="lessThan" value="25"/></Attribute>
        <Attribute partialScore="20">
          <CompoundPredicate booleanOperator="and">
            <SimplePredicate field="age" operator="greaterOrEqual" value="25"/>
            <SimplePredicate field="age" operator="lessThan" value="50"/>
          </CompoundPredicate>
        </Attribute>
        <Attribute partialScore="30"><SimplePredicate field="age" operator="greaterOrEqual" value="50"/></Attribute>
      </Characteristic>
      <Characteristic name="residence" reasonCode="RC_RES" baselineScore="40">
        <Attribute partialScore="25">
          <SimpleSetPredicate field="residence" booleanOperator="isIn"><Array type="string">owner "company housing"</Array></SimpleSetPredicate>
        </Attribute>
        <Attribute partialScore="0"><SimplePredicate field="residence" operator="isMissing"/></Attribute>
        <Attribute partialScore="10"><True/></Attribute>
      </Characteristic>
    </Characteristics>
  </Scorecard>
</PMML>"""


class PMMLTest(unittest.TestCase):
    def test_a_scorecard_adds_points_and_ranks_reason_codes(self):
        model = PMMLModel(SCORECARD)
        young_renter = model.predict({"age": 22, "residence": "renter"})
        self.assertEqual(young_renter["prediction"], 100 + 5 + 10)
        # Ranked by points lost, NOT by declaration order: residence lost 30 against its
        # baseline, age 15 -- the reverse of how the characteristics are declared.
        self.assertEqual(young_renter["reason_codes"], ["RC_RES", "RC_AGE"])
        self.assertEqual(model.predict({"age": 60, "residence": "company housing"})["prediction"], 155,
                         "a quoted multi-word set member must match")

    def test_a_missing_value_bins_where_the_scorecard_says(self):
        self.assertEqual(PMMLModel(SCORECARD).predict({"age": 30})["prediction"], 100 + 20 + 0)

    def test_an_unbinned_value_is_an_error_not_zero_points(self):
        card = SCORECARD.replace(b'<Attribute partialScore="10"><True/></Attribute>', b"")
        with self.assertRaises(ValueError):
            PMMLModel(card).predict({"age": 30, "residence": "renter"})

    def test_a_tree_follows_its_predicates(self):
        tree = b"""<PMML xmlns="http://www.dmg.org/PMML-4_4" version="4.4"><DataDictionary/>
          <TreeModel functionName="classification" noTrueChildStrategy="returnNullPrediction">
            <MiningSchema><MiningField name="dti"/><MiningField name="y" usageType="predicted"/></MiningSchema>
            <Node score="0"><True/>
              <Node score="1"><SimplePredicate field="dti" operator="greaterThan" value="0.4"/>
                <ScoreDistribution value="0" recordCount="10"/><ScoreDistribution value="1" recordCount="30"/></Node>
              <Node score="0"><SimplePredicate field="dti" operator="lessOrEqual" value="0.4"/>
                <ScoreDistribution value="0" recordCount="45"/><ScoreDistribution value="1" recordCount="5"/></Node>
            </Node>
          </TreeModel></PMML>"""
        model = PMMLModel(tree)
        high = model.predict({"dti": 0.55})
        self.assertEqual(high["prediction"], "1")
        self.assertEqual(high["probabilities"], [0.25, 0.75])
        self.assertEqual(model.predict({"dti": 0.2})["prediction"], "0")

    def test_unsupported_models_are_refused_by_name_at_upload(self):
        mining = b"""<PMML xmlns="http://www.dmg.org/PMML-4_4" version="4.4"><DataDictionary/>
          <MiningModel functionName="classification"><MiningSchema/></MiningModel></PMML>"""
        with self.assertRaises(PMMLUnsupported) as caught:
            PMMLModel(mining)
        self.assertIn("MiningModel", str(caught.exception))
        result = model_executor.validate_model(mining)
        self.assertFalse(result["valid"])
        self.assertIn("MiningModel", result["error"])

    def test_entity_expansion_is_refused(self):
        """PMML is customer XML. A billion-laughs document must be rejected, not expanded."""
        # Otherwise a VALID model: without the DOCTYPE this document loads. So the only thing that
        # can reject it is the refusal to expand entities -- not "no model element".
        valid = _blob("logistic.pmml").replace(b'<?xml version="1.0" encoding="UTF-8"?>\n', b"")
        PMMLModel(valid)
        bomb = (b'<?xml version="1.0"?>\n<!DOCTYPE PMML [<!ENTITY lol "lol">'
                b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>\n'
                + valid.replace(b'description="RuleMind test fixture: logistic regression"', b'description="&lol2;"'))
        with self.assertRaises(ValueError) as caught:
            PMMLModel(bomb)
        self.assertIn("Entit", str(caught.exception), "rejected, but not for its entities")


class UploadEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "models.db"))
        app_main.storage = self.storage
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.key = {"x-api-key": self.storage.generate_api_key_for_tenant(self.storage.default_tenant_id)["plaintext"]}

    def test_every_format_uploads_and_predicts_over_http(self):
        for name in ("logistic.onnx", "logistic.pmml", "xgboost.json", "lightgbm.txt"):
            created = self.client.post("/api/v1/models", headers=self.key, json={
                "name": name, "model_type": "sklearn",  # a WRONG declared type, on purpose
                "model_base64": base64.b64encode(_blob(name)).decode(),
                "input_schema": {"features": FEATURES}})
            self.assertEqual(created.status_code, 200, created.text)
            model = created.json()
            self.assertEqual(model["model_format"], model_executor.detect_format(_blob(name)))
            predicted = self.client.post("/api/v1/models/{0}/predict".format(model["id"]), headers=self.key,
                                         json={"input_data": PROBES[2]})
            self.assertEqual(predicted.status_code, 200, (name, predicted.text))

    def test_an_unsupported_pmml_is_a_422_naming_the_element(self):
        body = b"<PMML version='4.4'><NeuralNetwork functionName='regression'><MiningSchema/></NeuralNetwork></PMML>"
        response = self.client.post("/api/v1/models", headers=self.key, json={
            "name": "nn", "model_base64": base64.b64encode(body).decode()})
        self.assertEqual(response.status_code, 422)
        self.assertIn("NeuralNetwork", response.text)


if __name__ == "__main__":
    unittest.main()
