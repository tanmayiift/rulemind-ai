import "dart:math" as math;

import "models.dart";
import "safe_formula.dart";

/// Offline scorecard scoring, byte-for-byte with the Python core (`app/logic.py:evaluate_scorecard`,
/// the declared source of truth for cross-engine semantics).
///
/// This used to implement POINTS ONLY and silently ignore `scoring_method`, `weight`,
/// `coefficient`, `woe_values`, `intercept`, `pdo`, `target_score`, `target_odds`, `formula` and
/// `metrics` — so a WoE scorecard returned a *different number* on-device than the server returned
/// for the same applicant, with no error and no signal. All four methods are implemented here:
///
///  - **points**    `clamp(base_score + Σ trunc(points × weight))`
///  - **weighted**  the same path; `weight` is simply not 1.0 (Python has no separate branch)
///  - **woe**       `clamp(round(offset + factor × (intercept + Σ coefficient × woe)))`
///  - **formula**   `clamp(trunc(evaluateFormula(formula)))`, falling back to `base_score` on error
///
/// Every clamp is `max(0, min(max_score, x))`, and the WoE `round` is Python's round-half-to-EVEN.
class ScorecardEvaluator {
  static const String _defaultFormula = "base_score + total_points";

  Map<String, dynamic> evaluate(
    Scorecard scorecard,
    Map<String, dynamic> variables, [
    Map<String, Map<String, dynamic>> variableLookup = const <String, Map<String, dynamic>>{},
  ]) {
    final baseScore = scorecard.baseScore;
    final maxScore = scorecard.maxScore;
    final scoringMethod = scorecard.scoringMethod;

    final breakdown = <Map<String, dynamic>>[];
    final woeDetails = <Map<String, dynamic>>[];
    var totalPoints = 0;
    var totalWoeScore = 0.0;
    var totalIv = 0.0;

    for (final factor in scorecard.bins) {
      final variableId = factor.variableId;
      // Python: `variable_values.get(variable_id, 0)` — a MISSING variable scores as 0, a variable
      // explicitly set to null matches no bin at all (float(None) raises).
      final dynamic value =
          variableId != null && variables.containsKey(variableId) ? variables[variableId] : 0;
      final numericValue = _pythonFloat(value);
      final lookup = variableId == null ? null : variableLookup[variableId];

      ScoreRange? activeRange;
      if (numericValue != null) {
        for (final range in factor.ranges) {
          if (range.matchable && numericValue >= range.lowerBound && numericValue <= range.upperBound) {
            activeRange = range;
            break;
          }
        }
      }

      WoeRange? activeWoeRange;
      if (numericValue != null) {
        for (final range in factor.woeValues) {
          if (range.matchable && numericValue >= range.lowerBound && numericValue <= range.upperBound) {
            activeWoeRange = range;
            break;
          }
        }
      }

      final points = activeRange?.points ?? 0;
      // Python `int(points * weight)` truncates TOWARD ZERO — not floor, not round.
      final weightedPoints = _truncate(points * factor.weight);
      totalPoints += weightedPoints;

      final woe = activeWoeRange?.woe ?? 0.0;
      final iv = activeWoeRange?.iv ?? 0.0;
      totalIv += iv;
      totalWoeScore += factor.coefficient * woe;

      if (activeWoeRange != null) {
        woeDetails.add(<String, dynamic>{
          "variable_id": variableId,
          "variable_name": lookup?["name"] ?? variableId,
          "value": value,
          "woe": woe,
          "iv": iv,
          "coefficient": factor.coefficient,
          "contribution": factor.coefficient * woe,
          "event_rate": activeWoeRange.eventRate,
          "non_event_rate": activeWoeRange.nonEventRate,
        });
      }

      breakdown.add(<String, dynamic>{
        "variable_id": variableId,
        "variable_name": lookup?["name"] ?? variableId,
        "source_id": lookup?["source_id"],
        "value": value,
        "active_range": activeRange == null
            ? null
            : <String, dynamic>{
                "min": activeRange.min,
                "max": activeRange.max,
                "points": activeRange.points,
              },
        "points": points,
        "weighted_points": weightedPoints,
        "weight": factor.weight,
        "woe": woe,
        "coefficient": factor.coefficient,
      });
    }

    final int score;
    switch (scoringMethod) {
      case "woe":
        // score = offset + factor × (intercept + Σ coefficient × WoE), where factor = PDO / ln(2)
        // and offset = target_score − factor × ln(target_odds).
        final factorValue = scorecard.pdo > 0 ? scorecard.pdo / math.ln2 : 1.0;
        final offset = scorecard.targetOdds > 0
            ? scorecard.targetScore - factorValue * math.log(scorecard.targetOdds)
            : scorecard.targetScore;
        final raw = offset + factorValue * (scorecard.intercept + totalWoeScore);
        score = _clamp(bankersRound(raw), maxScore);
      case "formula":
        final formula = scorecard.formula ?? _defaultFormula;
        final context = _formulaContext(scorecard, variables, breakdown, totalPoints, totalWoeScore);
        final result = _evaluate(formula, context);
        // Python: `int(result) if isinstance(result, (int, float)) else base_score`.
        // A formula that errors scores base_score — it never silently falls back to points.
        final int raw;
        if (result is bool) {
          raw = result ? 1 : 0;
        } else if (result is num) {
          raw = _truncate(result);
        } else {
          raw = baseScore;
        }
        score = _clamp(raw, maxScore);
      default:
        score = _clamp(baseScore + totalPoints, maxScore);
    }

    final metrics = <String, dynamic>{};
    for (final metric in scorecard.metrics) {
      final context = _formulaContext(
        scorecard, variables, breakdown, totalPoints, totalWoeScore,
        // Ordering matters: Python seeds these first, so a factor whose variable_id is literally
        // "score" or "total_iv" shadows them.
        extraBase: <String, dynamic>{"score": score, "total_iv": totalIv},
      );
      metrics[metric.name] = <String, dynamic>{
        "value": _evaluate(metric.formula, context),
        "unit": metric.unit,
        "category": metric.category,
      };
    }

    final result = <String, dynamic>{
      "score": score,
      "base_score": baseScore,
      "max_score": maxScore,
      "scoring_method": scoringMethod,
      "breakdown": breakdown,
    };
    if (woeDetails.isNotEmpty) {
      result["woe_details"] = woeDetails;
      result["information_value"] = totalIv;
    }
    if (metrics.isNotEmpty) {
      result["metrics"] = metrics;
    }
    return result;
  }

