"""API tests for the model-evaluation router (app/routers/evaluations.py).

Covers CRUD over the scored-dataset path, the from-model path with a real pickled
sklearn LogisticRegression, and a LightGBM end-to-end that closes the "does RuleMind
support model eval + backtest for our models" question — skipped only if the LightGBM
native lib (libomp) is unavailable on the host."""
from __future__ import annotations

import base64
import os
import pickle
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402


def _binary_dataset(n=1500, seed=1):
    rng = np.random.default_rng(seed)
    p = rng.random(n)
    y = (rng.random(n) < p).astype(int)
    return p, y


class TestEvaluationsCRUD(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def test_create_get_list_delete(self):
        p, y = _binary_dataset()
        rows = [
            {"score": float(p[i]), "label": int(y[i]), "seg": "A" if i % 2 else "B", "d": f"2026-0{1 + i % 6}-01"}
            for i in range(len(p))
        ]
        r = self.client.post(
            "/api/v1/evaluations",
            json={"name": "unit eval", "task": "binary", "rows": rows,
                  "config": {"segment_col": "seg", "date_col": "d", "date_freq": "month"}},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("gini", body["metrics"])
        self.assertIsNotNone(body["metrics"]["gini"])
        self.assertEqual(len(body["segments"]["slices"]), 2)
        self.assertEqual(len(body["temporal"]["buckets"]), 6)
        self.assertIn(body["gate_status"], {"pass", "fail"})

        eid = body["id"]
        self.assertEqual(self.client.get(f"/api/v1/evaluations/{eid}", headers=self.headers).status_code, 200)
        listing = self.client.get("/api/v1/evaluations", headers=self.headers).json()
        self.assertTrue(any(e["id"] == eid for e in listing))
        self.assertEqual(self.client.delete(f"/api/v1/evaluations/{eid}", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/evaluations/{eid}", headers=self.headers).status_code, 404)

    def test_empty_rows_rejected(self):
        r = self.client.post("/api/v1/evaluations", json={"name": "x", "task": "binary", "rows": []}, headers=self.headers)
        self.assertEqual(r.status_code, 422)

    def test_backtest_requires_date_col(self):
        p, y = _binary_dataset(n=200)
        rows = [{"score": float(p[i]), "label": int(y[i])} for i in range(len(p))]
        r = self.client.post("/api/v1/evaluations/backtest", json={"name": "bt", "task": "binary", "rows": rows}, headers=self.headers)
        self.assertEqual(r.status_code, 422)


class TestFromModelSklearn(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def _host_model(self, clf, name):
        blob = base64.b64encode(pickle.dumps(clf)).decode("ascii")
        r = self.client.post("/api/v1/models", json={"name": name, "model_base64": blob}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_from_model_logistic_regression(self):
        from sklearn.linear_model import LogisticRegression

        p, y = _binary_dataset(n=1500, seed=2)
        rng = np.random.default_rng(2)
        X = np.column_stack([p, rng.random(len(p))])
        clf = LogisticRegression(max_iter=200).fit(X, y)
        mid = self._host_model(clf, "lr-eval-model")

        rows = [{"feature_0": float(X[i, 0]), "feature_1": float(X[i, 1]), "label": int(y[i])} for i in range(len(y))]
        r = self.client.post(
            f"/api/v1/evaluations/from-model/{mid}",
            json={"name": "lr eval", "features": ["feature_0", "feature_1"], "label_col": "label", "rows": rows},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["model_id"], mid)
        self.assertGreater(body["metrics"]["gini"], 0.3)  # feature_0 (=p) is predictive

    def test_from_model_404(self):
        r = self.client.post(
            "/api/v1/evaluations/from-model/does-not-exist",
            json={"name": "x", "features": ["a"], "rows": [{"a": 1, "label": 1}]},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 404)


def _lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_lightgbm_available(), "lightgbm native lib (libomp) unavailable on host")
class TestFromModelLightGBM(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def test_lightgbm_end_to_end(self):
        import lightgbm as lgb

        rng = np.random.default_rng(4)
        n = 2000
        X = rng.random((n, 4))
        y = (X[:, 0] + 0.5 * X[:, 1] + 0.2 * rng.random(n) > 0.9).astype(int)
        clf = lgb.LGBMClassifier(n_estimators=40, verbose=-1).fit(X, y)

        blob = base64.b64encode(pickle.dumps(clf)).decode("ascii")
        mid = self.client.post(
            "/api/v1/models", json={"name": "lgbm-eval", "model_type": "lightgbm", "model_base64": blob}, headers=self.headers
        ).json()["id"]

        feats = [f"feature_{i}" for i in range(4)]
        rows = [{**{feats[j]: float(X[i, j]) for j in range(4)}, "label": int(y[i])} for i in range(n)]
        r = self.client.post(
            f"/api/v1/evaluations/from-model/{mid}",
            json={"name": "lgbm eval", "features": feats, "label_col": "label", "rows": rows},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(r.json()["metrics"]["gini"], 0.5)  # LightGBM should learn the signal


if __name__ == "__main__":
    unittest.main()
