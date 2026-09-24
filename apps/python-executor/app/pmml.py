"""A PMML evaluator for the model types a credit-risk team actually exports (plan 6.3).

WHY IN-HOUSE. Every maintained Python PMML scorer (pypmml, jpmml-evaluator, pmml4s) runs on a JVM.
Shipping a JVM in the API image to score a regression is not a trade a self-hosted product should
make for its customers. What credit shops export as PMML is overwhelmingly three things -- a
Scorecard (SAS, FICO Model Builder, R `scorecard`), a logistic/linear RegressionModel, and a
TreeModel -- and all three are small, closed-form evaluations.

WHAT IS SUPPORTED, AND NOTHING ELSE. `Scorecard` (baseline score, per-characteristic attributes,
reason codes), `RegressionModel` (regression, and classification with the `logit`, `softmax` or
`none` normalizations), `TreeModel` (`returnLastPrediction` / `returnNullPrediction`
no-true-child strategies). Predicates: SimplePredicate, CompoundPredicate (and/or/xor/surrogate),
SimpleSetPredicate, True, False. Anything else -- MiningModel ensembles, NeuralNetwork,
TransformationDictionary derived fields -- is REFUSED at upload with the element named, rather
than evaluated approximately. A scoring model that is silently a little wrong is the worst
possible failure for a credit decision.

SAFE PARSING. PMML is XML from the customer, so it is parsed with defusedxml: no entity expansion
(billion laughs), no external entities (XXE). Still evaluated in the isolated model worker like
every other format, for the resource limits.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from defusedxml import ElementTree as SafeET

SUPPORTED = ("Scorecard", "RegressionModel", "TreeModel")
_MODEL_ELEMENTS = ("Scorecard", "RegressionModel", "TreeModel", "MiningModel", "NeuralNetwork",
                   "SupportVectorMachineModel", "NaiveBayesModel", "ClusteringModel", "GeneralRegressionModel",
                   "RuleSetModel", "NearestNeighborModel", "BayesianNetworkModel", "GaussianProcessModel",
                   "AssociationModel", "SequenceModel", "TextModel", "TimeSeriesModel", "AnomalyDetectionModel")


class PMMLUnsupported(ValueError):
    """The document is valid PMML but uses something this evaluator does not implement."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: Any, name: str) -> List[Any]:
    return [child for child in list(node) if _local(child.tag) == name]