  Map<String, dynamic> _formulaContext(
    Scorecard scorecard,
    Map<String, dynamic> variables,
    List<Map<String, dynamic>> breakdown,
    int totalPoints,
    double totalWoeScore, {
    Map<String, dynamic> extraBase = const <String, dynamic>{},
  }) {
    final context = <String, dynamic>{
      ...extraBase,
      "base_score": scorecard.baseScore,
      "max_score": scorecard.maxScore,
      "total_points": totalPoints,
      "total_woe_score": totalWoeScore,
      "variables": variables,
    };
    for (final entry in breakdown) {
      // A bin with no variable_id binds the Python dict key `None`, which no formula Name can ever
      // reference — so there is nothing to bind here either.
      final id = entry["variable_id"] as String?;
      if (id == null) continue;
      context[id] = entry["value"];
    }
    // `scope.setdefault` in the Python core: these are names, never a reachable builtins map.
    context.putIfAbsent("True", () => true);
    context.putIfAbsent("False", () => false);
    context.putIfAbsent("None", () => null);
    return context;
  }

  /// Mirrors `logic._evaluate_formula`: a bad formula yields `{"error": ...}` rather than raising
  /// into the scoring path, so the caller sees the failure instead of a plausible wrong number.
  dynamic _evaluate(String formula, Map<String, dynamic> context) {
    try {
      return evaluateFormula(formula, context);
    } on FormulaError catch (error) {
      return <String, dynamic>{"error": error.message};
    }
  }

  /// Python's `float(x)`, returning null where `float()` would raise TypeError/ValueError.
  double? _pythonFloat(dynamic value) {
    if (value is bool) return value ? 1.0 : 0.0;
    if (value is num) return value.toDouble();
    if (value is String) return double.tryParse(value.trim());
    return null;
  }

  int _truncate(num value) {
    if (value is double && (value.isNaN || value.isInfinite)) return 0;
    return value.truncate();
  }

  int _clamp(int value, int maxScore) => math.max(0, math.min(maxScore, value));
}
