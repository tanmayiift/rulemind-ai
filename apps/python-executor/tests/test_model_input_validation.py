"""Model inference input handling.

Two defects motivated these: a feature absent from the request was silently
replaced with 0 (indistinguishable from a real zero, so a dropped upstream field
produced a confident wrong prediction), and a non-numeric value raised out of
numpy and surfaced as a 500 for what is plainly a caller error.
"""
import pickle
import unittest

from app.model_executor import INVALID_INPUT, execute_model, prepare_features

FEATURES = ["a", "b", "c"]


class _Stub:
    """Minimal sklearn-shaped model: predicts 1 when the row sums above 10."""
    classes_ = [0, 1]

    def predict(self, x):
        return [1 if sum(row) > 10 else 0 for row in x]

    def predict_proba(self, x):
        return [[0.2, 0.8] if sum(row) > 10 else [0.8, 0.2] for row in x]


def _blob():
    return pickle.dumps(_Stub())


class PrepareFeaturesTests(unittest.TestCase):
    def test_orders_values_by_the_feature_list(self):
        out = prepare_features({"c": 3, "a": 1, "b": 2}, FEATURES)
        self.assertEqual(out["values"], [[1.0, 2.0, 3.0]])
        self.assertEqual(out["imputed_features"], [])

    def test_missing_feature_is_rejected_by_default(self):
        out = prepare_features({"a": 1}, FEATURES)
        self.assertEqual(out["error_kind"], INVALID_INPUT)
        self.assertIn("b", out["error"])
        self.assertIn("c", out["error"])

    def test_none_counts_as_missing_not_as_zero(self):
        out = prepare_features({"a": 1, "b": None, "c": 3}, FEATURES)
        self.assertEqual(out["error_kind"], INVALID_INPUT)
        self.assertIn("b", out["error"])

    def test_allow_missing_imputes_and_reports_what_it_imputed(self):
        out = prepare_features({"a": 1}, FEATURES, allow_missing=True)
        self.assertNotIn("error", out)
        self.assertEqual(out["values"], [[1.0, 0.0, 0.0]])
        self.assertEqual(out["imputed_features"], ["b", "c"])

    def test_non_numeric_is_a_caller_error_naming_the_field(self):
        out = prepare_features({"a": 1, "b": "oops", "c": 3}, FEATURES)
        self.assertEqual(out["error_kind"], INVALID_INPUT)
        self.assertIn("b='oops'", out["error"])

    def test_booleans_coerce_to_one_and_zero(self):
        out = prepare_features({"a": True, "b": False, "c": 2}, FEATURES)
        self.assertEqual(out["values"], [[1.0, 0.0, 2.0]])

    def test_numeric_strings_are_accepted(self):
        out = prepare_features({"a": "1.5", "b": 2, "c": 3}, FEATURES)
        self.assertEqual(out["values"], [[1.5, 2.0, 3.0]])

    def test_no_features_at_all_is_a_caller_error(self):
        out = prepare_features({}, [])
        self.assertEqual(out["error_kind"], INVALID_INPUT)


class ExecuteModelTests(unittest.TestCase):
    def test_happy_path_predicts(self):
        r = execute_model(_blob(), {"a": 5, "b": 5, "c": 5}, FEATURES)
        self.assertIsNone(r["error"])
        self.assertEqual(r["prediction"], 1)
        self.assertEqual(r["imputed_features"], [])

    def test_caller_error_is_tagged_so_the_router_can_answer_422(self):
        r = execute_model(_blob(), {"a": "x", "b": 1, "c": 1}, FEATURES)
        self.assertEqual(r["error_kind"], INVALID_INPUT)
        self.assertIsNone(r["prediction"])

    def test_missing_feature_does_not_silently_become_zero(self):
        # The bug: {"a": 5} scored identically to {"a": 5, "b": 0, "c": 0}.
        rejected = execute_model(_blob(), {"a": 5}, FEATURES)
        self.assertEqual(rejected["error_kind"], INVALID_INPUT)

        imputed = execute_model(_blob(), {"a": 5}, FEATURES, allow_missing=True)
        explicit = execute_model(_blob(), {"a": 5, "b": 0, "c": 0}, FEATURES)
        self.assertEqual(imputed["prediction"], explicit["prediction"])
        # ...but only the imputed one admits that it guessed.
        self.assertEqual(imputed["imputed_features"], ["b", "c"])
        self.assertEqual(explicit["imputed_features"], [])


if __name__ == "__main__":
    unittest.main()