def _child(node: Any, name: str) -> Optional[Any]:
    found = _children(node, name)
    return found[0] if found else None


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PMMLModel:
    """A parsed, validated PMML model. `predict(row)` takes a {field: value} dict."""

    def __init__(self, blob: bytes) -> None:
        try:
            root = SafeET.fromstring(blob)
        except Exception as exc:  # defusedxml raises its own subclasses for entity attacks
            raise ValueError("Not a well-formed PMML document: {0}".format(exc)) from exc
        if _local(root.tag) != "PMML":
            raise ValueError("Root element is <{0}>, not <PMML>.".format(_local(root.tag)))
        self.version = root.get("version", "")
        models = [child for child in list(root) if _local(child.tag) in _MODEL_ELEMENTS]
        if not models:
            raise ValueError("The PMML document contains no model element.")
        if len(models) > 1:
            raise PMMLUnsupported("The document holds {0} models; exactly one is supported.".format(len(models)))
        if _child(root, "TransformationDictionary") is not None:
            raise PMMLUnsupported("TransformationDictionary (derived fields) is not supported.")
        self.model = models[0]
        self.kind = _local(self.model.tag)
        if self.kind not in SUPPORTED:
            raise PMMLUnsupported("<{0}> is not supported; supported models are {1}.".format(
                self.kind, ", ".join(SUPPORTED)))
        if _child(self.model, "LocalTransformations") is not None:
            raise PMMLUnsupported("LocalTransformations (derived fields) are not supported.")
        self.function = self.model.get("functionName", "")
        schema = _child(self.model, "MiningSchema")
        if schema is None:
            raise ValueError("<{0}> has no MiningSchema.".format(self.kind))
        self.inputs: List[str] = []
        self.target: Optional[str] = None
        for field in _children(schema, "MiningField"):
            usage = field.get("usageType", "active")
            if usage in ("predicted", "target"):
                self.target = field.get("name")
            elif usage == "active":
                self.inputs.append(field.get("name"))
        self._validate()

    # ── validation: walk everything once at upload, so scoring cannot hit an unsupported node ──
    def _validate(self) -> None:
        if self.kind == "Scorecard":
            characteristics = _child(self.model, "Characteristics")
            if characteristics is None or not _children(characteristics, "Characteristic"):
                raise ValueError("Scorecard has no Characteristics.")
            for characteristic in _children(characteristics, "Characteristic"):
                for attribute in _children(characteristic, "Attribute"):
                    if _child(attribute, "ComplexPartialScore") is not None:
                        raise PMMLUnsupported("ComplexPartialScore is not supported.")
                    if attribute.get("partialScore") is None:
                        raise ValueError("A Scorecard Attribute has no partialScore.")
                    self._check_predicate(self._predicate_of(attribute))
        elif self.kind == "RegressionModel":
            tables = _children(self.model, "RegressionTable")
            if not tables:
                raise ValueError("RegressionModel has no RegressionTable.")
            for table in tables:
                for unsupported in ("CategoricalPredictor", "PredictorTerm"):
                    if _child(table, unsupported) is not None:
                        raise PMMLUnsupported("{0} is not supported.".format(unsupported))
            norm = self.model.get("normalizationMethod", "none")
            if norm not in ("none", "logit", "softmax"):
                raise PMMLUnsupported("normalizationMethod {0!r} is not supported.".format(norm))
        elif self.kind == "TreeModel":
            root = _child(self.model, "Node")
            if root is None:
                raise ValueError("TreeModel has no root Node.")
            strategy = self.model.get("noTrueChildStrategy", "returnNullPrediction")
            if strategy not in ("returnNullPrediction", "returnLastPrediction"):
                raise PMMLUnsupported("noTrueChildStrategy {0!r} is not supported.".format(strategy))
            if self.model.get("missingValueStrategy", "none") not in ("none",):
                raise PMMLUnsupported("missingValueStrategy other than 'none' is not supported.")
            stack = [root]
            while stack:
                node = stack.pop()
                self._check_predicate(self._predicate_of(node))
                stack.extend(_children(node, "Node"))

    @staticmethod
    def _predicate_of(node: Any) -> Any:
        for child in list(node):
            if _local(child.tag) in ("SimplePredicate", "CompoundPredicate", "SimpleSetPredicate",
                                     "True", "False"):
                return child
        raise ValueError("<{0}> has no predicate.".format(_local(node.tag)))

    def _check_predicate(self, predicate: Any) -> None:
        kind = _local(predicate.tag)
        if kind == "CompoundPredicate":
            if predicate.get("booleanOperator") not in ("and", "or", "xor", "surrogate"):
                raise PMMLUnsupported("CompoundPredicate booleanOperator {0!r} is not supported.".format(
                    predicate.get("booleanOperator")))
            for child in list(predicate):
                if _local(child.tag) != "Extension":
                    self._check_predicate(child)
        elif kind == "SimplePredicate":
            if predicate.get("operator") not in ("equal", "notEqual", "lessThan", "lessOrEqual",
                                                 "greaterThan", "greaterOrEqual", "isMissing", "isNotMissing"):
                raise PMMLUnsupported("SimplePredicate operator {0!r} is not supported.".format(
                    predicate.get("operator")))
        elif kind == "SimpleSetPredicate":
            if predicate.get("booleanOperator") not in ("isIn", "isNotIn"):
                raise PMMLUnsupported("SimpleSetPredicate operator is not supported.")

    # ── predicates, with PMML's three-valued logic (None = UNKNOWN) ────────────────────────
    def _eval(self, predicate: Any, row: Dict[str, Any]) -> Optional[bool]:
        kind = _local(predicate.tag)
        if kind == "True":
            return True
        if kind == "False":
            return False
        if kind == "SimplePredicate":
            value = row.get(predicate.get("field"))
            operator = predicate.get("operator")
            if operator == "isMissing":
                return value is None
            if operator == "isNotMissing":
                return value is not None
            if value is None:
                return None
            expected = predicate.get("value")
            left, right = _num(value), _num(expected)
            if left is None or right is None:
                left, right = str(value), str(expected)  # categorical comparison
            return {"equal": left == right, "notEqual": left != right, "lessThan": left < right,
                    "lessOrEqual": left <= right, "greaterThan": left > right,
                    "greaterOrEqual": left >= right}[operator]
        if kind == "SimpleSetPredicate":
            value = row.get(predicate.get("field"))
            if value is None:
                return None
            array = _child(predicate, "Array")
            members = _parse_array(array.text if array is not None else "")
            hit = str(value) in members or (_num(value) is not None and any(
                _num(m) == _num(value) for m in members))
            return hit if predicate.get("booleanOperator") == "isIn" else not hit
        # CompoundPredicate
        operator = predicate.get("booleanOperator")
        results = [self._eval(child, row) for child in list(predicate) if _local(child.tag) != "Extension"]
        if operator == "surrogate":
            for result in results:
                if result is not None:
                    return result
            return None
        if operator == "and":
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        if operator == "or":
            if any(r is True for r in results):
                return True
            return None if any(r is None for r in results) else False
        if any(r is None for r in results):  # xor
            return None
        return sum(1 for r in results if r) % 2 == 1

    # ── scoring ────────────────────────────────────────────────────────────────────────────
    def predict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        missing = [name for name in self.inputs if row.get(name) is None]
        if self.kind == "Scorecard":
            return self._scorecard(row)
        if self.kind == "RegressionModel":
            if missing:
                raise ValueError("Missing value for feature(s): {0}.".format(", ".join(sorted(missing))))
            return self._regression(row)
        return self._tree(row)

    def _scorecard(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Baseline + one attribute per characteristic (the FIRST whose predicate is true). A
        characteristic with no matching attribute is an error, not a zero: a scorecard that
        silently scores an unbinned value as 0 points is the scorecard-shaped fail-open."""
        score = _num(self.model.get("initialScore")) or 0.0
        use_reasons = self.model.get("useReasonCodes", "true") == "true"
        points_below = self.model.get("reasonCodeAlgorithm", "pointsBelow") == "pointsBelow"
        reasons: List[Tuple[float, str]] = []
        for characteristic in _children(_child(self.model, "Characteristics"), "Characteristic"):
            matched = None
            for attribute in _children(characteristic, "Attribute"):
                if self._eval(self._predicate_of(attribute), row) is True:
                    matched = attribute
                    break
            if matched is None:
                raise ValueError("No attribute of characteristic {0!r} matches the input.".format(
                    characteristic.get("name")))
            partial = float(matched.get("partialScore"))
            score += partial
            code = matched.get("reasonCode") or characteristic.get("reasonCode")
            baseline = _num(characteristic.get("baselineScore"))
            if baseline is None:
                baseline = _num(self.model.get("baselineScore"))
            if use_reasons and code and baseline is not None:
                reasons.append(((baseline - partial) if points_below else (partial - baseline), code))
        ranked = [code for gap, code in sorted(reasons, key=lambda item: -item[0]) if gap > 0]
        return {"prediction": score, "probabilities": None, "reason_codes": ranked}

    def _regression(self, row: Dict[str, Any]) -> Dict[str, Any]:
        tables = _children(self.model, "RegressionTable")
        linear: List[Tuple[Optional[str], float]] = []
        for table in tables:
            value = _num(table.get("intercept")) or 0.0
            for predictor in _children(table, "NumericPredictor"):
                exponent = int(predictor.get("exponent", "1"))
                value += float(predictor.get("coefficient")) * (float(_num(row.get(predictor.get("name")))) ** exponent)
            linear.append((table.get("targetCategory"), value))
        norm = self.model.get("normalizationMethod", "none")
        if self.function == "regression":
            value = linear[0][1]
            return {"prediction": 1.0 / (1.0 + math.exp(-value)) if norm == "logit" else value,
                    "probabilities": None}
        if norm == "logit":
            # PMML 4.4 RegressionModel, classification + logit: p_j = 1 / (1 + exp(-y_j)) for every
            # category but the last, and the last takes the remainder. (The last table is usually
            # the reference category with intercept 0, but the spec does not use its value.)
            head = [1.0 / (1.0 + math.exp(-v)) for _, v in linear[:-1]]
            probs = head + [1.0 - sum(head)]
        elif norm == "softmax":
            peak = max(v for _, v in linear)
            exps = [math.exp(v - peak) for _, v in linear]
            probs = [e / sum(exps) for e in exps]
        else:
            probs = [v for _, v in linear]
        categories = [c for c, _ in linear]
        best = max(range(len(probs)), key=lambda i: probs[i])
        order = sorted(range(len(categories)), key=lambda i: str(categories[i]))
        return {"prediction": categories[best],
                "probabilities": [probs[i] for i in order],
                "model_classes": [categories[i] for i in order]}

    def _tree(self, row: Dict[str, Any]) -> Dict[str, Any]:
        node = _child(self.model, "Node")
        if self._eval(self._predicate_of(node), row) is not True:
            raise ValueError("The tree's root predicate does not match the input.")
        last_prediction = self.model.get("noTrueChildStrategy") == "returnLastPrediction"
        while True:
            children = _children(node, "Node")
            if not children:
                break
            chosen = next((c for c in children if self._eval(self._predicate_of(c), row) is True), None)
            if chosen is None:
                if last_prediction and node.get("score") is not None:
                    break
                raise ValueError("No branch of the tree matches the input.")
            node = chosen
        score = node.get("score")
        if score is None:
            raise ValueError("The matched tree node has no score.")
        distributions = _children(node, "ScoreDistribution")
        probabilities = None
        classes = None
        if distributions:
            total = sum(float(d.get("recordCount", 0)) for d in distributions)
            ordered = sorted(distributions, key=lambda d: str(d.get("value")))
            classes = [d.get("value") for d in ordered]
            probabilities = [(_num(d.get("probability")) if d.get("probability") is not None
                              else (float(d.get("recordCount", 0)) / total if total else 0.0)) for d in ordered]
        prediction: Any = _num(score) if self.function == "regression" else score
        return {"prediction": prediction, "probabilities": probabilities, "model_classes": classes}


def _parse_array(text: str) -> List[str]:
    """PMML Array content: whitespace-separated, double-quoted when a member has spaces."""
    members: List[str] = []
    token, quoted = "", False
    for char in (text or "").strip():
        if char == '"':
            quoted = not quoted
            continue
        if char.isspace() and not quoted:
            if token:
                members.append(token)
                token = ""
            continue
        token += char
    if token:
        members.append(token)
    return members
