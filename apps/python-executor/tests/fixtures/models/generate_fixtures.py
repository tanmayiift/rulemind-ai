"""Regenerate the model-format fixtures and the predictions they must reproduce.

    pip install --target /tmp/onnxgen onnx==1.19.1 skl2onnx==1.19.1 ml_dtypes --no-deps
    PYTHONPATH=/tmp/onnxgen python tests/fixtures/models/generate_fixtures.py

One dataset, one feature order, fitted by each library. `expected.json` records what each NATIVE
library predicts for a set of probe rows, so the tests compare RuleMind's loaders against the
libraries themselves -- not against RuleMind. onnx/skl2onnx are needed only here, never at runtime.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from xml.sax.saxutils import quoteattr

import lightgbm as lgb
import numpy as np
import xgboost as xgb
from sklearn.linear_model import LogisticRegression

HERE = Path(__file__).resolve().parent
FEATURES = ["bureau_score", "dti", "income_k"]
PROBES = [
    {"bureau_score": 780, "dti": 0.12, "income_k": 95},
    {"bureau_score": 610, "dti": 0.55, "income_k": 28},
    {"bureau_score": 700, "dti": 0.30, "income_k": 50},
    {"bureau_score": 655, "dti": 0.41, "income_k": 61},
    {"bureau_score": 742, "dti": 0.22, "income_k": 40},
]


def _data():
    rng = np.random.RandomState(7)
    n = 400
    x = np.column_stack([rng.normal(690, 55, n), rng.uniform(0.02, 0.65, n), rng.normal(55, 18, n)])
    logit = 0.03 * (x[:, 0] - 690) - 6.0 * (x[:, 1] - 0.3) + 0.02 * (x[:, 2] - 55)
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int)
    return x.astype(np.float32), y


def _probe_matrix():
    return np.array([[row[f] for f in FEATURES] for row in PROBES], dtype=np.float32)


def main() -> None:
    x, y = _data()
    probes = _probe_matrix()
    expected = {"features": FEATURES, "probes": PROBES}

    lr = LogisticRegression(max_iter=5000).fit(x, y)
    (HERE / "logistic.pkl").write_bytes(pickle.dumps(lr, protocol=5))
    expected["sklearn_logistic_proba1"] = lr.predict_proba(probes)[:, 1].tolist()

    from skl2onnx import to_onnx  # generation-time only

    onnx_model = to_onnx(lr, x[:1], target_opset={"": 17, "ai.onnx.ml": 3})
    (HERE / "logistic.onnx").write_bytes(onnx_model.SerializeToString())

    booster = xgb.train({"objective": "binary:logistic", "max_depth": 2, "eta": 0.3, "seed": 7},
                        xgb.DMatrix(x, label=y, feature_names=FEATURES), num_boost_round=6)
    (HERE / "xgboost.json").write_bytes(bytes(booster.save_raw("json")))
    expected["xgboost_proba1"] = booster.predict(xgb.DMatrix(probes, feature_names=FEATURES)).tolist()

    lgbm = lgb.train({"objective": "binary", "num_leaves": 4, "min_data_in_leaf": 10, "verbose": -1,
                      "seed": 7, "deterministic": True},
                     lgb.Dataset(x, label=y, feature_name=FEATURES), num_boost_round=6)
    (HERE / "lightgbm.txt").write_text(lgbm.model_to_string(), encoding="utf-8")
    expected["lightgbm_proba1"] = lgbm.predict(probes).tolist()

    # PMML RegressionModel with the SAME coefficients as the sklearn model, written by hand the
    # way SAS/R exporters write binary logistic: the "1" table carries the linear term, "0" is the
    # reference. Its predictions must equal sklearn's.
    predictors = "\n".join('        <NumericPredictor name={0} coefficient="{1!r}"/>'.format(
        quoteattr(name), float(coef)) for name, coef in zip(FEATURES, lr.coef_[0], strict=True))
    pmml = """<?xml version="1.0" encoding="UTF-8"?>
<PMML xmlns="http://www.dmg.org/PMML-4_4" version="4.4">
  <Header description="RuleMind test fixture: logistic regression"/>
  <DataDictionary numberOfFields="4">
{fields}
    <DataField name="default" optype="categorical" dataType="string"><Value value="0"/><Value value="1"/></DataField>
  </DataDictionary>
  <RegressionModel functionName="classification" normalizationMethod="logit">
    <MiningSchema>
{mining}
      <MiningField name="default" usageType="target"/>
    </MiningSchema>
    <RegressionTable intercept="{intercept!r}" targetCategory="1">
{predictors}
    </RegressionTable>
    <RegressionTable intercept="0" targetCategory="0"/>
  </RegressionModel>
</PMML>
""".format(
        fields="\n".join('    <DataField name="{0}" optype="continuous" dataType="double"/>'.format(f) for f in FEATURES),
        mining="\n".join('      <MiningField name="{0}"/>'.format(f) for f in FEATURES),
        intercept=float(lr.intercept_[0]), predictors=predictors)
    (HERE / "logistic.pmml").write_text(pmml, encoding="utf-8")

    (HERE / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    print("wrote fixtures to", HERE)


if __name__ == "__main__":
    main()
